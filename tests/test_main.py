from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from codex_dispatch.adapters.discord import (
    DiscordAdapterError,
    DiscordCommandRequest,
    DiscordIncomingMessage,
)
from codex_dispatch.codex_runner import CodexCapabilityError
from codex_dispatch.config import SettingsError, load_settings
from codex_dispatch.database import DatabaseError
from codex_dispatch.main import _run_discord_until_shutdown, main, run_service
from codex_dispatch.notify_server import NotifyEvent, NotifyServerError


DISCORD_ENV = {
    "DISCORD_BOT_TOKEN": "test-token",
    "DISCORD_CONTROL_CHANNEL_ID": "200",
    "DISCORD_ALLOWED_GUILD_IDS": "100",
    "DISCORD_ALLOWED_CHANNEL_IDS": "200",
    "DISCORD_ALLOWED_USER_IDS": "300",
    "CODEX_ALLOWED_ROOTS": "/tmp",
}


class ShutdownAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_event_closes_discord_and_waits_for_exit(self) -> None:
        closed = __import__("asyncio").Event()
        shutdown = __import__("asyncio").Event()
        adapter = MagicMock()

        async def start() -> None:
            await closed.wait()

        async def close() -> None:
            closed.set()

        adapter.start = AsyncMock(side_effect=start)
        adapter.close = AsyncMock(side_effect=close)
        task = __import__("asyncio").create_task(
            _run_discord_until_shutdown(adapter, shutdown)
        )
        await __import__("asyncio").sleep(0)
        shutdown.set()
        await task

        adapter.start.assert_awaited_once()
        adapter.close.assert_awaited_once()


class RunServiceAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_service_wires_job_manager_commands_and_notify(self) -> None:
        settings = load_settings(DISCORD_ENV)
        adapter = MagicMock()
        adapter.start = AsyncMock(return_value=None)
        adapter.close = AsyncMock(return_value=None)
        notify_server = MagicMock()
        notify_server.start = AsyncMock(return_value=None)
        notify_server.close = AsyncMock(return_value=None)
        database = MagicMock()
        codex_runner = MagicMock()
        codex_runner.verify_capability = AsyncMock(return_value=None)
        managed_runner = MagicMock()
        managed_runner.verify_capability = AsyncMock(return_value=None)
        job_manager = MagicMock()
        job_manager.handle_message = AsyncMock(return_value=None)
        job_manager.handle_completion = AsyncMock(return_value=None)
        job_manager.recover.return_value = 0
        job_manager.status_text.return_value = "status"
        job_manager.queue_text.return_value = "queue"
        job_manager.cancel_text = AsyncMock(return_value="cancelled")
        job_manager.mode_text = AsyncMock(return_value="mode")
        job_manager.auto_text = AsyncMock(return_value="auto")
        job_manager.resume_auto_run = AsyncMock(return_value=1)
        job_manager.shutdown = AsyncMock(return_value=0)
        session_manager = MagicMock()
        registration = object()
        session_manager.handle_notify = AsyncMock(return_value=registration)
        completion_notifier = MagicMock()
        completion_notifier.deliver = AsyncMock(return_value=None)

        with patch("codex_dispatch.main.Database", return_value=database), patch(
            "codex_dispatch.main.DiscordAdapter", return_value=adapter
        ) as adapter_cls, patch(
            "codex_dispatch.main.CodexQueueRunner", return_value=codex_runner
        ), patch(
            "codex_dispatch.main.CodexManagedRunner", return_value=managed_runner
        ), patch(
            "codex_dispatch.main.JobManager", return_value=job_manager
        ), patch(
            "codex_dispatch.main.SessionManager", return_value=session_manager
        ), patch(
            "codex_dispatch.main.CompletionNotifier", return_value=completion_notifier
        ), patch("codex_dispatch.main.NotifyServer", return_value=notify_server) as server_cls:
            await run_service(settings)

        discord_handler = adapter_cls.call_args.args[1]
        discord_message = DiscordIncomingMessage(1, 100, 7001, 200, 300, "next")
        await discord_handler(discord_message)
        job_manager.handle_message.assert_awaited_once_with(discord_message)

        command_handler = adapter_cls.call_args.args[2]
        mode_request = DiscordCommandRequest(
            "mode", 100, 7001, 200, 300, mode="managed"
        )
        self.assertEqual(await command_handler(mode_request), "mode")
        job_manager.mode_text.assert_awaited_once_with(7001, "managed")

        cancel_request = DiscordCommandRequest(
            "cancel", 100, 7001, 200, 300, job_id=42
        )
        self.assertEqual(await command_handler(cancel_request), "cancelled")
        job_manager.cancel_text.assert_awaited_once_with(7001, 42)

        auto_request = DiscordCommandRequest(
            "auto", 100, 7001, 200, 300, auto_state="on"
        )
        self.assertEqual(await command_handler(auto_request), "auto")
        job_manager.auto_text.assert_awaited_once_with(7001, "on")

        ready_handler = adapter_cls.call_args.args[3]
        await ready_handler()
        job_manager.resume_auto_run.assert_awaited_once()

        notify_handler = server_cls.call_args.args[2]
        notify_event = NotifyEvent(
            "agent-turn-complete",
            "thread-1",
            "turn-1",
            Path("/tmp"),
            "codex-tui",
            (),
            "done",
        )
        await notify_handler(notify_event)
        session_manager.handle_notify.assert_awaited_once_with(notify_event)
        job_manager.handle_completion.assert_awaited_once_with("thread-1")
        completion_notifier.deliver.assert_awaited_once_with(notify_event, registration)

        job_manager.recover.assert_called_once()
        database.open.assert_called_once()
        codex_runner.verify_capability.assert_awaited_once()
        managed_runner.verify_capability.assert_awaited_once()
        job_manager.shutdown.assert_awaited_once()
        notify_server.start.assert_awaited_once()
        adapter.start.assert_awaited_once()
        adapter.close.assert_awaited_once()
        notify_server.close.assert_awaited_once()
        database.close.assert_called_once()


