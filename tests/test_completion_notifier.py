from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from codex_dispatch.completion_notifier import (
    CompletionDeliveryError,
    CompletionNotifier,
    format_completion_message,
)
from codex_dispatch.database import Database
from codex_dispatch.models import NotifyRegistration
from codex_dispatch.notify_server import NotifyEvent


def event(
    cwd: Path,
    *,
    thread_id: str = "abcdefgh-1111-2222",
    turn_id: str = "turn-001",
    result: str | None = "work completed",
) -> NotifyEvent:
    return NotifyEvent(
        event_type="agent-turn-complete",
        thread_id=thread_id,
        turn_id=turn_id,
        cwd=cwd,
        client="codex-tui",
        input_messages=("do work",),
        last_assistant_message=result,
    )


class CompletionNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.database = Database(self.root / "state" / "codex-dispatch.db")
        self.database.open()

    async def _cleanup(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def registration(self, current_event: NotifyEvent) -> NotifyRegistration:
        registration = self.database.register_notify(current_event)
        session = self.database.set_discord_thread(current_event.thread_id, 7001)
        return NotifyRegistration(
            workspace=registration.workspace,
            session=session,
            notify_event_id=registration.notify_event_id,
            event_inserted=registration.event_inserted,
            session_created=registration.session_created,
        )

    async def test_success_is_persisted_and_duplicate_is_suppressed(self) -> None:
        current_event = event(self.workspace)
        registration = self.registration(current_event)
        sender = AsyncMock()
        sender.send_text = AsyncMock(return_value=9001)
        notifier = CompletionNotifier(
            self.database,
            sender,
            retry_attempts=3,
            retry_base_seconds=0.01,
            sleep=AsyncMock(),
        )

        first = await notifier.deliver(current_event, registration)
        second = await notifier.deliver(current_event, registration)

        self.assertEqual(first.status, "SENT")
        self.assertEqual(second.status, "SENT")
        self.assertEqual(first.discord_message_id, 9001)
        sender.send_text.assert_awaited_once()
        args = sender.send_text.await_args
        self.assertEqual(args.args[0], 7001)
        self.assertEqual(args.kwargs["nonce"], registration.notify_event_id)

    async def test_transient_failures_retry_with_exponential_backoff(self) -> None:
        current_event = event(self.workspace)
        registration = self.registration(current_event)
        sender = AsyncMock()
        sender.send_text = AsyncMock(
            side_effect=[RuntimeError("one"), RuntimeError("two"), 9002]
        )
        sleeper = AsyncMock()
        notifier = CompletionNotifier(
            self.database,
            sender,
            retry_attempts=3,
            retry_base_seconds=0.5,
            sleep=sleeper,
        )

        delivered = await notifier.deliver(current_event, registration)

        self.assertEqual(delivered.status, "SENT")
        self.assertEqual(delivered.attempts, 3)
        self.assertEqual(sender.send_text.await_count, 3)
        self.assertEqual([call.args[0] for call in sleeper.await_args_list], [0.5, 1.0])

    async def test_retry_exhaustion_leaves_failed_state(self) -> None:
        current_event = event(self.workspace)
        registration = self.registration(current_event)
        sender = AsyncMock()
        sender.send_text = AsyncMock(side_effect=RuntimeError("offline"))
        notifier = CompletionNotifier(
            self.database,
            sender,
            retry_attempts=2,
            retry_base_seconds=0.01,
            sleep=AsyncMock(),
        )

        with self.assertRaises(CompletionDeliveryError):
            await notifier.deliver(current_event, registration)

        delivery = self.database.get_discord_delivery(registration.notify_event_id)
        assert delivery is not None
        self.assertEqual(delivery.status, "FAILED")
        self.assertEqual(delivery.attempts, 2)
        self.assertIn("RuntimeError", delivery.last_error or "")

    async def test_delivery_error_persistence_redacts_runtime_secret(self) -> None:
        token = "discord-token-must-not-persist"
        current_event = event(self.workspace)
        registration = self.registration(current_event)
        sender = AsyncMock()
        sender.send_text = AsyncMock(
            side_effect=RuntimeError(f"Authorization: Bot {token}")
        )
        notifier = CompletionNotifier(
            self.database,
            sender,
            retry_attempts=1,
            retry_base_seconds=0.01,
            sleep=AsyncMock(),
            secrets=(token,),
        )

        with self.assertRaises(CompletionDeliveryError):
            await notifier.deliver(current_event, registration)

        delivery = self.database.get_discord_delivery(registration.notify_event_id)
        assert delivery is not None
        self.assertNotIn(token, delivery.last_error or "")
        self.assertIn("[REDACTED]", delivery.last_error or "")

    async def test_unconfirmed_sending_state_is_not_automatically_retried(self) -> None:
        current_event = event(self.workspace)
        registration = self.registration(current_event)
        sending = self.database.begin_discord_delivery(registration.notify_event_id)
        self.assertEqual(sending.status, "SENDING")
        sender = AsyncMock()
        sender.send_text = AsyncMock(return_value=9003)
        notifier = CompletionNotifier(
            self.database,
            sender,
            retry_attempts=3,
            retry_base_seconds=0.01,
        )

        result = await notifier.deliver(current_event, registration)

        self.assertEqual(result.status, "SENDING")
        sender.send_text.assert_not_awaited()

    async def test_concurrent_duplicate_delivery_sends_once(self) -> None:
        current_event = event(self.workspace)
        registration = self.registration(current_event)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def send_text(channel_id: int, content: str, *, nonce: int | None = None) -> int:
            entered.set()
            await release.wait()
            return 9004

        sender = AsyncMock()
        sender.send_text = AsyncMock(side_effect=send_text)
        notifier = CompletionNotifier(
            self.database,
            sender,
            retry_attempts=3,
            retry_base_seconds=0.01,
        )

        first = asyncio.create_task(notifier.deliver(current_event, registration))
        await entered.wait()
        second = asyncio.create_task(notifier.deliver(current_event, registration))
        release.set()
        results = await asyncio.gather(first, second)

        self.assertEqual(sender.send_text.await_count, 1)
        self.assertTrue(all(result.status == "SENT" for result in results))


class CompletionFormattingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.database = Database(Path(self.temp.name) / "state.db")
        self.database.open()
        self.addCleanup(self.database.close)

    def registration(self, current_event: NotifyEvent) -> NotifyRegistration:
        registration = self.database.register_notify(current_event)
        session = self.database.set_discord_thread(current_event.thread_id, 7001)
        return NotifyRegistration(
            workspace=registration.workspace,
            session=session,
            notify_event_id=registration.notify_event_id,
            event_inserted=registration.event_inserted,
            session_created=registration.session_created,
        )

    def test_message_contains_project_session_and_result(self) -> None:
        current_event = event(self.workspace, result="all tests passed")
        content = format_completion_message(
            current_event,
            self.registration(current_event),
        )
        self.assertIn("Project: project", content)
        self.assertIn("Session: abcdefgh", content)
        self.assertIn("all tests passed", content)
        self.assertLessEqual(len(content), 2000)

    def test_long_result_is_truncated_to_discord_limit(self) -> None:
        current_event = event(self.workspace, result="x" * 10_000)
        content = format_completion_message(
            current_event,
            self.registration(current_event),
        )
        self.assertEqual(len(content), 2000)
        self.assertTrue(content.endswith("… (truncated)"))

    def test_missing_result_has_explicit_placeholder(self) -> None:
        current_event = event(self.workspace, result=None)
        content = format_completion_message(
            current_event,
            self.registration(current_event),
        )
        self.assertIn("(결과 메시지 없음)", content)


if __name__ == "__main__":
    unittest.main()
