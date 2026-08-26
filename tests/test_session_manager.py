from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from codex_dispatch.database import Database
from codex_dispatch.notify_server import NotifyEvent
from codex_dispatch.session_manager import SessionManager


def event(
    cwd: Path,
    *,
    thread_id: str,
    turn_id: str,
) -> NotifyEvent:
    return NotifyEvent(
        event_type="agent-turn-complete",
        thread_id=thread_id,
        turn_id=turn_id,
        cwd=cwd,
        client="codex-tui",
        input_messages=(),
        last_assistant_message="done",
    )


class SessionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "creator-alliance"
        self.workspace.mkdir()
        self.db_path = self.root / "state" / "dispatch.db"
        self.db = Database(self.db_path)
        self.db.open()
        self.addCleanup(self.db.close)
        self.discord = AsyncMock()
        self.discord.create_thread.side_effect = [7001, 7002, 7003, 7004]
        self.manager = SessionManager(self.db, self.discord, 200)

    async def test_unknown_session_creates_discord_thread_and_mapping(self) -> None:
        registration = await self.manager.handle_notify(
            event(self.workspace, thread_id="abcdefgh-1111", turn_id="turn-1")
        )

        self.assertTrue(registration.session_created)
        self.assertTrue(registration.event_inserted)
        self.assertEqual(registration.session.discord_thread_id, 7001)
        self.discord.create_thread.assert_awaited_once_with(
            200,
            "creator-alliance / abcdefgh",
        )
        self.discord.send_text.assert_awaited_once_with(
            200,
            "🧵 새 Codex 세션 완료: <#7001>",
            mention_allowed_users=True,
        )

    async def test_duplicate_notify_does_not_create_second_thread(self) -> None:
        first = event(self.workspace, thread_id="session-1", turn_id="turn-1")
        await self.manager.handle_notify(first)
        second = await self.manager.handle_notify(first)

        self.assertFalse(second.event_inserted)
        self.assertEqual(self.discord.create_thread.await_count, 1)
        self.assertEqual(self.discord.send_text.await_count, 1)
        self.assertEqual(self.db.notify_event_count(), 1)

    async def test_next_turn_reuses_existing_discord_thread(self) -> None:
        await self.manager.handle_notify(
            event(self.workspace, thread_id="session-1", turn_id="turn-1")
        )
        second = await self.manager.handle_notify(
            event(self.workspace, thread_id="session-1", turn_id="turn-2")
        )

        self.assertFalse(second.session_created)
        self.assertEqual(second.session.discord_thread_id, 7001)
        self.assertEqual(self.discord.create_thread.await_count, 1)
        self.assertEqual(self.discord.send_text.await_count, 2)
        self.discord.send_text.assert_awaited_with(
            200,
            "✅ Codex 작업 완료: <#7001>",
            mention_allowed_users=True,
        )
        self.assertEqual(self.db.notify_event_count(), 2)

    async def test_three_sessions_receive_three_distinct_threads(self) -> None:
        for index in range(3):
            await self.manager.handle_notify(
                event(
                    self.workspace,
                    thread_id=f"session-{index}",
                    turn_id=f"turn-{index}",
                )
            )

        self.assertEqual(self.db.session_count(), 3)
        self.assertEqual(self.discord.create_thread.await_count, 3)
        self.assertEqual(self.discord.send_text.await_count, 3)
        mapped = {
            self.db.get_session(f"session-{index}").discord_thread_id  # type: ignore[union-attr]
            for index in range(3)
        }
        self.assertEqual(mapped, {7001, 7002, 7003})

    async def test_restart_reuses_persistent_mapping(self) -> None:
        await self.manager.handle_notify(
            event(self.workspace, thread_id="session-1", turn_id="turn-1")
        )
        self.db.close()

        reopened = Database(self.db_path)
        reopened.open()
        self.addCleanup(reopened.close)
        discord_after_restart = AsyncMock()
        discord_after_restart.create_thread.return_value = 7999
        restarted = SessionManager(reopened, discord_after_restart, 200)

        registration = await restarted.handle_notify(
            event(self.workspace, thread_id="session-1", turn_id="turn-1")
        )

        self.assertFalse(registration.event_inserted)
        self.assertEqual(registration.session.discord_thread_id, 7001)
        discord_after_restart.create_thread.assert_not_awaited()
        discord_after_restart.send_text.assert_not_awaited()

    async def test_failed_thread_creation_leaves_session_unmapped_for_retry(self) -> None:
        failing_discord = AsyncMock()
        failing_discord.create_thread.side_effect = RuntimeError("Discord unavailable")
        manager = SessionManager(self.db, failing_discord, 200)
        current = event(self.workspace, thread_id="session-1", turn_id="turn-1")

        with self.assertRaisesRegex(RuntimeError, "Discord unavailable"):
            await manager.handle_notify(current)

        session = self.db.get_session("session-1")
        assert session is not None
        self.assertIsNone(session.discord_thread_id)
        self.assertEqual(self.db.notify_event_count(), 1)

        retry_discord = AsyncMock()
        retry_discord.create_thread.return_value = 7555
        retry_manager = SessionManager(self.db, retry_discord, 200)
        registration = await retry_manager.handle_notify(current)
        self.assertFalse(registration.event_inserted)
        self.assertEqual(registration.session.discord_thread_id, 7555)
        retry_discord.create_thread.assert_awaited_once()
        retry_discord.send_text.assert_awaited_once_with(
            200,
            "🧵 새 Codex 세션 완료: <#7555>",
            mention_allowed_users=True,
        )

    async def test_parent_completion_alert_failure_does_not_break_mapping(self) -> None:
        self.discord.send_text.side_effect = RuntimeError("parent channel unavailable")

        registration = await self.manager.handle_notify(
            event(self.workspace, thread_id="session-1", turn_id="turn-1")
        )

        self.assertEqual(registration.session.discord_thread_id, 7001)
        self.assertEqual(self.db.notify_event_count(), 1)
        self.discord.create_thread.assert_awaited_once()
        self.discord.send_text.assert_awaited_once()

    async def test_lookup_by_discord_thread(self) -> None:
        await self.manager.handle_notify(
            event(self.workspace, thread_id="session-1", turn_id="turn-1")
        )
        session = self.manager.get_session_for_discord_thread(7001)
        assert session is not None
        self.assertEqual(session.thread_id, "session-1")


if __name__ == "__main__":
    unittest.main()
