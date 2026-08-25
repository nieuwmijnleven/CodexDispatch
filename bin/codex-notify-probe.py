#!/usr/bin/env python3
"""Capture a privacy-preserving summary of a Codex external notify payload.

Codex appends its legacy notify JSON payload as the final argv argument. This
probe intentionally does not persist prompt text, assistant output, full cwd,
or the actual thread/turn identifiers. It records only whether the fields are
present and enough schema information for PHASE 0 capability verification.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT = "/tmp/codex-dispatch-notify-probe.jsonl"


def _present_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def summarize(payload: dict[str, Any]) -> dict[str, Any]:
    input_messages = payload.get("input-messages")
    if not isinstance(input_messages, list):
        input_messages = []

    return {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "type": payload.get("type"),
        "keys": sorted(str(key) for key in payload.keys()),
        "thread_id_present": _present_string(payload.get("thread-id")),
        "turn_id_present": _present_string(payload.get("turn-id")),
        "cwd_present": _present_string(payload.get("cwd")),
        "client_present": _present_string(payload.get("client")),
        "input_message_count": len(input_messages),
        "last_assistant_message_present": _present_string(
            payload.get("last-assistant-message")
        ),
    }


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: codex-notify-probe.py <NOTIFICATION_JSON>", file=sys.stderr)
        return 64

    try:
        payload = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        print(f"invalid Codex notify JSON: {exc}", file=sys.stderr)
        return 65

    if not isinstance(payload, dict):
        print("Codex notify payload must be a JSON object", file=sys.stderr)
        return 65

    output = Path(
        os.environ.get("CODEX_DISPATCH_NOTIFY_PROBE_FILE", DEFAULT_OUTPUT)
    ).expanduser()
    append_jsonl(output, summarize(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
