#!/usr/bin/env python3
"""PHASE 5 host acceptance for persisted Discord completion delivery."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from codex_dispatch.config import SettingsError, load_settings
from codex_dispatch.database import Database, DatabaseError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify persisted Codex completion -> Discord delivery state",
    )
    parser.add_argument(
        "--minimum-sent",
        type=int,
        default=1,
        help="minimum confirmed SENT deliveries required (default: 1)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.minimum_sent <= 0:
        print("FAIL: --minimum-sent must be positive", file=sys.stderr)
        return 2

    try:
        settings = load_settings(os.environ)
        database = Database(Path(settings.database_path))
        database.open()
    except (SettingsError, DatabaseError, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    try:
        deliveries = database.list_discord_deliveries()
        sent = tuple(item for item in deliveries if item.status == "SENT")
        pending = tuple(item for item in deliveries if item.status == "PENDING")
        sending = tuple(item for item in deliveries if item.status == "SENDING")
        failed = tuple(item for item in deliveries if item.status == "FAILED")

        print(f"schema_version={database.schema_version}")
        print(f"deliveries_total={len(deliveries)}")
        print(f"deliveries_sent={len(sent)}")
        print(f"deliveries_pending={len(pending)}")
        print(f"deliveries_sending={len(sending)}")
        print(f"deliveries_failed={len(failed)}")
        if sent:
            latest = sent[-1]
            print(f"latest_sent_event_id={latest.notify_event_id}")
            print(f"latest_discord_message_id={latest.discord_message_id}")
            print(f"latest_attempts={latest.attempts}")

        if len(sent) < args.minimum_sent:
            print(
                "FAIL: not enough confirmed Discord completion deliveries",
                file=sys.stderr,
            )
            return 1
        if any(item.discord_message_id is None for item in sent):
            print("FAIL: a SENT delivery has no Discord message id", file=sys.stderr)
            return 1

        print("Codex Dispatch PHASE 5 acceptance: PASS")
        return 0
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
