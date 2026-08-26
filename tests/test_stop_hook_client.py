from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_dispatch.stop_hook_client import (
    StopHookValidationError,
    main,
    normalize_stop_hook_payload,
)


ROOT = Path(__file__).resolve().parents[1]
THREAD_ID = "01a03b5d-5a15-76f0-809e-c555c79bb0d1"
TURN_ID = "01a03b5d-7fa6-7b52-a88e-8ead8b53aa28"


def make_stop_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": THREAD_ID,
        "turn_id": TURN_ID,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/home/test/project",
        "hook_event_name": "Stop",
        "model": "gpt-test",
        "permission_mode": "dontAsk",
        "stop_hook_active": False,
        "last_assistant_message": "CODEX_DISPATCH_E2E_READY",
    }
    payload.update(updates)
    return payload


class StopHookPayloadTests(unittest.TestCase):
    def test_normalizes_stop_payload_to_existing_notify_protocol(self) -> None:
        normalized = normalize_stop_hook_payload(make_stop_payload())
        self.assertEqual(normalized["type"], "agent-turn-complete")
        self.assertEqual(normalized["thread-id"], THREAD_ID)
        self.assertEqual(normalized["turn-id"], TURN_ID)
        self.assertEqual(normalized["cwd"], "/home/test/project")
        self.assertEqual(normalized["client"], "codex-stop-hook")
        self.assertEqual(normalized["input-messages"], [])
        self.assertEqual(
            normalized["last-assistant-message"], "CODEX_DISPATCH_E2E_READY"
        )

    def test_accepts_null_last_assistant_message(self) -> None:
        normalized = normalize_stop_hook_payload(
            make_stop_payload(last_assistant_message=None)
        )
        self.assertIsNone(normalized["last-assistant-message"])

    def test_rejects_wrong_event_or_missing_identity(self) -> None:
        with self.assertRaisesRegex(StopHookValidationError, "unsupported"):
            normalize_stop_hook_payload(make_stop_payload(hook_event_name="SessionEnd"))
        with self.assertRaisesRegex(StopHookValidationError, "session_id"):
            normalize_stop_hook_payload(make_stop_payload(session_id=""))
        with self.assertRaisesRegex(StopHookValidationError, "turn_id"):
            normalize_stop_hook_payload(make_stop_payload(turn_id=None))

    def test_main_forwards_over_existing_socket_protocol(self) -> None:
        payload = json.dumps(make_stop_payload())
        stdin = io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")), encoding="utf-8")
        with patch.object(sys, "stdin", stdin), patch(
            "codex_dispatch.stop_hook_client.send_payload", return_value="ACK"
        ) as sender:
            self.assertEqual(main([]), 0)
        forwarded = json.loads(sender.call_args.args[0])
        self.assertEqual(forwarded["thread-id"], THREAD_ID)
        self.assertEqual(forwarded["turn-id"], TURN_ID)


class StopHookConfiguratorTests(unittest.TestCase):
    def test_configurator_merges_and_is_idempotent(self) -> None:
        if hasattr(__import__("os"), "geteuid") and __import__("os").geteuid() == 0:
            self.skipTest("configurator intentionally rejects root")
        with tempfile.TemporaryDirectory() as tmp:
            hooks_file = Path(tmp) / "hooks.json"
            hooks_file.write_text(
                json.dumps(
                    {
                        "description": "existing",
                        "hooks": {
                            "SessionStart": [
                                {
                                    "hooks": [
                                        {"type": "command", "command": "echo existing"}
                                    ]
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(ROOT / "scripts/configure-codex-stop-hook.py"),
                "--hooks-file",
                str(hooks_file),
            ]
            first = subprocess.run(command, check=True, capture_output=True, text=True)
            second = subprocess.run(command, check=True, capture_output=True, text=True)
            document = json.loads(hooks_file.read_text(encoding="utf-8"))
            self.assertEqual(document["description"], "existing")
            self.assertIn("SessionStart", document["hooks"])
            stop = document["hooks"]["Stop"]
            matching = [
                handler
                for group in stop
                for handler in group.get("hooks", [])
                if "codex_dispatch.stop_hook_client" in handler.get("command", "")
            ]
            self.assertEqual(len(matching), 1)
            self.assertIn("UPDATED", first.stdout)
            self.assertIn("ALREADY_CONFIGURED", second.stdout)
            self.assertEqual(hooks_file.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
