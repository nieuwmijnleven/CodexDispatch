#!/usr/bin/env python3
"""Read-only PHASE 9 acceptance check for persistent auto-run state."""

from __future__ import annotations

import argparse
import sys

from codex_dispatch.config import SettingsError, load_settings
from codex_dispatch.database import Database, DatabaseError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify PHASE 9 auto-run persistence and sequential completion for one "
            "Codex thread. Run after a real Discord auto-run E2E test."
        )
    )
    parser.add_argument("--thread", required=True, help="Codex thread-id to verify")
    parser.add_argument(
        "--minimum-completed",
        type=int,
        default=3,
        help="minimum completed jobs required for PASS (default: 3)",
    )
    parser.add_argument(
        "--expect-auto",
        choices=("on", "off"),
        default="on",
        help="expected persisted auto-run state (default: on)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.minimum_completed <= 0:
        print("FAIL: --minimum-completed must be positive", file=sys.stderr)
        return 64

    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    if not settings.database_path.exists():
        print(f"FAIL: database does not exist: {settings.database_path}", file=sys.stderr)
        return 2

    database = Database(settings.database_path)
    try:
        database.open()
        schema_version = database.schema_version
        session = database.get_session(args.thread)
        if session is None:
            print(f"FAIL: Codex thread is not persisted: {args.thread}", file=sys.stderr)
            return 1
        jobs = database.list_jobs(args.thread, limit=100000)
    except DatabaseError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 3
    finally:
        database.close()

    completed = sum(job.status == "COMPLETED" for job in jobs)
    queued = sum(job.status == "QUEUED" for job in jobs)
    dispatched = sum(job.status == "DISPATCHED" for job in jobs)
    failed = sum(job.status == "FAILED" for job in jobs)
    cancelled = sum(job.status == "CANCELLED" for job in jobs)
    expected_auto = args.expect_auto == "on"

    print(f"schema_version={schema_version}")
    print(f"thread_id={session.thread_id}")
    print(f"mode={session.mode}")
    print(f"status={session.status}")
    print(f"auto_run={'on' if session.auto_run else 'off'}")
    print(f"jobs_total={len(jobs)}")
    print(f"jobs_completed={completed}")
    print(f"jobs_queued={queued}")
    print(f"jobs_dispatched={dispatched}")
    print(f"jobs_failed={failed}")
    print(f"jobs_cancelled={cancelled}")

    if schema_version < 4:
        print("FAIL: schema version 4+ required", file=sys.stderr)
        return 1
    if session.auto_run != expected_auto:
        print(
            f"FAIL: expected auto_run={args.expect_auto}, got "
            f"{'on' if session.auto_run else 'off'}",
            file=sys.stderr,
        )
        return 1
    if completed < args.minimum_completed:
        print(
            f"FAIL: need at least {args.minimum_completed} COMPLETED jobs",
            file=sys.stderr,
        )
        return 1
    if dispatched > 1:
        print("FAIL: more than one DISPATCHED job exists", file=sys.stderr)
        return 1
    if expected_auto and session.status == "IDLE" and queued:
        print(
            "FAIL: auto-run is ON but an idle session still has queued jobs",
            file=sys.stderr,
        )
        return 1

    print("Codex Dispatch PHASE 9 acceptance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
