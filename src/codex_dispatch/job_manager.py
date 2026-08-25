"""Persistent Discord job queue and per-session dispatch serialization."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import logging
from pathlib import Path
from typing import Protocol

from .adapters.discord import DiscordIncomingMessage
from .codex_runner import (
    CodexCapabilityError,
    CodexQueueRejectedError,
    CodexQueueRunner,
    CodexQueueTimeoutError,
    CodexRunnerError,
    CodexSessionUnavailableError,
    PromptValidationError,
    normalize_prompt,
)
from .database import Database, DatabaseBusyError, DatabaseInvariantError
from .managed_runner import (
    CodexManagedCancelledError,
    CodexManagedError,
    CodexManagedRunner,
    CodexManagedTimeoutError,
)
from .models import JobRecord, SessionRecord
from .security import WorkspaceSecurityError, resolve_allowed_workspace


LOGGER = logging.getLogger(__name__)
_QUEUE_DISPLAY_LIMIT = 10
_PREVIEW_LIMIT = 80


class DiscordJobFeedback(Protocol):
    async def send_text(
        self,
        channel_id: int,
        content: str,
        *,
        nonce: int | None = None,
    ) -> int: ...


class JobManager:
    """Persist inbound prompts and allow at most one dispatched job per session.

    Queued jobs are persistent and serialized per Codex thread. PHASE 9 adds an
    opt-in auto-run flag; failures always pause it before another job can run.
    """

    def __init__(
        self,
        database: Database,
        runner: CodexQueueRunner,
        discord: DiscordJobFeedback,
        *,
        managed_runner: CodexManagedRunner | None = None,
        prompt_max_chars: int = 8_000,
        allowed_roots: Sequence[Path],
    ) -> None:
        if prompt_max_chars <= 0:
            raise ValueError("prompt_max_chars must be positive")
        self._database = database
        self._runner = runner
        self._managed_runner = managed_runner
        self._discord = discord
        self._prompt_max_chars = prompt_max_chars
        if not allowed_roots:
            raise ValueError("allowed_roots must not be empty")
        self._allowed_roots = tuple(Path(root) for root in allowed_roots)
        self._shutting_down = False
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def _session_lock(self, thread_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[thread_id] = lock
            return lock

    async def _feedback(
        self,
        channel_id: int,
        text: str,
        *,
        nonce: int | None = None,
    ) -> None:
        try:
            await self._discord.send_text(channel_id, text, nonce=nonce)
        except Exception as exc:
            LOGGER.warning(
                "discord.job_feedback_failed channel_id=%s error_type=%s",
                channel_id,
                type(exc).__name__,
            )

    def _resolve_session(self, discord_thread_id: int) -> SessionRecord | None:
        return self._database.get_session_by_discord_thread(discord_thread_id)

    def _track_background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def done(completed: asyncio.Task[None]) -> None:
            self._background_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                completed.result()
            except Exception:
                LOGGER.exception("job.background_dispatch_failed")

        task.add_done_callback(done)

    async def wait_background_tasks(self) -> None:
        """Wait for currently scheduled auto-dispatch tasks; useful for shutdown/tests."""

        tasks = tuple(self._background_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _prepare_auto_dispatch(
        self,
        session: SessionRecord,
    ) -> tuple[JobRecord, str, object, int] | None:
        """Claim the next queued job when the persisted auto-run state is safe."""

        if self._shutting_down or not session.auto_run or session.status != "IDLE":
            return None
        if self._database.get_dispatched_job(session.thread_id) is not None:
            return None
        next_job = self._database.get_next_queued_job(session.thread_id)
        if next_job is None:
            return None
        if session.discord_thread_id is None:
            self._database.set_session_auto_run(session.thread_id, False)
            return None
        if session.mode == "MANAGED" and self._managed_runner is None:
            self._database.set_session_auto_run(session.thread_id, False)
            self._database.set_session_status(session.thread_id, "ERROR")
            return None
        workspace = self._database.get_workspace(session.workspace_id)
        if workspace is None or not workspace.enabled:
            self._database.set_session_auto_run(session.thread_id, False)
            self._database.set_session_status(session.thread_id, "ERROR")
            return None
        claimed = self._database.begin_job_dispatch(next_job.id)
        return claimed, session.mode, workspace.path, session.discord_thread_id

    async def handle_message(self, message: DiscordIncomingMessage) -> None:
        """Persist and dispatch one prompt while containing transient SQLite lock errors."""

        try:
            await self._handle_message(message)
        except DatabaseBusyError:
            LOGGER.warning(
                "job.database_busy message_id=%s channel_id=%s",
                message.message_id,
                message.channel_id,
            )
            await self._feedback(
                message.channel_id,
                "⚠️ 상태 데이터베이스가 일시적으로 사용 중입니다. 잠시 후 같은 요청을 다시 보내 주세요.",
                nonce=message.message_id,
            )

    async def _handle_message(self, message: DiscordIncomingMessage) -> None:
        """Persist one authorized Discord prompt and dispatch it when session is idle."""

        session = self._resolve_session(message.channel_id)
        if session is None:
            await self._feedback(
                message.channel_id,
                "⚠️ 이 Discord Thread에 연결된 Codex 세션이 없습니다.",
                nonce=message.message_id,
            )
            return
        if session.mode not in {"LIVE", "MANAGED"}:
            await self._feedback(
                message.channel_id,
                "⚠️ 지원하지 않는 Codex 세션 모드입니다.",
                nonce=message.message_id,
            )
            return
        if session.mode == "MANAGED" and self._managed_runner is None:
            await self._feedback(
                message.channel_id,
                "⚠️ Managed Codex 실행기가 준비되지 않았습니다.",
                nonce=message.message_id,
            )
            return

        workspace = self._database.get_workspace(session.workspace_id)
        if workspace is None or not workspace.enabled:
            self._database.set_session_status(session.thread_id, "ERROR")
            await self._feedback(
                message.channel_id,
                "⚠️ 연결된 workspace를 사용할 수 없습니다.",
                nonce=message.message_id,
            )
            return

        try:
            prompt = normalize_prompt(message.content, max_chars=self._prompt_max_chars)
        except PromptValidationError as exc:
            await self._feedback(
                message.channel_id,
                f"⚠️ 프롬프트를 접수할 수 없습니다: {exc}",
                nonce=message.message_id,
            )
            return

        job, created = self._database.create_job(
            session.thread_id,
            message.message_id,
            prompt,
        )
        if not created:
            LOGGER.info(
                "job.duplicate_discord_message message_id=%s job_id=%s status=%s",
                message.message_id,
                job.id,
                job.status,
            )
            await self._feedback(
                message.channel_id,
                f"ℹ️ 이미 접수된 Job #{job.id}입니다. 현재 상태: {job.status}",
                nonce=message.message_id,
            )
            return

        lock = await self._session_lock(session.thread_id)
        async with lock:
            refreshed = self._database.get_session(session.thread_id)
            assert refreshed is not None
            active = self._database.get_dispatched_job(session.thread_id)
            if active is not None or refreshed.status in {"RUNNING", "WAITING"}:
                LOGGER.info(
                    "job.queued job_id=%s thread_id=%s reason=session_busy",
                    job.id,
                    session.thread_id,
                )
                await self._feedback(
                    message.channel_id,
                    f"📥 Job #{job.id} 대기열에 저장했습니다. 현재 Codex 작업 완료 후 검토할 수 있습니다.",
                    nonce=message.message_id,
                )
                return
            claimed = self._database.begin_job_dispatch(job.id)
            mode = refreshed.mode

        await self._dispatch_claimed(claimed, mode, workspace.path, message.channel_id)

    async def _dispatch_claimed(
        self,
        job: JobRecord,
        mode: str,
        cwd,
        channel_id: int,
    ) -> None:
        """Execute a claimed job after re-validating its workspace at execution time."""

        try:
            secure_cwd = resolve_allowed_workspace(cwd, self._allowed_roots)
        except WorkspaceSecurityError as exc:
            current = self._database.get_job(job.id)
            if current is not None and current.status == "DISPATCHED":
                self._database.mark_job_failed(job.id, str(exc), session_status="ERROR")
            await self._feedback(
                channel_id,
                f"⚠️ Job #{job.id}: workspace 보안 검증에 실패했습니다.",
            )
            LOGGER.warning(
                "job.workspace_rejected job_id=%s thread_id=%s",
                job.id,
                job.thread_id,
            )
            return

        if mode == "MANAGED":
            await self._dispatch_managed(job, secure_cwd, channel_id)
            return
        await self._dispatch_live(job, secure_cwd, channel_id)

    async def _dispatch_live(self, job: JobRecord, cwd, channel_id: int) -> None:
        try:
            result = await self._runner.queue_message(job.thread_id, job.prompt, cwd=cwd)
        except CodexSessionUnavailableError as exc:
            self._database.mark_job_failed(job.id, str(exc), session_status="OFFLINE")
            await self._feedback(
                channel_id,
                f"⚠️ Job #{job.id}: Codex live 세션을 찾을 수 없거나 사용할 수 없습니다.",
            )
            return
        except CodexQueueTimeoutError as exc:
            self._database.mark_job_failed(job.id, str(exc), session_status="ERROR")
            await self._feedback(
                channel_id,
                f"⚠️ Job #{job.id}: Codex queue 명령이 시간 안에 완료되지 않았습니다.",
            )
            return
        except CodexCapabilityError as exc:
            self._database.mark_job_failed(job.id, str(exc), session_status="ERROR")
            await self._feedback(
                channel_id,
                f"⚠️ Job #{job.id}: 설치된 Codex CLI가 queue 기능을 지원하지 않습니다.",
            )
            return
        except (CodexQueueRejectedError, CodexRunnerError) as exc:
            self._database.mark_job_failed(job.id, str(exc), session_status="ERROR")
            await self._feedback(
                channel_id,
                f"⚠️ Job #{job.id}: Codex가 프롬프트를 접수하지 못했습니다.",
            )
            return

        self._database.mark_job_queue_accepted(job.id, result.returncode)
        LOGGER.info(
            "job.dispatched job_id=%s thread_id=%s mode=LIVE prompt_length=%s",
            job.id,
            job.thread_id,
            len(job.prompt),
        )
        await self._feedback(channel_id, f"▶️ Job #{job.id} Codex에 전달했습니다.")

    async def _dispatch_managed(self, job: JobRecord, cwd, channel_id: int) -> None:
        runner = self._managed_runner
        if runner is None:
            self._database.mark_job_failed(
                job.id,
                "managed runner is unavailable",
                session_status="ERROR",
            )
            return
        try:
            result = await runner.resume(job.thread_id, job.prompt, cwd=cwd)
        except CodexManagedCancelledError as exc:
            current = self._database.get_job(job.id)
            if current is not None and current.status == "DISPATCHED":
                if self._shutting_down:
                    self._database.mark_job_failed(
                        job.id,
                        "service shutdown during managed execution",
                        session_status="ERROR",
                    )
                else:
                    self._database.cancel_dispatched_job(job.thread_id, job.id)
            if not self._shutting_down:
                await self._feedback(channel_id, f"🛑 Job #{job.id} Managed 실행을 취소했습니다.")
            return
        except CodexSessionUnavailableError as exc:
            self._database.mark_job_failed(job.id, str(exc), session_status="OFFLINE")
            await self._feedback(
                channel_id,
                f"⚠️ Job #{job.id}: 기존 Codex thread를 resume할 수 없습니다.",
            )
            return
        except CodexManagedTimeoutError as exc:
            self._database.mark_job_failed(job.id, str(exc), session_status="ERROR")
            await self._feedback(
                channel_id,
                f"⚠️ Job #{job.id}: Managed Codex 실행 제한 시간을 초과했습니다.",
            )
            return
        except (CodexManagedError, CodexCapabilityError, CodexRunnerError) as exc:
            self._database.mark_job_failed(job.id, str(exc), session_status="ERROR")
            await self._feedback(
                channel_id,
                f"⚠️ Job #{job.id}: Managed Codex 실행에 실패했습니다.",
            )
            return

        self._database.mark_job_queue_accepted(job.id, result.returncode)
        current = self._database.get_job(job.id)
        if current is not None and current.status == "DISPATCHED":
            self._database.set_session_status(job.thread_id, "WAITING")
        LOGGER.info(
            "job.managed_process_finished job_id=%s thread_id=%s prompt_length=%s",
            job.id,
            job.thread_id,
            len(job.prompt),
        )
        await self._feedback(
            channel_id,
            f"⏳ Job #{job.id} Managed 실행 프로세스가 종료되었습니다. 완료 notify를 확인 중입니다.",
        )

    async def handle_completion(self, thread_id: str) -> JobRecord | None:
        """Complete one job and schedule the next queued job when auto-run is enabled."""

        prepared = None
        lock = await self._session_lock(thread_id)
        async with lock:
            completed = self._database.complete_dispatched_job(thread_id)
            if completed is not None:
                queued = self._database.list_jobs(
                    thread_id,
                    statuses=("QUEUED",),
                    limit=1000,
                )
                LOGGER.info(
                    "job.completed job_id=%s thread_id=%s queued_remaining=%s",
                    completed.id,
                    thread_id,
                    len(queued),
                )
                session = self._database.get_session(thread_id)
                if session is not None:
                    prepared = self._prepare_auto_dispatch(session)
        if prepared is not None:
            job, mode, cwd, channel_id = prepared
            LOGGER.info(
                "job.auto_dispatch_scheduled job_id=%s thread_id=%s mode=%s",
                job.id,
                job.thread_id,
                mode,
            )
            self._track_background(self._dispatch_claimed(job, mode, cwd, channel_id))
        return completed

    def recover(self) -> int:
        """Recover LIVE ambiguity and fail orphaned MANAGED subprocess work."""

        count = self._database.recover_job_session_state()
        if count:
            LOGGER.warning("job.recovered_dispatched_sessions count=%s", count)
        return count

    async def resume_auto_run(self) -> int:
        """Resume safe idle auto-run queues after Discord becomes ready."""

        scheduled = 0
        for candidate in self._database.list_auto_run_sessions():
            lock = await self._session_lock(candidate.thread_id)
            prepared = None
            async with lock:
                current = self._database.get_session(candidate.thread_id)
                if current is not None:
                    prepared = self._prepare_auto_dispatch(current)
            if prepared is None:
                continue
            job, mode, cwd, channel_id = prepared
            self._track_background(self._dispatch_claimed(job, mode, cwd, channel_id))
            scheduled += 1
            LOGGER.info(
                "job.auto_run_recovered job_id=%s thread_id=%s mode=%s",
                job.id,
                job.thread_id,
                mode,
            )
        return scheduled

    async def shutdown(self) -> int:
        """Terminate managed subprocesses and persist an explicit failed state."""

        runner = self._managed_runner
        self._shutting_down = True
        managed_jobs = tuple(
            job
            for session in self._database.list_sessions()
            if session.mode == "MANAGED"
            for job in (self._database.get_dispatched_job(session.thread_id),)
            if job is not None
        )
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if runner is not None:
            await runner.close()
        failed = 0
        for job in managed_jobs:
            current = self._database.get_job(job.id)
            if current is None or current.status != "DISPATCHED":
                continue
            try:
                self._database.mark_job_failed(
                    job.id,
                    "service shutdown during managed execution",
                    session_status="ERROR",
                )
                failed += 1
            except DatabaseInvariantError:
                pass
        return failed

    def status_text(self, discord_thread_id: int) -> str:
        session = self._resolve_session(discord_thread_id)
        if session is None:
            return "⚠️ 이 Discord Thread에 연결된 Codex 세션이 없습니다."
        active = self._database.get_dispatched_job(session.thread_id)
        queued = self._database.list_jobs(session.thread_id, statuses=("QUEUED",), limit=1000)
        active_text = f"Job #{active.id}" if active is not None else "없음"
        return (
            f"Session: {session.thread_id[:8]}\n"
            f"Mode: {session.mode}\n"
            f"Status: {session.status}\n"
            f"Auto Run: {'ON' if session.auto_run else 'OFF'}\n"
            f"Active: {active_text}\n"
            f"Queued: {len(queued)}"
        )

    def queue_text(self, discord_thread_id: int) -> str:
        session = self._resolve_session(discord_thread_id)
        if session is None:
            return "⚠️ 이 Discord Thread에 연결된 Codex 세션이 없습니다."
        jobs = self._database.list_jobs(
            session.thread_id,
            statuses=("QUEUED", "DISPATCHED"),
            limit=_QUEUE_DISPLAY_LIMIT,
        )
        if not jobs:
            return "대기 중이거나 실행 중인 Job이 없습니다."
        lines = ["현재 Job 대기열:"]
        for job in jobs:
            preview = " ".join(job.prompt.split())[:_PREVIEW_LIMIT]
            lines.append(f"#{job.id} [{job.status}] {preview}")
        return "\n".join(lines)

    async def cancel_text(self, discord_thread_id: int, job_id: int) -> str:
        session = self._resolve_session(discord_thread_id)
        if session is None:
            return "⚠️ 이 Discord Thread에 연결된 Codex 세션이 없습니다."
        job = self._database.get_job(job_id)
        if job is None or job.thread_id != session.thread_id:
            return f"⚠️ Job #{job_id}을 취소할 수 없습니다: job does not belong to this session"
        if job.status == "QUEUED":
            try:
                cancelled = self._database.cancel_queued_job(session.thread_id, job_id)
            except DatabaseInvariantError as exc:
                return f"⚠️ Job #{job_id}을 취소할 수 없습니다: {exc}"
            LOGGER.info("job.cancelled job_id=%s thread_id=%s", cancelled.id, cancelled.thread_id)
            return f"🛑 Job #{cancelled.id} 취소 완료"
        if job.status == "DISPATCHED" and session.mode == "MANAGED":
            runner = self._managed_runner
            if runner is None or not await runner.cancel(session.thread_id):
                return f"⚠️ Job #{job_id} Managed 프로세스를 찾을 수 없습니다."
            try:
                cancelled = self._database.cancel_dispatched_job(session.thread_id, job_id)
            except DatabaseInvariantError:
                current = self._database.get_job(job_id)
                if current is not None and current.status == "CANCELLED":
                    return f"🛑 Job #{job_id} 취소 완료"
                return f"⚠️ Job #{job_id} 상태가 이미 변경되어 취소를 확정할 수 없습니다."
            LOGGER.info("job.managed_cancelled job_id=%s thread_id=%s", job_id, session.thread_id)
            return f"🛑 Job #{cancelled.id} Managed 실행 취소 완료"
        return f"⚠️ Job #{job_id}은 현재 취소할 수 없습니다."

    async def auto_text(self, discord_thread_id: int, state: str) -> str:
        session = self._resolve_session(discord_thread_id)
        if session is None:
            return "⚠️ 이 Discord Thread에 연결된 Codex 세션이 없습니다."

        normalized = state.strip().lower()
        if normalized not in {"on", "off"}:
            return "⚠️ state는 on 또는 off여야 합니다."

        prepared = None
        lock = await self._session_lock(session.thread_id)
        async with lock:
            current = self._database.get_session(session.thread_id)
            assert current is not None
            if normalized == "off":
                self._database.set_session_auto_run(session.thread_id, False)
                LOGGER.info("session.auto_run_changed thread_id=%s enabled=false", session.thread_id)
                return "✅ Auto Run: OFF\n현재 실행 중인 Job은 중단하지 않고 후속 자동 실행만 멈춥니다."

            if current.status in {"ERROR", "OFFLINE"}:
                return (
                    "⚠️ 현재 session 상태가 " + current.status +
                    "이므로 Auto Run을 활성화할 수 없습니다. 먼저 session 상태를 복구하세요."
                )
            if current.mode == "MANAGED" and self._managed_runner is None:
                return "⚠️ Managed Codex 실행기가 준비되지 않았습니다."
            updated = self._database.set_session_auto_run(session.thread_id, True)
            prepared = self._prepare_auto_dispatch(updated)

        LOGGER.info("session.auto_run_changed thread_id=%s enabled=true", session.thread_id)
        if prepared is not None:
            job, mode, cwd, channel_id = prepared
            self._track_background(self._dispatch_claimed(job, mode, cwd, channel_id))
            return f"✅ Auto Run: ON\n▶️ 대기 중인 Job #{job.id}을 자동 실행합니다."
        return "✅ Auto Run: ON\n현재 작업 완료 후 다음 QUEUED Job을 자동 실행합니다."

    async def mode_text(self, discord_thread_id: int, mode: str) -> str:
        session = self._resolve_session(discord_thread_id)
        if session is None:
            return "⚠️ 이 Discord Thread에 연결된 Codex 세션이 없습니다."
        normalized = mode.strip().upper()
        if normalized not in {"LIVE", "MANAGED"}:
            return "⚠️ mode는 live 또는 managed여야 합니다."
        lock = await self._session_lock(session.thread_id)
        async with lock:
            if normalized == "MANAGED" and self._managed_runner is None:
                return "⚠️ Managed Codex 실행기가 준비되지 않았습니다."
            try:
                updated = self._database.set_session_mode(session.thread_id, normalized)
            except DatabaseInvariantError as exc:
                return f"⚠️ mode를 변경할 수 없습니다: {exc}"
        LOGGER.info("session.mode_changed thread_id=%s mode=%s", session.thread_id, normalized)
        if updated.mode == "MANAGED":
            return (
                "✅ Mode: MANAGED\n"
                "이제 Discord prompt는 codex exec resume으로 실행됩니다. "
                "같은 thread의 interactive LIVE TUI는 동시에 사용하지 마세요."
            )
        return "✅ Mode: LIVE\n이제 Discord prompt는 codex queue로 전달됩니다."