class MainTests(unittest.TestCase):
    def test_main_runs_service_for_complete_discord_configuration(self) -> None:
        runner = AsyncMock(return_value=None)
        with patch.dict("os.environ", DISCORD_ENV, clear=True), patch(
            "codex_dispatch.main.run_service", runner
        ):
            self.assertEqual(main(), 0)
        runner.assert_awaited_once()

    def test_main_returns_two_for_missing_discord_configuration(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(main(), 2)

    def test_main_returns_two_for_invalid_configuration(self) -> None:
        with patch(
            "codex_dispatch.main.load_settings",
            side_effect=SettingsError("bad configuration"),
        ):
            self.assertEqual(main(), 2)

    def test_main_returns_two_for_runtime_security_failure(self) -> None:
        with patch.dict("os.environ", DISCORD_ENV, clear=True), patch(
            "codex_dispatch.main.validate_runtime_security",
            side_effect=ValueError("unsafe workspace boundary"),
        ), patch("codex_dispatch.main.run_service", AsyncMock()) as runner:
            self.assertEqual(main(), 2)
        runner.assert_not_awaited()

    def test_main_returns_three_for_discord_adapter_failure(self) -> None:
        runner = AsyncMock(side_effect=DiscordAdapterError("gateway unavailable"))
        with patch.dict("os.environ", DISCORD_ENV, clear=True), patch(
            "codex_dispatch.main.run_service", runner
        ):
            self.assertEqual(main(), 3)

    def test_main_returns_four_for_notify_server_failure(self) -> None:
        runner = AsyncMock(side_effect=NotifyServerError("socket unavailable"))
        with patch.dict("os.environ", DISCORD_ENV, clear=True), patch(
            "codex_dispatch.main.run_service", runner
        ):
            self.assertEqual(main(), 4)

    def test_main_returns_five_for_database_failure(self) -> None:
        runner = AsyncMock(side_effect=DatabaseError("database unavailable"))
        with patch.dict("os.environ", DISCORD_ENV, clear=True), patch(
            "codex_dispatch.main.run_service", runner
        ):
            self.assertEqual(main(), 5)

    def test_main_returns_six_for_codex_capability_failure(self) -> None:
        runner = AsyncMock(side_effect=CodexCapabilityError("queue unsupported"))
        with patch.dict("os.environ", DISCORD_ENV, clear=True), patch(
            "codex_dispatch.main.run_service", runner
        ):
            self.assertEqual(main(), 6)


if __name__ == "__main__":
    unittest.main()
