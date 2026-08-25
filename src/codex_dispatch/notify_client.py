"""Fast external-notify client used by Codex to reach the local orchestrator."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import struct
import sys
from typing import Mapping, Sequence


_HEADER = struct.Struct("!I")
_DEFAULT_MAX_PAYLOAD_BYTES = 262_144
_DEFAULT_TIMEOUT_SECONDS = 0.75
_DEFAULT_NOTIFY_ENV_FILE = Path("/etc/codex-dispatch/notify.env")
_NOTIFY_ENV_KEYS = {
    "CODEX_DISPATCH_NOTIFY_SOCKET",
    "CODEX_DISPATCH_NOTIFY_MAX_BYTES",
    "CODEX_DISPATCH_NOTIFY_TIMEOUT_SECONDS",
}


def _notify_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Merge notify-only settings from the systemd env file without executing it."""

    env = dict(os.environ if environ is None else environ)
    configured_file = (
        env.get("CODEX_DISPATCH_NOTIFY_ENV_FILE", "").strip()
        or env.get("CODEX_DISPATCH_ENV_FILE", "").strip()
    )
    if environ is not None and not configured_file:
        return env
    path = Path(configured_file).expanduser() if configured_file else _DEFAULT_NOTIFY_ENV_FILE
    if not path.is_absolute():
        raise ValueError("Codex Dispatch notify env file must be an absolute path")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        if configured_file:
            raise ValueError(f"Codex Dispatch notify env file does not exist: {path}")
        return env
    except OSError as exc:
        if configured_file:
            raise ValueError(f"cannot read Codex Dispatch notify env file: {path}") from exc
        return env

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in _NOTIFY_ENV_KEYS and not env.get(key, "").strip():
            env[key] = value.strip()
    return env


def default_notify_socket_path(environ: Mapping[str, str] | None = None) -> Path:
    env = _notify_environment(environ)
    configured = env.get("CODEX_DISPATCH_NOTIFY_SOCKET", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError("CODEX_DISPATCH_NOTIFY_SOCKET must be an absolute path")
        return path.resolve(strict=False)

    runtime_dir = env.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir:
        base = Path(runtime_dir).expanduser()
    else:
        base = Path(f"/run/user/{os.getuid()}")
    return (base / "codex-dispatch" / "notify.sock").resolve(strict=False)


def _positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise ValueError(f"{key} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _positive_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a positive number") from exc
    if value <= 0:
        raise ValueError(f"{key} must be a positive number")
    return value


def send_payload(
    raw_payload: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Validate and send one Codex notify JSON payload, returning the server status."""

    env = _notify_environment(environ)
    max_bytes = _positive_int(
        env,
        "CODEX_DISPATCH_NOTIFY_MAX_BYTES",
        _DEFAULT_MAX_PAYLOAD_BYTES,
    )
    timeout = _positive_float(
        env,
        "CODEX_DISPATCH_NOTIFY_TIMEOUT_SECONDS",
        _DEFAULT_TIMEOUT_SECONDS,
    )
    socket_path = default_notify_socket_path(env)

    encoded = raw_payload.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError("Codex notify payload exceeds the configured size limit")

    try:
        parsed = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Codex notify payload is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Codex notify payload must be a JSON object")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall(_HEADER.pack(len(encoded)) + encoded)
        response = client.recv(64)

    status = response.decode("ascii", errors="replace").strip()
    if not status:
        raise ConnectionError("notify server returned an empty response")
    return status


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: codex-dispatch-notify <NOTIFICATION_JSON>", file=sys.stderr)
        return 64

    try:
        status = send_payload(args[0])
    except ValueError as exc:
        print(f"codex-dispatch-notify: invalid input/configuration: {exc}", file=sys.stderr)
        return 65
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, socket.timeout) as exc:
        print(
            f"codex-dispatch-notify: orchestrator unavailable: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 69
    except OSError as exc:
        print(
            f"codex-dispatch-notify: local socket error: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 69
    except ConnectionError as exc:
        print(f"codex-dispatch-notify: protocol error: {exc}", file=sys.stderr)
        return 70

    if status in {"ACK", "DUPLICATE"}:
        return 0

    print(f"codex-dispatch-notify: server rejected event: {status}", file=sys.stderr)
    return 70


if __name__ == "__main__":
    raise SystemExit(main())
