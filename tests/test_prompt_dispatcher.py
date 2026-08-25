from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from codex_dispatch.adapters.discord import DiscordIncomingMessage
from codex_dispatch.codex_runner import (
    CodexCapabilityError,
    CodexQueueResult,
    CodexSessionUnavailableError,
)
from codex_dispatch.database import Database
from codex_dispatch.notify_server import NotifyEvent
from codex_dispatch.prompt_dispatcher import DiscordPromptDispatcher


def notify_event(workspace: Path) -> NotifyEvent:
    return NotifyEvent(
        event_type="agent-turn-complete",
        thread_id="thread-123",
        turn_id="turn-001",
        cwd=workspace,
        client="codex-tui",
        input_messages=("work",),
        last_assistant_message="done",
    )


def incoming(content: str = "next task", *, channel_id: int = 7001) -> DiscordIncomingMessage:
    return DiscordIncomingMessage(
        message_id=9001,
        guild_id=100,
        channel_id=channel_id,
        parent_channel_id=200,
        user_id=300,
        content=content,
    )


class PromptDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.db = Database(self.root / "state" / "dispatch.db")
        self.db.open()
        self.db.register_notify(notify_event(self.workspace))
        self.db.set_discord_thread("thread-123", 7001)
        self.runner = type("Runner", (), {})()
        self.runner.queue_message = AsyncMock(
            return_value=CodexQueueResult(0, "queued", "")
        )
        self.discord = type("Discord", (), {})()
        self.discord.send_text = AsyncMock(return_value=9100)
        self.dispatcher = DiscordPromptDispatcher(
            self.db,
            self.runner,
            self.discord,
            prompt_max_chars=100,
        )

    async def _cleanup(self) -> None:
        self.db.close()
        self.temp.cleanup()

    async def test_mapped_live_thread_dispatches_and_marks_running(self) -> None:
        await self.dispatcher.dispatch(incoming("  next\r\nstep  "))

        self.runner.queue_message.assert_awaited_once_with(
            "thread-123",
            "next\nstep",
            cwd=self.workspace,
        )
        session = self.db.get_session("thread-123")
        assert session is not None
        self.assertEqual(session.status, "RUNNING")
        self.discord.send_text.assert_awaited_once_with(
            7001,
            "▶️ Codex에 전달했습니다.",
            nonce=9001,
        )

    async def test_unmapped_discord_thread_is_rejected(self) -> None:
        await self.dispatcher.dispatch(incoming(channel_id=7999))

        self.runner.queue_message.assert_not_awaited()
        self.assertIn(
            "연결된 Codex 세션이 없습니다",
            self.discord.send_text.await_args.args[1],
        )

    async def test_non_live_session_is_rejected(self) -> None:
        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE sessions SET mode = 'MANAGED' WHERE thread_id = ?",
                ("thread-123",),
            )

        await self.dispatcher.dispatch(incoming())

        self.runner.queue_message.assert_not_awaited()
        self.assertIn("LIVE Codex 세션이 아니어서", self.discord.send_text.await_args.args[1])

    async def test_oversized_prompt_is_rejected_before_codex(self) -> None:
        await self.dispatcher.dispatch(incoming("x" * 101))

        self.runner.queue_message.assert_not_awaited()
        self.assertIn("프롬프트를 전달할 수 없습니다", self.discord.send_text.await_args.args[1])

    async def test_unavailable_live_session_marks_offline(self) -> None:
        self.runner.queue_message.side_effect = CodexSessionUnavailableError("gone")

        await self.dispatcher.dispatch(incoming())

        session = self.db.get_session("thread-123")
        assert session is not None
        self.assertEqual(session.status, "OFFLINE")
        self.assertIn("live 세션", self.discord.send_text.await_args.args[1])

    async def test_missing_queue_capability_marks_error(self) -> None:
        self.runner.queue_message.side_effect = CodexCapabilityError("unsupported")

        await self.dispatcher.dispatch(incoming())

        session = self.db.get_session("thread-123")
        assert session is not None
        self.assertEqual(session.status, "ERROR")
        self.assertIn("queue 기능", self.discord.send_text.await_args.args[1])

    async def test_feedback_failure_does_not_undo_successful_dispatch(self) -> None:
        self.discord.send_text.side_effect = RuntimeError("discord unavailable")

        await self.dispatcher.dispatch(incoming())

        self.runner.queue_message.assert_awaited_once()
        session = self.db.get_session("thread-123")
        assert session is not None
        self.assertEqual(session.status, "RUNNING")


if __name__ == "__main__":
    unittest.main()
