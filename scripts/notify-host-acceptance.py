#!/usr/bin/env python3
"""Wait for one real Codex notify event and report PHASE 3 host acceptance."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codex_dispatch.config import SettingsError, load_settings  # noqa: E402
from codex_dispatch.notify_server import (  # noqa: E402
    NotifyEvent,
    NotifyServer,
    NotifyServerError,
)


async def run(timeout_seconds: float) -> int:
    try:
        settings = load_settings()
        settings.require_notify()
    except SettingsError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    loop = asyncio.get_running_loop()
    received: asyncio.Future[NotifyEvent] = loop.create_future()

    async def capture(event: NotifyEvent) -> None:
        if not received.done():
            received.set_result(event)

    server = NotifyServer(
        settings.notify_socket_path,
        settings.allowed_roots,
        capture,
        max_payload_bytes=settings.notify_max_payload_bytes,
    )

    try:
        await server.start()
    except NotifyServerError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 3

    print(f"Listening on {settings.notify_socket_path}")
    print("Complete one Codex turn whose cwd is inside CODEX_ALLOWED_ROOTS.")
    print("The configured Codex notify command must invoke bin/codex-notify.py")

    try:
        event = await asyncio.wait_for(received, timeout=timeout_seconds)
    except TimeoutError:
        print("FAIL: no Codex notify event received before timeout", file=sys.stderr)
        return 4
    finally:
        await server.close()

    print("Codex Dispatch PHASE 3 acceptance: PASS")
    print(f"type={event.event_type}")
    print(f"thread-id={event.thread_id}")
    print(f"turn-id={event.turn_id}")
    print(f"cwd={event.cwd}")
    print(f"client={event.client or 'unknown'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="seconds to wait for one real Codex completion event (default: 120)",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return asyncio.run(run(args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
