from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from codex_dispatch.adapters.discord import DiscordIncomingMessage
from codex_dispatch.codex_runner import (
    CodexQueueResult,
    CodexSessionUnavailableError,
)
from codex_dispatch.database import Database, DatabaseBusyError
from codex_dispatch.job_manager import JobManager
from codex_dispatch.managed_runner import CodexManagedCancelledError, CodexManagedResult
from codex_dispatch.notify_server import NotifyEvent


def notify(cwd: Path, thread_id: str = "thread-1", turn_id: str = "turn-1") -> NotifyEvent:
    return NotifyEvent(
        "agent-turn-complete",
        thread_id,
        turn_id,
        cwd,
        "codex-tui",
        (),
        "done",
    )


def incoming(message_id: int, content: str = "do work") -> DiscordIncomingMessage:
    return DiscordIncomingMessage(message_id, 100, 7001, 200, 300, content)


class JobManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.db = Database(self.root / "state" / "db.sqlite")
        self.db.open()
        self.db.register_notify(notify(self.workspace))
        self.db.set_discord_thread("thread-1", 7001)
        self.runner = AsyncMock()
        self.runner.queue_message.return_value = CodexQueueResult(0, "", "")
        self.discord = AsyncMock()
        self.discord.send_text.return_value = 9999
        self.manager = JobManager(
            self.db, self.runner, self.discord, allowed_roots=(self.root,)
        )

    async def _cleanup(self) -> None:
        self.db.close()
        self.temp.cleanup()

    async def test_idle_session_dispatches_first_job(self) -> None:
        await self.manager.handle_message(incoming(9001))
        job = self.db.get_job_by_discord_message(9001)
        assert job is not None
        self.assertEqual(job.status, "DISPATCHED")
        self.assertEqual(job.exit_code, 0)
        self.assertEqual(self.db.get_session("thread-1").status, "RUNNING")  # type: ignore[union-attr]
        self.runner.queue_message.assert_awaited_once_with(
            "thread-1", "do work", cwd=self.workspace
        )

    async def test_database_busy_returns_clear_feedback_without_dispatch(self) -> None:
        with patch.object(
            self.db,
            "create_job",
            side_effect=DatabaseBusyError("SQLite database is temporarily busy"),
        ):
            await self.manager.handle_message(incoming(9000, "retry me"))

        self.runner.queue_message.assert_not_awaited()
        self.assertEqual(self.db.job_count(), 0)
        self.discord.send_text.assert_awaited_once()
        self.assertIn("데이터베이스", self.discord.send_text.await_args.args[1])

    async def test_busy_session_keeps_followup_queued(self) -> None:
        await self.manager.handle_message(incoming(9001, "first"))
        await self.manager.handle_message(incoming(9002, "second"))
        first = self.db.get_job_by_discord_message(9001)
        second = self.db.get_job_by_discord_message(9002)
        assert first is not None and second is not None
        self.assertEqual(first.status, "DISPATCHED")
        self.assertEqual(second.status, "QUEUED")
        self.assertEqual(self.runner.queue_message.await_count, 1)

    async def test_duplicate_discord_message_is_idempotent(self) -> None:
        await self.manager.handle_message(incoming(9001))
        await self.manager.handle_message(incoming(9001))
        self.assertEqual(self.db.job_count(), 1)
        self.assertEqual(self.runner.queue_message.await_count, 1)

    async def test_three_concurrent_messages_dispatch_only_one(self) -> None:
        import asyncio

        await asyncio.gather(
            self.manager.handle_message(incoming(9001, "one")),
            self.manager.handle_message(incoming(9002, "two")),
            self.manager.handle_message(incoming(9003, "three")),
        )
        jobs = self.db.list_jobs("thread-1")
        self.assertEqual(len(jobs), 3)
        self.assertEqual(sum(job.status == "DISPATCHED" for job in jobs), 1)
        self.assertEqual(sum(job.status == "QUEUED" for job in jobs), 2)
        self.assertEqual(self.runner.queue_message.await_count, 1)

    async def test_completion_marks_active_job_complete_without_auto_run(self) -> None:
        await self.manager.handle_message(incoming(9001, "first"))
        await self.manager.handle_message(incoming(9002, "second"))
        completed = await self.manager.handle_completion("thread-1")
        assert completed is not None
        self.assertEqual(completed.status, "COMPLETED")
        queued = self.db.get_job_by_discord_message(9002)
        assert queued is not None
        self.assertEqual(queued.status, "QUEUED")
        self.assertEqual(self.runner.queue_message.await_count, 1)

    async def test_unavailable_session_marks_job_failed_and_session_offline(self) -> None:
        self.runner.queue_message.side_effect = CodexSessionUnavailableError("gone")
        await self.manager.handle_message(incoming(9001))
        job = self.db.get_job_by_discord_message(9001)
        assert job is not None
        self.assertEqual(job.status, "FAILED")
        self.assertEqual(self.db.get_session("thread-1").status, "OFFLINE")  # type: ignore[union-attr]

    async def test_dispatch_revalidates_workspace_after_symlink_replacement(self) -> None:
        outside_temp = tempfile.TemporaryDirectory()
        self.addCleanup(outside_temp.cleanup)
        outside = Path(outside_temp.name)
        self.workspace.rmdir()
        self.workspace.symlink_to(outside, target_is_directory=True)

        await self.manager.handle_message(incoming(9001, "must not execute"))

        job = self.db.get_job_by_discord_message(9001)
        assert job is not None
        self.assertEqual(job.status, "FAILED")
        self.assertEqual(self.db.get_session("thread-1").status, "ERROR")  # type: ignore[union-attr]
        self.runner.queue_message.assert_not_awaited()

    async def test_cancel_only_affects_queued_job_in_same_thread(self) -> None:
        await self.manager.handle_message(incoming(9001, "first"))
        await self.manager.handle_message(incoming(9002, "second"))
        result = await self.manager.cancel_text(7001, 2)
        self.assertIn("취소 완료", result)
        job = self.db.get_job(2)
        assert job is not None
        self.assertEqual(job.status, "CANCELLED")
        refused = await self.manager.cancel_text(7001, 1)
        self.assertIn("취소할 수 없습니다", refused)

    async def test_status_and_queue_text(self) -> None:
        await self.manager.handle_message(incoming(9001, "first task"))
        await self.manager.handle_message(incoming(9002, "second task"))
        status = self.manager.status_text(7001)
        queue = self.manager.queue_text(7001)
        self.assertIn("Status: RUNNING", status)
        self.assertIn("Auto Run: OFF", status)
        self.assertIn("Queued: 1", status)
        self.assertIn("#1 [DISPATCHED]", queue)
        self.assertIn("#2 [QUEUED]", queue)

    async def test_auto_run_dispatches_three_jobs_sequentially(self) -> None:
        await self.manager.handle_message(incoming(9401, "one"))
        await self.manager.handle_message(incoming(9402, "two"))
        await self.manager.handle_message(incoming(9403, "three"))
        enabled = await self.manager.auto_text(7001, "on")
        self.assertIn("Auto Run: ON", enabled)
        self.assertTrue(self.db.get_session("thread-1").auto_run)  # type: ignore[union-attr]

        self.db.set_session_status("thread-1", "IDLE")
        await self.manager.handle_completion("thread-1")
        await self.manager.wait_background_tasks()
        second = self.db.get_job_by_discord_message(9402)
        assert second is not None
        self.assertEqual(second.status, "DISPATCHED")
        self.assertEqual(self.runner.queue_message.await_count, 2)

        self.db.set_session_status("thread-1", "IDLE")
        await self.manager.handle_completion("thread-1")
        await self.manager.wait_background_tasks()
        third = self.db.get_job_by_discord_message(9403)
        assert third is not None
        self.assertEqual(third.status, "DISPATCHED")
        self.assertEqual(self.runner.queue_message.await_count, 3)

        self.db.set_session_status("thread-1", "IDLE")
        await self.manager.handle_completion("thread-1")
        await self.manager.wait_background_tasks()
        jobs = self.db.list_jobs("thread-1")
        self.assertTrue(all(job.status == "COMPLETED" for job in jobs))
        self.assertTrue(self.db.get_session("thread-1").auto_run)  # type: ignore[union-attr]

    async def test_auto_run_failure_pauses_queue(self) -> None:
        result = await self.manager.auto_text(7001, "on")
        self.assertIn("Auto Run: ON", result)
        self.runner.queue_message.side_effect = CodexSessionUnavailableError("gone")

        await self.manager.handle_message(incoming(9411, "will fail"))

        session = self.db.get_session("thread-1")
        job = self.db.get_job_by_discord_message(9411)
        assert session is not None and job is not None
        self.assertEqual(job.status, "FAILED")
        self.assertEqual(session.status, "OFFLINE")
        self.assertFalse(session.auto_run)

    async def test_auto_on_dispatches_existing_idle_queue_and_off_stops_followups(self) -> None:
        queued, _ = self.db.create_job("thread-1", 9421, "queued first")
        result = await self.manager.auto_text(7001, "on")
        self.assertIn(f"Job #{queued.id}", result)
        await self.manager.wait_background_tasks()
        self.assertEqual(self.db.get_job(queued.id).status, "DISPATCHED")  # type: ignore[union-attr]

        second, _ = self.db.create_job("thread-1", 9422, "queued second")
        disabled = await self.manager.auto_text(7001, "off")
        self.assertIn("Auto Run: OFF", disabled)
        self.db.set_session_status("thread-1", "IDLE")
        await self.manager.handle_completion("thread-1")
        await self.manager.wait_background_tasks()
        self.assertEqual(self.db.get_job(second.id).status, "QUEUED")  # type: ignore[union-attr]
        self.assertEqual(self.runner.queue_message.await_count, 1)

    async def test_auto_run_skips_cancelled_queued_job(self) -> None:
        await self.manager.handle_message(incoming(9431, "first"))
        await self.manager.handle_message(incoming(9432, "cancel me"))
        await self.manager.handle_message(incoming(9433, "run me"))
        await self.manager.auto_text(7001, "on")
        cancelled = await self.manager.cancel_text(7001, 2)
        self.assertIn("취소 완료", cancelled)

        self.db.set_session_status("thread-1", "IDLE")
        await self.manager.handle_completion("thread-1")
        await self.manager.wait_background_tasks()

        second = self.db.get_job_by_discord_message(9432)
        third = self.db.get_job_by_discord_message(9433)
        assert second is not None and third is not None
        self.assertEqual(second.status, "CANCELLED")
        self.assertEqual(third.status, "DISPATCHED")
        self.runner.queue_message.assert_awaited_with(
            "thread-1", "run me", cwd=self.workspace
        )

    async def test_restart_recovery_resumes_only_safe_idle_auto_queue(self) -> None:
        queued, _ = self.db.create_job("thread-1", 9441, "resume after restart")
        self.db.set_session_auto_run("thread-1", True)
        recovered_manager = JobManager(
            self.db, self.runner, self.discord, allowed_roots=(self.root,)
        )
        self.assertEqual(recovered_manager.recover(), 0)

        scheduled = await recovered_manager.resume_auto_run()
        self.assertEqual(scheduled, 1)
        await recovered_manager.wait_background_tasks()
        self.assertEqual(self.db.get_job(queued.id).status, "DISPATCHED")  # type: ignore[union-attr]
        self.assertEqual(await recovered_manager.resume_auto_run(), 0)
        self.assertEqual(self.runner.queue_message.await_count, 1)

    async def test_recovery_preserves_dispatched_job_without_resend(self) -> None:
        await self.manager.handle_message(incoming(9001))
        self.db.set_session_status("thread-1", "IDLE")
        new_manager = JobManager(
            self.db, self.runner, self.discord, allowed_roots=(self.root,)
        )
        recovered = new_manager.recover()
        self.assertEqual(recovered, 1)
        self.assertEqual(self.db.get_session("thread-1").status, "RUNNING")  # type: ignore[union-attr]
        self.assertEqual(self.runner.queue_message.await_count, 1)

    async def test_managed_mode_uses_exec_runner_and_waits_for_notify(self) -> None:
        self.db.set_session_mode("thread-1", "MANAGED")
        managed = AsyncMock()
        managed.resume.return_value = CodexManagedResult(0, "done", "", False, False)
        manager = JobManager(
            self.db,
            self.runner,
            self.discord,
            managed_runner=managed,
            allowed_roots=(self.root,),
        )

        await manager.handle_message(incoming(9101, "managed work"))

        job = self.db.get_job_by_discord_message(9101)
        assert job is not None
        self.assertEqual(job.status, "DISPATCHED")
        self.assertEqual(job.exit_code, 0)
        self.assertEqual(self.db.get_session("thread-1").status, "WAITING")  # type: ignore[union-attr]
        managed.resume.assert_awaited_once_with(
            "thread-1", "managed work", cwd=self.workspace
        )
        self.runner.queue_message.assert_not_awaited()

        completed = await manager.handle_completion("thread-1")
        assert completed is not None
        self.assertEqual(completed.status, "COMPLETED")

    async def test_managed_auto_run_dispatches_next_after_completion(self) -> None:
        self.db.set_session_mode("thread-1", "MANAGED")
        managed = AsyncMock()
        managed.resume.return_value = CodexManagedResult(0, "done", "", False, False)
        manager = JobManager(
            self.db,
            self.runner,
            self.discord,
            managed_runner=managed,
            allowed_roots=(self.root,),
        )
        await manager.handle_message(incoming(9451, "managed one"))
        await manager.handle_message(incoming(9452, "managed two"))
        await manager.auto_text(7001, "on")

        self.db.set_session_status("thread-1", "IDLE")
        await manager.handle_completion("thread-1")
        await manager.wait_background_tasks()

        second = self.db.get_job_by_discord_message(9452)
        assert second is not None
        self.assertEqual(second.status, "DISPATCHED")
        self.assertEqual(self.db.get_session("thread-1").status, "WAITING")  # type: ignore[union-attr]
        self.assertEqual(managed.resume.await_count, 2)

    async def test_mode_switch_rejects_pending_work(self) -> None:
        await self.manager.handle_message(incoming(9001, "first"))
        managed = AsyncMock()
        manager = JobManager(
            self.db,
            self.runner,
            self.discord,
            managed_runner=managed,
            allowed_roots=(self.root,),
        )
        result = await manager.mode_text(7001, "managed")
        self.assertIn("변경할 수 없습니다", result)
        self.assertEqual(self.db.get_session("thread-1").mode, "LIVE")  # type: ignore[union-attr]

    async def test_mode_switch_to_managed_requires_runner(self) -> None:
        result = await self.manager.mode_text(7001, "managed")
        self.assertIn("준비되지 않았습니다", result)

        managed = AsyncMock()
        manager = JobManager(
            self.db,
            self.runner,
            self.discord,
            managed_runner=managed,
            allowed_roots=(self.root,),
        )
        result = await manager.mode_text(7001, "managed")
        self.assertIn("Mode: MANAGED", result)
        self.assertEqual(self.db.get_session("thread-1").mode, "MANAGED")  # type: ignore[union-attr]

    async def test_shutdown_marks_active_managed_job_failed(self) -> None:
        self.db.set_session_mode("thread-1", "MANAGED")
        job, _ = self.db.create_job("thread-1", 9191, "managed work")
        self.db.begin_job_dispatch(job.id)
        managed = AsyncMock()
        managed.active_thread_ids = ("thread-1",)
        managed.close.return_value = ("thread-1",)
        manager = JobManager(
            self.db,
            self.runner,
            self.discord,
            managed_runner=managed,
            allowed_roots=(self.root,),
        )

        failed = await manager.shutdown()

        self.assertEqual(failed, 1)
        current = self.db.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "FAILED")
        self.assertIn("service shutdown", current.error or "")
        managed.close.assert_awaited_once()

    async def test_cancel_active_managed_job_terminates_process(self) -> None:
        self.db.set_session_mode("thread-1", "MANAGED")
        self.db.set_session_auto_run("thread-1", True)
        managed = AsyncMock()
        blocker = __import__("asyncio").Event()

        async def resume(*_args, **_kwargs):
            await blocker.wait()
            raise CodexManagedCancelledError("cancelled")

        async def cancel(_thread_id: str) -> bool:
            blocker.set()
            return True

        managed.resume.side_effect = resume
        managed.cancel.side_effect = cancel
        manager = JobManager(
            self.db,
            self.runner,
            self.discord,
            managed_runner=managed,
            allowed_roots=(self.root,),
        )
        task = __import__("asyncio").create_task(
            manager.handle_message(incoming(9201, "long managed work"))
        )
        await __import__("asyncio").sleep(0)
        job = self.db.get_job_by_discord_message(9201)
        assert job is not None
        self.assertEqual(job.status, "DISPATCHED")

        result = await manager.cancel_text(7001, job.id)
        self.assertIn("취소 완료", result)
        cancelled = self.db.get_job(job.id)
        assert cancelled is not None
        self.assertEqual(cancelled.status, "CANCELLED")
        self.assertFalse(self.db.get_session("thread-1").auto_run)  # type: ignore[union-attr]
        managed.cancel.assert_awaited_once_with("thread-1")

        await task


if __name__ == "__main__":
    unittest.main()
