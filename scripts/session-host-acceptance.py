#!/usr/bin/env python3
"""Read-only PHASE 4 host acceptance against the persistent SQLite state."""

from __future__ import annotations

import argparse
import sys

from codex_dispatch.config import SettingsError, load_settings
from codex_dispatch.database import Database, DatabaseError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that persistent Codex sessions have unique Discord thread mappings. "
            "Run after generating real Codex notify events."
        )
    )
    parser.add_argument(
        "--minimum-sessions",
        type=int,
        default=3,
        help="minimum mapped sessions required for PASS (default: 3)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.minimum_sessions <= 0:
        print("FAIL: --minimum-sessions must be positive", file=sys.stderr)
        return 64

    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    if not settings.database_path.exists():
        print(
            f"FAIL: database does not exist: {settings.database_path}",
            file=sys.stderr,
        )
        return 2

    database = Database(settings.database_path)
    try:
        database.open()
        schema_version = database.schema_version
        sessions = database.list_sessions()
    except DatabaseError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 3
    finally:
        database.close()

    mapped = [session for session in sessions if session.discord_thread_id is not None]
    mapped_ids = [session.discord_thread_id for session in mapped]
    unique_ids = set(mapped_ids)

    print(f"schema_version={schema_version}")
    print(f"sessions_total={len(sessions)}")
    print(f"sessions_mapped={len(mapped)}")
    print(f"unique_discord_threads={len(unique_ids)}")

    if len(mapped) < args.minimum_sessions:
        print(
            f"FAIL: need at least {args.minimum_sessions} persisted mapped sessions",
            file=sys.stderr,
        )
        return 1
    if len(unique_ids) != len(mapped_ids):
        print("FAIL: duplicate Discord thread mapping detected", file=sys.stderr)
        return 1

    print("Codex Dispatch PHASE 4 acceptance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
