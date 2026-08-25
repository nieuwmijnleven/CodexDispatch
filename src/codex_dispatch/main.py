"""Codex Dispatch process entrypoint."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import signal
import sys

from .adapters.discord import (
    DiscordAdapter,
    DiscordAdapterError,
    DiscordCommandRequest,
    DiscordIncomingMessage,
)
from .codex_runner import CodexQueueRunner, CodexRunnerError
from .completion_notifier import CompletionNotifier
from .config import Settings, SettingsError, load_settings
from .database import Database, DatabaseError
from .job_manager import JobManager
from .logging import configure_logging
from .managed_runner import CodexManagedRunner
from .notify_server import NotifyEvent, NotifyServer, NotifyServerError
from .security import validate_runtime_security
from .session_manager import SessionManager


LOGGER = logging.getLogger(__name__)


async def _run_discord_until_shutdown(
    adapter: DiscordAdapter,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Run Discord until it exits or SIGTERM/SIGINT requests graceful shutdown."""

    event = shutdown_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    if shutdown_event is None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, event.set)
            except (NotImplementedError, RuntimeError):
                continue
            installed.append(signum)

    adapter_task = asyncio.create_task(adapter.start(), name="discord-adapter")
    shutdown_task = asyncio.create_task(event.wait(), name="service-shutdown")
    try:
        done, _ = await asyncio.wait(
            {adapter_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if shutdown_task in done and not adapter_task.done():
            LOGGER.info("service.shutdown_requested")
            await adapter.close()
        await adapter_task
    finally:
        shutdown_task.cancel()
        await asyncio.gather(shutdown_task, return_exceptions=True)
        for signum in installed:
            loop.remove_signal_handler(signum)


async def run_service(
    settings: Settings,
    *,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Run the Discord adapter and local Codex notify receiver together."""

    settings.require_discord()
    settings.require_notify()
    database = Database(settings.database_path)
    database.open()

    job_manager: JobManager | None = None

    async def handle_discord_message(message: DiscordIncomingMessage) -> None:
        assert job_manager is not None
        await job_manager.handle_message(message)

    async def handle_discord_command(request: DiscordCommandRequest) -> str:
        assert job_manager is not None
        if request.name == "status":
            return job_manager.status_text(request.channel_id)
        if request.name == "queue":
            return job_manager.queue_text(request.channel_id)
        if request.name == "cancel":
            if request.job_id is None or request.job_id <= 0:
                return "⚠️ 유효한 Job ID가 필요합니다."
            return await job_manager.cancel_text(request.channel_id, request.job_id)
        if request.name == "mode":
            if request.mode is None:
                return "⚠️ mode가 필요합니다."
            return await job_manager.mode_text(request.channel_id, request.mode)
        if request.name == "auto":
            if request.auto_state is None:
                return "⚠️ state가 필요합니다."
            return await job_manager.auto_text(request.channel_id, request.auto_state)
        return "⚠️ 지원하지 않는 명령입니다."

    async def handle_discord_ready() -> None:
        assert job_manager is not None
        scheduled = await job_manager.resume_auto_run()
        if scheduled:
            LOGGER.info("job.auto_run_recovery_scheduled count=%s", scheduled)

    adapter = DiscordAdapter(
        settings,
        handle_discord_message,
        handle_discord_command,
        handle_discord_ready,
    )
    runner = CodexQueueRunner(
        settings.codex_binary,
        queue_timeout_seconds=settings.codex_queue_timeout_seconds,
        capability_timeout_seconds=settings.codex_capability_timeout_seconds,
        prompt_max_chars=settings.codex_prompt_max_chars,
    )
    managed_runner = CodexManagedRunner(
        settings.codex_binary,
        timeout_seconds=settings.managed_exec_timeout_seconds,
        capability_timeout_seconds=settings.codex_capability_timeout_seconds,
        prompt_max_chars=settings.codex_prompt_max_chars,
        output_max_bytes=settings.managed_output_max_bytes,
    )
    job_manager = JobManager(
        database,
        runner,
        adapter,
        managed_runner=managed_runner,
        prompt_max_chars=settings.codex_prompt_max_chars,
        allowed_roots=settings.allowed_roots,
    )
    job_manager.recover()
    control_channel_id = settings.discord_control_channel_id
    assert control_channel_id is not None
    session_manager = SessionManager(database, adapter, control_channel_id)
    completion_notifier = CompletionNotifier(
        database,
        adapter,
        retry_attempts=settings.discord_delivery_retry_attempts,
        retry_base_seconds=settings.discord_delivery_retry_base_seconds,
        secrets=(settings.discord_bot_token or "",),
    )

    async def handle_notify(event: NotifyEvent) -> None:
        registration = await session_manager.handle_notify(event)
        await job_manager.handle_completion(event.thread_id)
        await completion_notifier.deliver(event, registration)

    notify_server = NotifyServer(
        settings.notify_socket_path,
        settings.allowed_roots,
        handle_notify,
        max_payload_bytes=settings.notify_max_payload_bytes,
    )
    LOGGER.info(
        "service.discord_starting guild_allowlist=%d channel_allowlist=%d "
        "user_allowlist=%d",
        len(settings.discord_allowed_guild_ids),
        len(settings.discord_allowed_channel_ids),
        len(settings.discord_allowed_user_ids),
    )
    try:
        await runner.verify_capability()
        await managed_runner.verify_capability()
        await notify_server.start()
        try:
            await _run_discord_until_shutdown(adapter, shutdown_event)
        finally:
            await adapter.close()
            await job_manager.shutdown()
            await notify_server.close()
    finally:
        database.close()


def main() -> int:
    """Load configuration, configure logging, and run the Discord service."""

    try:
        settings = load_settings()
        configure_logging(
            settings.log_level,
            secrets=(settings.discord_bot_token or "",),
        )
        settings.require_discord()
        settings.require_notify()
        validate_runtime_security(
            allowed_roots=settings.allowed_roots,
            database_path=settings.database_path,
            codex_binary=settings.codex_binary,
            project_root=Path(__file__).resolve().parents[2],
        )
    except (SettingsError, ValueError) as exc:
        print(f"codex-dispatch: configuration error: {exc}", file=sys.stderr)
        return 2

    LOGGER.info(
        "service.bootstrap_complete database_path=%s allowed_roots=%d",
        settings.database_path,
        len(settings.allowed_roots),
    )

    try:
        asyncio.run(run_service(settings))
    except KeyboardInterrupt:
        LOGGER.info("service.interrupted")
        return 0
    except DiscordAdapterError as exc:
        LOGGER.error("service.discord_failed error=%s", exc)
        return 3
    except NotifyServerError as exc:
        LOGGER.error("service.notify_failed error=%s", exc)
        return 4
    except DatabaseError as exc:
        LOGGER.error("service.database_failed error=%s", exc)
        return 5
    except CodexRunnerError as exc:
        LOGGER.error("service.codex_failed error=%s", exc)
        return 6

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
