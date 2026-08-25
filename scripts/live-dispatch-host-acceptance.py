#!/usr/bin/env python3
"""Active PHASE 6 host acceptance for Codex live-session queue dispatch."""

from __future__ import annotations

import argparse
import asyncio
import sys

from codex_dispatch.codex_runner import CodexQueueRunner, CodexRunnerError
from codex_dispatch.config import SettingsError, load_settings
from codex_dispatch.database import Database, DatabaseError


PROBE_PROMPT = (
    "Codex Dispatch PHASE 6 acceptance probe. Do not modify files. "
    "Reply exactly: CODEX_DISPATCH_PHASE6_OK"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Queue a harmless probe into one persisted LIVE Codex session. "
            "The target Codex session must currently be reachable by codex queue."
        )
    )
    parser.add_argument(
        "--thread",
        required=True,
        help="persisted Codex thread-id to probe",
    )
    return parser.parse_args()


async def run_probe(thread_id: str) -> int:
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    database = Database(settings.database_path)
    try:
        database.open()
        session = database.get_session(thread_id)
        if session is None:
            print("FAIL: Codex thread is not persisted in the session database", file=sys.stderr)
            return 1
        if session.mode != "LIVE":
            print(f"FAIL: session mode is {session.mode}, expected LIVE", file=sys.stderr)
            return 1
        workspace = database.get_workspace(session.workspace_id)
        if workspace is None or not workspace.enabled:
            print("FAIL: session workspace is unavailable", file=sys.stderr)
            return 1

        runner = CodexQueueRunner(
            settings.codex_binary,
            queue_timeout_seconds=settings.codex_queue_timeout_seconds,
            capability_timeout_seconds=settings.codex_capability_timeout_seconds,
            prompt_max_chars=settings.codex_prompt_max_chars,
        )
        await runner.verify_capability()
        await runner.queue_message(thread_id, PROBE_PROMPT, cwd=workspace.path)
    except (DatabaseError, CodexRunnerError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 3
    finally:
        database.close()

    print("Codex Dispatch PHASE 6 queue command: PASS")
    print(f"thread_id={thread_id}")
    print("Expected Codex response: CODEX_DISPATCH_PHASE6_OK")
    print(
        "For full E2E acceptance, send the same probe from the mapped Discord Thread "
        "and confirm the Codex completion returns to that Thread."
    )
    return 0


def main() -> int:
    args = parse_args()
    return asyncio.run(run_probe(args.thread))


if __name__ == "__main__":
    raise SystemExit(main())
