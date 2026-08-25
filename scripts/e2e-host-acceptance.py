#!/usr/bin/env python3
"""PHASE 12 read-only host acceptance plus reboot mapping baseline verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any


DEFAULT_ENV_FILE = Path("/etc/codex-dispatch/codex-dispatch.env")
SERVICE_NAME = "codex-dispatch.service"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify Codex Dispatch PHASE 12 E2E persistent state and optionally "
            "capture/verify a reboot baseline without reading prompt contents."
        )
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--minimum-sessions", type=int, default=3)
    parser.add_argument("--minimum-workspaces", type=int, default=2)
    parser.add_argument("--minimum-live", type=int, default=1)
    parser.add_argument("--minimum-managed", type=int, default=1)
    parser.add_argument("--minimum-completed-jobs", type=int, default=3)
    parser.add_argument("--minimum-sent-deliveries", type=int, default=3)
    parser.add_argument(
        "--require-auto-run",
        action="store_true",
        help="require at least one persisted auto_run=ON session",
    )
    parser.add_argument(
        "--capture-baseline",
        type=Path,
        help="write a privacy-minimal session-mapping baseline for pre-reboot capture",
    )
    parser.add_argument(
        "--verify-baseline",
        type=Path,
        help="verify a previously captured baseline after restart/reboot",
    )
    parser.add_argument(
        "--skip-systemd",
        action="store_true",
        help="skip systemctl active/enabled checks for non-systemd test hosts",
    )
    return parser.parse_args()


def read_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def ro_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def scalar(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = connection.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


def mapping_snapshot(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT thread_id, discord_thread_id
        FROM sessions
        WHERE discord_thread_id IS NOT NULL
        ORDER BY thread_id
        """
    ).fetchall()
    return {str(row["thread_id"]): int(row["discord_thread_id"]) for row in rows}


