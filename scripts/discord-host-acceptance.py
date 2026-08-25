#!/usr/bin/env python3
"""Interactive PHASE 2 Discord host acceptance check.

Run this as the same Linux user that will run Codex Dispatch. The script connects
using the configured Bot token, waits for one authorized text message, replies in
that same channel/thread, and exits successfully.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from codex_dispatch.adapters.discord import (  # noqa: E402
    DiscordAdapter,
    DiscordAdapterError,
    DiscordIncomingMessage,
)
from codex_dispatch.config import SettingsError, load_settings  # noqa: E402
from codex_dispatch.logging import configure_logging  # noqa: E402


async def run_acceptance(timeout: float) -> int:
    settings = load_settings()
    settings.require_discord()
    configure_logging(
        settings.log_level,
        secrets=(settings.discord_bot_token or "",),
    )

    received = asyncio.Event()
    inbound: DiscordIncomingMessage | None = None
    adapter: DiscordAdapter

    async def on_message(message: DiscordIncomingMessage) -> None:
        nonlocal inbound
        if inbound is not None:
            return
        inbound = message
        await adapter.send_text(
            message.channel_id,
            "Codex Dispatch PHASE 2 acceptance: PASS",
        )
        received.set()

    adapter = DiscordAdapter(settings, on_message)
    task = asyncio.create_task(adapter.start())

    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while adapter.client is None:
            if task.done():
                await task
                raise DiscordAdapterError("Discord adapter stopped before connecting")
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("Discord client creation timed out")
            await asyncio.sleep(0.05)

        remaining = max(0.1, deadline - asyncio.get_running_loop().time())
        await asyncio.wait_for(adapter.client.wait_until_ready(), timeout=remaining)
        print("Discord Gateway connected.")
        print("Send one text message from an allowed user in an allowed channel/thread.")

        remaining = max(0.1, deadline - asyncio.get_running_loop().time())
        await asyncio.wait_for(received.wait(), timeout=remaining)
        assert inbound is not None
        print(
            "PASS: authorized inbound message received and outbound reply sent "
            f"(guild={inbound.guild_id}, channel={inbound.channel_id}, "
            f"user={inbound.user_id})."
        )
        return 0
    finally:
        await adapter.close()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="overall connection/message timeout in seconds (default: 120)",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    try:
        return asyncio.run(run_acceptance(args.timeout))
    except (SettingsError, DiscordAdapterError, TimeoutError, asyncio.TimeoutError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
