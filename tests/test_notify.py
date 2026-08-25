from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock

from codex_dispatch.notify_client import default_notify_socket_path, send_payload
from codex_dispatch.notify_server import (
    NotifyServer,
    NotifyServerError,
    NotifyValidationError,
    parse_notify_payload,
)


THREAD_ID = "00000000-1111-2222-3333-444444444444"
TURN_ID = "phase3-turn-001"


def make_payload(cwd: Path, **updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "agent-turn-complete",
        "thread-id": THREAD_ID,
        "turn-id": TURN_ID,
        "cwd": str(cwd),
        "client": "codex-tui",
        "input-messages": ["test prompt"],
        "last-assistant-message": "test result",
    }
    payload.update(updates)
    return payload


class NotifyValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.allowed = self.base / "allowed"
        self.workspace = self.allowed / "project"
        self.outside = self.base / "outside"
        self.workspace.mkdir(parents=True)
        self.outside.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_valid_payload_is_normalized(self) -> None:
        event = parse_notify_payload(make_payload(self.workspace), [self.allowed])

        self.assertEqual(event.thread_id, THREAD_ID)
        self.assertEqual(event.turn_id, TURN_ID)
        self.assertEqual(event.cwd, self.workspace.resolve())
        self.assertEqual(event.client, "codex-tui")
        self.assertEqual(event.input_messages, ("test prompt",))
        self.assertEqual(event.last_assistant_message, "test result")

    def test_rejects_unknown_event_type(self) -> None:
        with self.assertRaisesRegex(NotifyValidationError, "unsupported"):
            parse_notify_payload(
                make_payload(self.workspace, type="approval-request"),
                [self.allowed],
            )

    def test_rejects_missing_identity_fields(self) -> None:
        payload = make_payload(self.workspace)
        del payload["thread-id"]

        with self.assertRaisesRegex(NotifyValidationError, "thread-id"):
            parse_notify_payload(payload, [self.allowed])

    def test_rejects_relative_cwd(self) -> None:
        payload = make_payload(self.workspace)
        payload["cwd"] = "relative/project"

        with self.assertRaisesRegex(NotifyValidationError, "absolute"):
            parse_notify_payload(payload, [self.allowed])

    def test_rejects_workspace_outside_allowed_roots(self) -> None:
        with self.assertRaisesRegex(NotifyValidationError, "outside"):
            parse_notify_payload(make_payload(self.outside), [self.allowed])

    def test_rejects_symlink_escape(self) -> None:
        link = self.allowed / "escape"
        link.symlink_to(self.outside, target_is_directory=True)

        with self.assertRaisesRegex(NotifyValidationError, "outside"):
            parse_notify_payload(make_payload(link), [self.allowed])

    def test_rejects_non_string_message_list(self) -> None:
        with self.assertRaisesRegex(NotifyValidationError, "list of strings"):
            parse_notify_payload(
                make_payload(self.workspace, **{"input-messages": ["ok", 42]}),
                [self.allowed],
            )

    def test_default_socket_prefers_xdg_runtime_dir(self) -> None:
        path = default_notify_socket_path({"XDG_RUNTIME_DIR": "/tmp/runtime-test"})

        self.assertEqual(path, Path("/tmp/runtime-test/codex-dispatch/notify.sock"))

    def test_notify_settings_can_be_read_from_explicit_systemd_env_file(self) -> None:
        env_file = self.base / "codex-dispatch.env"
        env_file.write_text(
            "CODEX_DISPATCH_NOTIFY_SOCKET=/run/codex-dispatch/notify.sock\n"
            "CODEX_DISPATCH_NOTIFY_TIMEOUT_SECONDS=1.25\n",
            encoding="utf-8",
        )
        path = default_notify_socket_path(
            {"CODEX_DISPATCH_NOTIFY_ENV_FILE": str(env_file)}
        )
        self.assertEqual(path, Path("/run/codex-dispatch/notify.sock"))

    def test_explicit_missing_notify_env_file_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not exist"):
            default_notify_socket_path(
                {"CODEX_DISPATCH_NOTIFY_ENV_FILE": str(self.base / "missing.env")}
            )

    def test_legacy_notify_env_override_remains_compatible(self) -> None:
        env_file = self.base / "legacy-notify.env"
        env_file.write_text(
            "CODEX_DISPATCH_NOTIFY_SOCKET=/run/codex-dispatch/notify.sock\n",
            encoding="utf-8",
        )
        path = default_notify_socket_path({"CODEX_DISPATCH_ENV_FILE": str(env_file)})
        self.assertEqual(path, Path("/run/codex-dispatch/notify.sock"))


class NotifyServerAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.allowed = self.base / "allowed"
        self.workspace = self.allowed / "project"
        self.runtime = self.base / "runtime" / "codex-dispatch"
        self.socket_path = self.runtime / "notify.sock"
        self.workspace.mkdir(parents=True)
        self.handler = AsyncMock(return_value=None)
        self.server = NotifyServer(
            self.socket_path,
            [self.allowed],
            self.handler,
            max_payload_bytes=16_384,
        )
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.close()
        self.temp.cleanup()

    def client_env(self) -> dict[str, str]:
        return {
            "CODEX_DISPATCH_NOTIFY_SOCKET": str(self.socket_path),
            "CODEX_DISPATCH_NOTIFY_MAX_BYTES": "16384",
            "CODEX_DISPATCH_NOTIFY_TIMEOUT_SECONDS": "2",
        }

    async def send(self, payload: dict[str, object]) -> str:
        raw = json.dumps(payload)
        return await asyncio.to_thread(send_payload, raw, environ=self.client_env())

    async def test_round_trip_calls_handler_and_returns_ack(self) -> None:
        status = await self.send(make_payload(self.workspace))

        self.assertEqual(status, "ACK")
        self.handler.assert_awaited_once()
        event = self.handler.await_args.args[0]
        self.assertEqual(event.thread_id, THREAD_ID)
        self.assertEqual(event.cwd, self.workspace.resolve())

    async def test_duplicate_event_is_not_delivered_twice(self) -> None:
        first = await self.send(make_payload(self.workspace))
        second = await self.send(make_payload(self.workspace))

        self.assertEqual(first, "ACK")
        self.assertEqual(second, "DUPLICATE")
        self.handler.assert_awaited_once()

    async def test_handler_failure_does_not_mark_event_duplicate(self) -> None:
        self.handler.side_effect = [RuntimeError("boom"), None]

        first = await self.send(make_payload(self.workspace))
        second = await self.send(make_payload(self.workspace))

        self.assertEqual(first, "ERROR handler")
        self.assertEqual(second, "ACK")
        self.assertEqual(self.handler.await_count, 2)

    async def test_invalid_workspace_is_rejected_before_handler(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()

        status = await self.send(make_payload(outside))

        self.assertEqual(status, "ERROR validation")
        self.handler.assert_not_awaited()

    async def test_invalid_json_frame_is_rejected(self) -> None:
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        raw = b"{not-json"
        writer.write(struct.pack("!I", len(raw)) + raw)
        await writer.drain()

        response = await reader.readline()
        writer.close()
        await writer.wait_closed()

        self.assertEqual(response, b"ERROR validation\n")
        self.handler.assert_not_awaited()

    async def test_oversized_frame_is_rejected_without_body_read(self) -> None:
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        writer.write(struct.pack("!I", 16_385))
        await writer.drain()

        response = await reader.readline()
        writer.close()
        await writer.wait_closed()

        self.assertEqual(response, b"ERROR validation\n")
        self.handler.assert_not_awaited()

    async def test_runtime_permissions_are_private(self) -> None:
        socket_mode = stat.S_IMODE(self.socket_path.stat().st_mode)
        directory_mode = stat.S_IMODE(self.runtime.stat().st_mode)

        self.assertEqual(socket_mode, 0o600)
        self.assertEqual(directory_mode, 0o700)

    async def test_close_removes_socket(self) -> None:
        await self.server.close()

        self.assertFalse(self.socket_path.exists())


class NotifyServerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_refuses_second_instance_on_active_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            allowed = base / "allowed"
            allowed.mkdir()
            runtime = base / "runtime"
            socket_path = runtime / "notify.sock"
            first = NotifyServer(socket_path, [allowed], AsyncMock())
            second = NotifyServer(socket_path, [allowed], AsyncMock())
            await first.start()
            try:
                with self.assertRaisesRegex(NotifyServerError, "already active"):
                    await second.start()
                self.assertTrue(socket_path.exists())
            finally:
                await first.close()

    async def test_refuses_to_replace_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            allowed = base / "allowed"
            allowed.mkdir()
            socket_path = base / "runtime" / "notify.sock"
            socket_path.parent.mkdir(mode=0o700)
            socket_path.write_text("do not replace", encoding="utf-8")
            server = NotifyServer(socket_path, [allowed], AsyncMock())

            with self.assertRaisesRegex(NotifyServerError, "non-socket"):
                await server.start()

            self.assertTrue(socket_path.is_file())

    async def test_refuses_insecure_existing_socket_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            allowed = base / "allowed"
            allowed.mkdir()
            runtime = base / "shared-runtime"
            runtime.mkdir(mode=0o755)
            runtime.chmod(0o755)
            socket_path = runtime / "notify.sock"
            server = NotifyServer(socket_path, [allowed], AsyncMock())

            with self.assertRaisesRegex(NotifyServerError, "private"):
                await server.start()

            self.assertFalse(socket_path.exists())


class NotifyClientTests(unittest.TestCase):
    def test_rejects_non_json_before_connecting(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid JSON"):
            send_payload(
                "not-json",
                environ={"CODEX_DISPATCH_NOTIFY_SOCKET": "/tmp/unused.sock"},
            )

    def test_rejects_payload_over_client_limit(self) -> None:
        raw = json.dumps({"value": "x" * 100})
        with self.assertRaisesRegex(ValueError, "size limit"):
            send_payload(
                raw,
                environ={
                    "CODEX_DISPATCH_NOTIFY_SOCKET": "/tmp/unused.sock",
                    "CODEX_DISPATCH_NOTIFY_MAX_BYTES": "16",
                },
            )


if __name__ == "__main__":
    unittest.main()