def mapping_digest(mapping: dict[str, int]) -> str:
    payload = json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def service_check(action: str) -> bool:
    result = subprocess.run(
        ["systemctl", action, "--quiet", SERVICE_NAME],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def write_baseline(path: Path, schema_version: int, mapping: dict[str, int]) -> None:
    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError("baseline path must be absolute")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = {
        "schema_version": schema_version,
        "mapping": mapping,
        "mapping_sha256": mapping_digest(mapping),
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def verify_baseline(path: Path, schema_version: int, mapping: dict[str, int]) -> tuple[bool, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return False, "baseline is not a JSON object"
    baseline_schema = data.get("schema_version")
    baseline_mapping = data.get("mapping")
    baseline_digest = data.get("mapping_sha256")
    if not isinstance(baseline_schema, int) or not isinstance(baseline_mapping, dict):
        return False, "baseline schema/mapping is invalid"
    normalized: dict[str, int] = {}
    for key, value in baseline_mapping.items():
        if not isinstance(key, str) or not isinstance(value, int):
            return False, "baseline mapping contains invalid values"
        normalized[key] = value
    if baseline_digest != mapping_digest(normalized):
        return False, "baseline mapping digest does not match file contents"
    if schema_version < baseline_schema:
        return False, "database schema regressed after restart/reboot"
    for thread_id, discord_thread_id in normalized.items():
        if mapping.get(thread_id) != discord_thread_id:
            return False, f"session mapping changed for {thread_id}"
    return True, "baseline session mappings preserved"


def main() -> int:
    args = parse_args()
    minimums = (
        args.minimum_sessions,
        args.minimum_workspaces,
        args.minimum_live,
        args.minimum_managed,
        args.minimum_completed_jobs,
        args.minimum_sent_deliveries,
    )
    if any(value < 0 for value in minimums):
        print("FAIL: minimum values must be zero or greater", file=sys.stderr)
        return 64

    env_file = args.env_file.expanduser().resolve(strict=False)
    if not env_file.is_file():
        print(f"FAIL: environment file missing: {env_file}", file=sys.stderr)
        return 2
    try:
        env = read_env_file(env_file)
    except OSError as exc:
        print(f"FAIL: cannot read environment file: {exc}", file=sys.stderr)
        return 2

    database_path = Path(
        env.get("CODEX_DISPATCH_DB_PATH", "/var/lib/codex-dispatch/codex-dispatch.db")
    ).expanduser()
    if not database_path.is_absolute() or not database_path.is_file():
        print(f"FAIL: database unavailable: {database_path}", file=sys.stderr)
        return 2

    try:
        with ro_connection(database_path) as connection:
            schema_version = scalar(connection, "PRAGMA user_version")
            sessions = scalar(connection, "SELECT COUNT(*) FROM sessions")
            mapped = scalar(
                connection,
                "SELECT COUNT(*) FROM sessions WHERE discord_thread_id IS NOT NULL",
            )
            live = scalar(connection, "SELECT COUNT(*) FROM sessions WHERE mode = 'LIVE'")
            managed = scalar(connection, "SELECT COUNT(*) FROM sessions WHERE mode = 'MANAGED'")
            auto_run = scalar(connection, "SELECT COUNT(*) FROM sessions WHERE auto_run = 1")
            workspaces = scalar(connection, "SELECT COUNT(*) FROM workspaces WHERE enabled = 1")
            completed = scalar(connection, "SELECT COUNT(*) FROM jobs WHERE status = 'COMPLETED'")
            queued = scalar(connection, "SELECT COUNT(*) FROM jobs WHERE status = 'QUEUED'")
            dispatched = scalar(connection, "SELECT COUNT(*) FROM jobs WHERE status = 'DISPATCHED'")
            failed = scalar(connection, "SELECT COUNT(*) FROM jobs WHERE status = 'FAILED'")
            sent = scalar(connection, "SELECT COUNT(*) FROM discord_deliveries WHERE status = 'SENT'")
            active_violations = scalar(
                connection,
                """
                SELECT COUNT(*) FROM (
                    SELECT thread_id
                    FROM jobs
                    WHERE status = 'DISPATCHED'
                    GROUP BY thread_id
                    HAVING COUNT(*) > 1
                )
                """,
            )
            duplicate_threads = scalar(
                connection,
                """
                SELECT COUNT(*) FROM (
                    SELECT discord_thread_id
                    FROM sessions
                    WHERE discord_thread_id IS NOT NULL
                    GROUP BY discord_thread_id
                    HAVING COUNT(*) > 1
                )
                """,
            )
            duplicate_messages = scalar(
                connection,
                """
                SELECT COUNT(*) FROM (
                    SELECT discord_message_id
                    FROM jobs
                    GROUP BY discord_message_id
                    HAVING COUNT(*) > 1
                )
                """,
            )
            duplicate_notify = scalar(
                connection,
                """
                SELECT COUNT(*) FROM (
                    SELECT thread_id, turn_id, event_type
                    FROM notify_events
                    GROUP BY thread_id, turn_id, event_type
                    HAVING COUNT(*) > 1
                )
                """,
            )
            mapping = mapping_snapshot(connection)
    except (sqlite3.Error, OSError) as exc:
        print(f"FAIL: cannot inspect persistent state: {exc}", file=sys.stderr)
        return 3

    checks: list[tuple[str, bool]] = [
        ("schema_v4_or_newer", schema_version >= 4),
        ("minimum_sessions", sessions >= args.minimum_sessions),
        ("all_sessions_mapped", mapped == sessions),
        ("minimum_workspaces", workspaces >= args.minimum_workspaces),
        ("minimum_live", live >= args.minimum_live),
        ("minimum_managed", managed >= args.minimum_managed),
        ("minimum_completed_jobs", completed >= args.minimum_completed_jobs),
        ("minimum_sent_deliveries", sent >= args.minimum_sent_deliveries),
        ("single_active_job_per_session", active_violations == 0),
        ("unique_discord_thread_mapping", duplicate_threads == 0),
        ("unique_discord_message_ids", duplicate_messages == 0),
        ("unique_notify_identity", duplicate_notify == 0),
    ]
    if args.require_auto_run:
        checks.append(("auto_run_session_present", auto_run >= 1))
    if not args.skip_systemd:
        checks.extend(
            [
                ("systemd_enabled", service_check("is-enabled")),
                ("systemd_active", service_check("is-active")),
            ]
        )

    if args.capture_baseline is not None:
        try:
            write_baseline(args.capture_baseline, schema_version, mapping)
        except (OSError, ValueError) as exc:
            print(f"FAIL: cannot write baseline: {exc}", file=sys.stderr)
            return 4
        print(f"baseline_captured={args.capture_baseline}")

    if args.verify_baseline is not None:
        try:
            baseline_ok, baseline_message = verify_baseline(
                args.verify_baseline.expanduser().resolve(strict=False),
                schema_version,
                mapping,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"FAIL: cannot verify baseline: {exc}", file=sys.stderr)
            return 4
        checks.append(("reboot_baseline", baseline_ok))
        print(f"baseline_result={baseline_message}")

    for name, passed in checks:
        print(f"{name}={'PASS' if passed else 'FAIL'}")
    print(f"schema_version={schema_version}")
    print(f"sessions_total={sessions}")
    print(f"sessions_live={live}")
    print(f"sessions_managed={managed}")
    print(f"sessions_auto_run={auto_run}")
    print(f"workspaces_enabled={workspaces}")
    print(f"jobs_completed={completed}")
    print(f"jobs_queued={queued}")
    print(f"jobs_dispatched={dispatched}")
    print(f"jobs_failed={failed}")
    print(f"deliveries_sent={sent}")
    print(f"mapping_sha256={mapping_digest(mapping)}")

    if not all(passed for _, passed in checks):
        print("Codex Dispatch PHASE 12 acceptance: FAIL", file=sys.stderr)
        return 1
    print("Codex Dispatch PHASE 12 acceptance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
