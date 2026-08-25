#!/usr/bin/env python3
"""Read-only PHASE 7 acceptance check for persistent jobs and serialization."""

from __future__ import annotations

import argparse
import sys

from codex_dispatch.config import SettingsError, load_settings
from codex_dispatch.database import Database, DatabaseError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify PHASE 7 job persistence and one-active-job-per-session invariants."
    )
    parser.add_argument("--minimum-jobs", type=int, default=3)
    parser.add_argument("--minimum-queued", type=int, default=1)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.minimum_jobs <= 0 or args.minimum_queued < 0:
        print("FAIL: invalid minimum values", file=sys.stderr)
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
        sessions = database.list_sessions()
        jobs = []
        active_violation = False
        for session in sessions:
            session_jobs = database.list_jobs(session.thread_id, limit=100000)
            jobs.extend(session_jobs)
            if sum(job.status == "DISPATCHED" for job in session_jobs) > 1:
                active_violation = True
        schema_version = database.schema_version
    except DatabaseError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 3
    finally:
        database.close()

    queued = sum(job.status == "QUEUED" for job in jobs)
    dispatched = sum(job.status == "DISPATCHED" for job in jobs)
    completed = sum(job.status == "COMPLETED" for job in jobs)
    failed = sum(job.status == "FAILED" for job in jobs)
    cancelled = sum(job.status == "CANCELLED" for job in jobs)
    message_ids = [job.discord_message_id for job in jobs]

    print(f"schema_version={schema_version}")
    print(f"jobs_total={len(jobs)}")
    print(f"jobs_queued={queued}")
    print(f"jobs_dispatched={dispatched}")
    print(f"jobs_completed={completed}")
    print(f"jobs_failed={failed}")
    print(f"jobs_cancelled={cancelled}")

    if schema_version < 3:
        print("FAIL: schema version 3+ required", file=sys.stderr)
        return 1
    if len(jobs) < args.minimum_jobs:
        print(f"FAIL: need at least {args.minimum_jobs} jobs", file=sys.stderr)
        return 1
    if queued < args.minimum_queued:
        print(f"FAIL: need at least {args.minimum_queued} QUEUED jobs", file=sys.stderr)
        return 1
    if active_violation:
        print("FAIL: a session has more than one DISPATCHED job", file=sys.stderr)
        return 1
    if len(message_ids) != len(set(message_ids)):
        print("FAIL: duplicate Discord message IDs found", file=sys.stderr)
        return 1

    print("Codex Dispatch PHASE 7 acceptance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
