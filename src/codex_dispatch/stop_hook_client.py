"""Bridge Codex Stop hook events into the existing notify socket protocol."""

from __future__ import annotations

import json
import socket
import sys
from typing import Any, Mapping, Sequence

from .notify_client import send_payload

_MAX_STDIN_BYTES = 262_144
_MAX_ID_LENGTH = 256
_MAX_CWD_LENGTH = 4096


class StopHookValidationError(ValueError):
    """Raised when a Codex Stop hook payload cannot be normalized safely."""


def _required_text(
    payload: Mapping[str, Any],
    key: str,
    *,
    max_length: int,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise StopHookValidationError(f"{key} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise StopHookValidationError(f"{key} exceeds the maximum length")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise StopHookValidationError(f"{key} contains control characters")
    return normalized


def normalize_stop_hook_payload(payload: Mapping[str, Any]) -> dict[str, object]:
    """Map the current Codex Stop hook schema to legacy completion fields.

    Codex 0.149.x always uses the app-server-backed TUI. Its supported Stop hook
    carries the same stable identifiers that Codex Dispatch previously received
    through the legacy top-level ``notify`` callback. Keeping the wire format
    unchanged lets the existing server-side validation, idempotency, session
    mapping, and Discord delivery paths remain authoritative.
    """

    event_name = _required_text(payload, "hook_event_name", max_length=64)
    if event_name != "Stop":
        raise StopHookValidationError("unsupported hook event")

    session_id = _required_text(payload, "session_id", max_length=_MAX_ID_LENGTH)
    turn_id = _required_text(payload, "turn_id", max_length=_MAX_ID_LENGTH)
    cwd = _required_text(payload, "cwd", max_length=_MAX_CWD_LENGTH)

    last_message = payload.get("last_assistant_message")
    if last_message is not None and not isinstance(last_message, str):
        raise StopHookValidationError("last_assistant_message must be a string or null")

    return {
        "type": "agent-turn-complete",
        "thread-id": session_id,
        "turn-id": turn_id,
        "cwd": cwd,
        "client": "codex-stop-hook",
        "input-messages": [],
        "last-assistant-message": last_message,
    }


def _read_stdin() -> str:
    raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if len(raw) > _MAX_STDIN_BYTES:
        raise StopHookValidationError("Stop hook payload exceeds the size limit")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StopHookValidationError("Stop hook payload is not valid UTF-8") from exc


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        print("usage: codex-dispatch-stop-hook < hook.json", file=sys.stderr)
        return 64

    try:
        raw = _read_stdin()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise StopHookValidationError("Stop hook payload must be a JSON object")
        normalized = normalize_stop_hook_payload(payload)
        status = send_payload(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")))
    except json.JSONDecodeError as exc:
        print(f"codex-dispatch-stop-hook: invalid JSON: {exc}", file=sys.stderr)
        return 65
    except StopHookValidationError as exc:
        print(f"codex-dispatch-stop-hook: invalid hook payload: {exc}", file=sys.stderr)
        return 65
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, socket.timeout) as exc:
        print(
            f"codex-dispatch-stop-hook: orchestrator unavailable: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 69
    except OSError as exc:
        print(
            f"codex-dispatch-stop-hook: local socket error: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 69
    except ConnectionError as exc:
        print(f"codex-dispatch-stop-hook: protocol error: {exc}", file=sys.stderr)
        return 70

    if status in {"ACK", "DUPLICATE"}:
        return 0

    print(f"codex-dispatch-stop-hook: server rejected event: {status}", file=sys.stderr)
    return 70


if __name__ == "__main__":
    raise SystemExit(main())
