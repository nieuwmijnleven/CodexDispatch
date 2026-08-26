from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_dispatch.adapters.discord import DiscordIncomingMessage
from codex_dispatch.codex_runner import (
    CodexQueueRejectedError,
    CodexQueueResult,
    CodexSessionUnavailableError,
)
from codex_dispatch.completion_notifier import CompletionNotifier
from codex_dispatch.database import Database
from codex_dispatch.job_manager import JobManager
from codex_dispatch.managed_runner import CodexManagedResult
from codex_dispatch.notify_client import send_payload
from codex_dispatch.notify_server import NotifyEvent, NotifyValidationError, parse_notify_payload
from codex_dispatch.session_manager import SessionManager


class FakeDiscord:
    def __init__(self) -> None:
        self.next_thread_id = 7000
        self.next_message_id = 90000
        self.threads: dict[int, tuple[int, str]] = {}
        self.sent: list[tuple[int, str, int | None, int]] = []
        self.failures_remaining = 0

    async def create_thread(self, parent_channel_id: int, name: str) -> int:
        self.next_thread_id += 1
        self.threads[self.next_thread_id] = (parent_channel_id, name)
        return self.next_thread_id

    async def send_text(
        self,
        channel_id: int,
        content: str,
        *,
        nonce: int | None = None,
        mention_allowed_users: bool = False,
    ) -> int:
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise RuntimeError("simulated Discord API failure")
        self.next_message_id += 1
        self.sent.append((channel_id, content, nonce, self.next_message_id))
        return self.next_message_id


class FakeLiveRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Path]] = []
        self.failures: dict[str, list[Exception]] = {}

    def fail_next(self, thread_id: str, exc: Exception) -> None:
        self.failures.setdefault(thread_id, []).append(exc)

    async def queue_message(self, thread_id: str, prompt: str, *, cwd: Path) -> CodexQueueResult:
        self.calls.append((thread_id, prompt, cwd))
        pending = self.failures.get(thread_id)
        if pending:
            raise pending.pop(0)
        return CodexQueueResult(0, "queued", "")


class FakeManagedRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Path]] = []
        self.active_thread_ids: tuple[str, ...] = ()

    async def resume(self, thread_id: str, prompt: str, *, cwd: Path) -> CodexManagedResult:
        self.calls.append((thread_id, prompt, cwd))
        return CodexManagedResult(0, "done", "", False, False)

    async def cancel(self, _thread_id: str) -> bool:
        return False

    async def close(self) -> tuple[str, ...]:
        return ()


class EndToEndRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.root = Path(self.temp.name)
        self.workspace_root = self.root / "workspaces"
        self.workspace_a = self.workspace_root / "project-a"
        self.workspace_b = self.workspace_root / "project-b"
        self.workspace_a.mkdir(parents=True)
        self.workspace_b.mkdir(parents=True)
        self.db_path = self.root / "state" / "codex-dispatch.db"
        self.db = Database(self.db_path)
        self.db.open()
        self.discord = FakeDiscord()
        self.live = FakeLiveRunner()
        self.managed = FakeManagedRunner()
        self.sessions = SessionManager(self.db, self.discord, 6000)
        self.jobs = JobManager(
            self.db,
            self.live,
            self.discord,
            managed_runner=self.managed,
            allowed_roots=(self.workspace_root,),
        )

        async def no_sleep(_delay: float) -> None:
            return None

        self.completions = CompletionNotifier(
            self.db,
            self.discord,
            retry_attempts=3,
            retry_base_seconds=0.001,
            sleep=no_sleep,
        )

    async def _cleanup(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def event(
        self,
        thread_id: str,
        turn_id: str,
        cwd: Path,
        result: str = "done",
    ) -> NotifyEvent:
        return NotifyEvent(
            event_type="agent-turn-complete",
            thread_id=thread_id,
            turn_id=turn_id,
            cwd=cwd,
            client="codex-tui",
            input_messages=("work",),
            last_assistant_message=result,
        )

    async def finish_turn(
        self,
        thread_id: str,
        turn_id: str,
        cwd: Path,
        result: str = "done",
    ):
        current = self.event(thread_id, turn_id, cwd, result)
        registration = await self.sessions.handle_notify(current)
        await self.jobs.handle_completion(thread_id)
        await self.completions.deliver(current, registration)
        await self.jobs.wait_background_tasks()
        return registration

    @staticmethod
    def incoming(message_id: int, channel_id: int, content: str) -> DiscordIncomingMessage:
        return DiscordIncomingMessage(
            message_id=message_id,
            guild_id=100,
            channel_id=channel_id,
            parent_channel_id=6000,
            user_id=300,
            content=content,
        )

    async def test_live_sessions_same_workspace_and_multiple_workspaces_end_to_end(self) -> None:
        one = await self.finish_turn("live-a1", "turn-1", self.workspace_a, "initial a1")
        two = await self.finish_turn("live-a2", "turn-1", self.workspace_a, "initial a2")
        three = await self.finish_turn("live-b1", "turn-1", self.workspace_b, "initial b1")

        channels = {
            one.session.discord_thread_id,
            two.session.discord_thread_id,
            three.session.discord_thread_id,
        }
        self.assertNotIn(None, channels)
        self.assertEqual(len(channels), 3)
        self.assertEqual(len(self.db.list_sessions()), 3)
        workspace_ids = {session.workspace_id for session in self.db.list_sessions()}
        self.assertEqual(len(workspace_ids), 2)

        registrations = (one, two, three)
        for index, registration in enumerate(registrations, start=1):
            assert registration.session.discord_thread_id is not None
            await self.jobs.handle_message(
                self.incoming(1000 + index, registration.session.discord_thread_id, f"job-{index}")
            )

        self.assertEqual(len(self.live.calls), 3)
        self.assertEqual({call[0] for call in self.live.calls}, {"live-a1", "live-a2", "live-b1"})

        await self.finish_turn("live-a1", "turn-2", self.workspace_a, "a1 complete")
        await self.finish_turn("live-a2", "turn-2", self.workspace_a, "a2 complete")
        await self.finish_turn("live-b1", "turn-2", self.workspace_b, "b1 complete")

        jobs = [job for session in self.db.list_sessions() for job in self.db.list_jobs(session.thread_id)]
        self.assertEqual(len(jobs), 3)
        self.assertTrue(all(job.status == "COMPLETED" for job in jobs))
        self.assertTrue(all(session.status == "IDLE" for session in self.db.list_sessions()))

    async def test_managed_queue_auto_run_and_fast_input_end_to_end(self) -> None:
        registration = await self.finish_turn("managed-1", "turn-1", self.workspace_a)
        channel = registration.session.discord_thread_id
        assert channel is not None
        self.db.set_session_mode("managed-1", "MANAGED")

        await asyncio.gather(
            self.jobs.handle_message(self.incoming(2001, channel, "managed-one")),
            self.jobs.handle_message(self.incoming(2002, channel, "managed-two")),
            self.jobs.handle_message(self.incoming(2003, channel, "managed-three")),
        )
        await self.jobs.auto_text(channel, "on")

        current_jobs = self.db.list_jobs("managed-1")
        self.assertEqual(sum(job.status == "DISPATCHED" for job in current_jobs), 1)
        self.assertEqual(sum(job.status == "QUEUED" for job in current_jobs), 2)

        await self.finish_turn("managed-1", "turn-2", self.workspace_a, "one done")
        self.assertEqual(len(self.managed.calls), 2)
        await self.finish_turn("managed-1", "turn-3", self.workspace_a, "two done")
        self.assertEqual(len(self.managed.calls), 3)
        await self.finish_turn("managed-1", "turn-4", self.workspace_a, "three done")

        final_jobs = self.db.list_jobs("managed-1")
        self.assertEqual(len(final_jobs), 3)
        self.assertTrue(all(job.status == "COMPLETED" for job in final_jobs))
        session = self.db.get_session("managed-1")
        assert session is not None
        self.assertEqual(session.status, "IDLE")
        self.assertTrue(session.auto_run)

    async def test_restart_duplicate_events_and_reconnect_do_not_redispatch(self) -> None:
        registration = await self.finish_turn("restart-1", "turn-1", self.workspace_a)
        channel = registration.session.discord_thread_id
        assert channel is not None
        await self.jobs.handle_message(self.incoming(3001, channel, "long-running-live"))
        await self.jobs.handle_message(self.incoming(3001, channel, "long-running-live"))
        self.assertEqual(self.db.job_count(), 1)
        self.assertEqual(len(self.live.calls), 1)

        original_mapping = self.db.get_session("restart-1")
        assert original_mapping is not None
        original_discord_thread = original_mapping.discord_thread_id
        self.db.close()

        self.db = Database(self.db_path)
        self.db.open()
        restarted_live = FakeLiveRunner()
        self.live = restarted_live
        self.sessions = SessionManager(self.db, self.discord, 6000)
        self.jobs = JobManager(
            self.db,
            restarted_live,
            self.discord,
            managed_runner=self.managed,
            allowed_roots=(self.workspace_root,),
        )

        async def no_sleep(_delay: float) -> None:
            return None

        self.completions = CompletionNotifier(
            self.db,
            self.discord,
            retry_attempts=3,
            retry_base_seconds=0.001,
            sleep=no_sleep,
        )
        self.assertEqual(self.jobs.recover(), 1)
        self.assertEqual(len(restarted_live.calls), 0)
        recovered = self.db.get_session("restart-1")
        assert recovered is not None
        self.assertEqual(recovered.discord_thread_id, original_discord_thread)
        self.assertEqual(recovered.status, "RUNNING")

        before = len(self.discord.sent)
        await self.finish_turn("restart-1", "turn-2", self.workspace_a, "completed after restart")
        after_first = len(self.discord.sent)
        await self.finish_turn("restart-1", "turn-2", self.workspace_a, "completed after restart")
        self.assertGreater(after_first, before)
        self.assertEqual(len(self.discord.sent), after_first)
        self.assertEqual(len(restarted_live.calls), 0)
        self.assertEqual(self.db.get_job_by_discord_message(3001).status, "COMPLETED")  # type: ignore[union-attr]

        self.db.set_session_auto_run("restart-1", True)
        self.db.create_job("restart-1", 3002, "queued-after-reconnect")
        scheduled_first = await self.jobs.resume_auto_run()
        scheduled_second = await self.jobs.resume_auto_run()
        await self.jobs.wait_background_tasks()
        self.assertEqual(scheduled_first, 1)
        self.assertEqual(scheduled_second, 0)
        self.assertEqual(len(restarted_live.calls), 1)

    async def test_failure_surfaces_and_discord_retry_are_contained(self) -> None:
        offline = await self.finish_turn("offline-1", "turn-1", self.workspace_a)
        offline_channel = offline.session.discord_thread_id
        assert offline_channel is not None
        self.live.fail_next("offline-1", CodexSessionUnavailableError("thread not found"))
        await self.jobs.handle_message(self.incoming(4001, offline_channel, "offline request"))
        offline_job = self.db.get_job_by_discord_message(4001)
        assert offline_job is not None
        self.assertEqual(offline_job.status, "FAILED")
        self.assertEqual(self.db.get_session("offline-1").status, "OFFLINE")  # type: ignore[union-attr]

        rejected = await self.finish_turn("reject-1", "turn-1", self.workspace_a)
        reject_channel = rejected.session.discord_thread_id
        assert reject_channel is not None
        self.live.fail_next("reject-1", CodexQueueRejectedError("exit code 7"))
        await self.jobs.handle_message(self.incoming(4002, reject_channel, "rejected request"))
        rejected_job = self.db.get_job_by_discord_message(4002)
        assert rejected_job is not None
        self.assertEqual(rejected_job.status, "FAILED")
        self.assertEqual(self.db.get_session("reject-1").status, "ERROR")  # type: ignore[union-attr]

        event = self.event("discord-retry", "turn-1", self.workspace_b, "retry delivery")
        registration = await self.sessions.handle_notify(event)
        self.discord.failures_remaining = 2
        delivered = await self.completions.deliver(event, registration)
        self.assertEqual(delivered.status, "SENT")
        self.assertEqual(delivered.attempts, 3)

    async def test_sqlite_busy_malformed_notify_and_socket_unavailable_are_contained(self) -> None:
        registration = await self.finish_turn("busy-1", "turn-1", self.workspace_a)
        channel = registration.session.discord_thread_id
        assert channel is not None
        assert self.db._connection is not None
        self.db._connection.execute("PRAGMA busy_timeout = 25")
        blocker = sqlite3.connect(self.db_path, timeout=0.1, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            before_calls = len(self.live.calls)
            await self.jobs.handle_message(self.incoming(5001, channel, "busy request"))
            self.assertEqual(len(self.live.calls), before_calls)
        finally:
            blocker.rollback()
            blocker.close()
        self.assertIsNone(self.db.get_job_by_discord_message(5001))
        self.assertTrue(any("데이터베이스" in content for _, content, _, _ in self.discord.sent))

        malformed = {
            "type": "agent-turn-complete",
            "thread-id": "bad-thread",
            "turn-id": "bad-turn",
            "cwd": "../../etc",
        }
        with self.assertRaises(NotifyValidationError):
            parse_notify_payload(malformed, (self.workspace_root,))

        missing_socket = self.root / "missing-runtime" / "notify.sock"
        raw = '{"type":"agent-turn-complete"}'
        with self.assertRaises((FileNotFoundError, ConnectionRefusedError, OSError)):
            send_payload(
                raw,
                environ={
                    "CODEX_DISPATCH_NOTIFY_SOCKET": str(missing_socket),
                    "CODEX_DISPATCH_NOTIFY_TIMEOUT_SECONDS": "0.05",
                },
            )


if __name__ == "__main__":
    unittest.main()
