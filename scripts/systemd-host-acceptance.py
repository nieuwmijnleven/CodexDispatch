#!/usr/bin/env python3
"""PHASE 10 host acceptance for systemd deployment and restart recovery."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time


SERVICE_NAME = "codex-dispatch.service"
DEFAULT_ENV_FILE = Path("/etc/codex-dispatch/codex-dispatch.env")
DEFAULT_NOTIFY_ENV_FILE = Path("/etc/codex-dispatch/notify.env")
DEFAULT_SECRET_ENV_FILE = Path("/etc/codex-dispatch/secret.env")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify Codex Dispatch systemd service, permissions, and restart recovery."
    )
    parser.add_argument("--restart", action="store_true", help="restart the service and verify DB mappings survive")
    parser.add_argument("--minimum-sessions", type=int, default=1)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--notify-env-file", type=Path, default=DEFAULT_NOTIFY_ENV_FILE)
    parser.add_argument("--secret-env-file", type=Path, default=DEFAULT_SECRET_ENV_FILE)
    return parser.parse_args()


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def snapshot(database_path: Path) -> tuple[int, tuple[tuple[str, int | None, str, bool], ...]]:
    uri = f"file:{database_path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        rows = connection.execute(
            "SELECT thread_id, discord_thread_id, mode, auto_run FROM sessions ORDER BY thread_id"
        ).fetchall()
        sessions = tuple(
            (
                str(row[0]),
                int(row[1]) if row[1] is not None else None,
                str(row[2]),
                bool(row[3]),
            )
            for row in rows
        )
        return version, sessions
    finally:
        connection.close()


def wait_active(socket_path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active = systemctl("is-active", "--quiet", SERVICE_NAME, check=False).returncode == 0
        if active and socket_path.exists():
            return
        time.sleep(0.25)
    raise RuntimeError("service did not become active with notify socket in time")


def main() -> int:
    args = parse_args()
    if args.minimum_sessions < 0:
        print("FAIL: --minimum-sessions must be zero or greater", file=sys.stderr)
        return 64

    env_file = args.env_file.resolve(strict=False)
    notify_env_file = args.notify_env_file.resolve(strict=False)
    secret_env_file = args.secret_env_file.resolve(strict=False)
    if not env_file.is_file():
        print(f"FAIL: environment file missing: {env_file}", file=sys.stderr)
        return 2
    if not notify_env_file.is_file():
        print(f"FAIL: notify environment file missing: {notify_env_file}", file=sys.stderr)
        return 2
    if not secret_env_file.is_file():
        print(f"FAIL: secret environment file missing: {secret_env_file}", file=sys.stderr)
        return 2
    try:
        env = read_env_file(env_file)
        notify_env = read_env_file(notify_env_file)
    except OSError as exc:
        print(f"FAIL: cannot read environment file: {exc}", file=sys.stderr)
        return 2

    database_path = Path(
        env.get("CODEX_DISPATCH_DB_PATH", "/var/lib/codex-dispatch/codex-dispatch.db")
    )
    socket_path = Path(
        notify_env.get("CODEX_DISPATCH_NOTIFY_SOCKET", "/run/codex-dispatch/notify.sock")
    )
    if not database_path.is_absolute() or not socket_path.is_absolute():
        print("FAIL: DB and notify socket paths must be absolute", file=sys.stderr)
        return 2

    enabled = systemctl("is-enabled", "--quiet", SERVICE_NAME, check=False).returncode == 0
    active = systemctl("is-active", "--quiet", SERVICE_NAME, check=False).returncode == 0
    show = systemctl(
        "show",
        SERVICE_NAME,
        "--property=User",
        "--property=Group",
        "--property=Restart",
        "--property=KillMode",
        "--property=RuntimeDirectory",
        "--property=StateDirectory",
    )
    properties = dict(
        line.split("=", 1) for line in show.stdout.splitlines() if "=" in line
    )
    service_user = properties.get("User", "")

    checks: list[tuple[str, bool]] = [
        ("enabled", enabled),
        ("active", active),
        ("non_root_user", bool(service_user) and service_user != "root"),
        ("restart_always", properties.get("Restart") == "always"),
        ("kill_mode_mixed", properties.get("KillMode") == "mixed"),
        ("config_env_mode_0640", mode(env_file) == 0o640),
        ("notify_env_mode_0640", mode(notify_env_file) == 0o640),
        ("secret_env_mode_0600", mode(secret_env_file) == 0o600),
        ("runtime_directory", socket_path.parent.is_dir() and mode(socket_path.parent) == 0o700),
        ("notify_socket", socket_path.exists() and stat.S_ISSOCK(socket_path.stat().st_mode) and mode(socket_path) == 0o600),
        ("database", database_path.is_file() and mode(database_path) == 0o600),
    ]

    try:
        before_version, before_sessions = snapshot(database_path)
    except (sqlite3.Error, OSError) as exc:
        print(f"FAIL: cannot inspect database: {exc}", file=sys.stderr)
        return 3

    if len(before_sessions) < args.minimum_sessions:
        checks.append(("minimum_sessions", False))
    else:
        checks.append(("minimum_sessions", True))

    if args.restart:
        if os.geteuid() != 0:
            print("FAIL: --restart requires root/sudo", file=sys.stderr)
            return 77
        try:
            systemctl("restart", SERVICE_NAME)
            wait_active(socket_path)
            after_version, after_sessions = snapshot(database_path)
        except (subprocess.CalledProcessError, RuntimeError, sqlite3.Error, OSError) as exc:
            print(f"FAIL: restart recovery failed: {exc}", file=sys.stderr)
            return 4
        checks.append(("schema_preserved_after_restart", after_version == before_version))
        checks.append(("session_mappings_preserved_after_restart", after_sessions == before_sessions))

    for name, passed in checks:
        print(f"{name}={'PASS' if passed else 'FAIL'}")
    print(f"schema_version={before_version}")
    print(f"sessions_total={len(before_sessions)}")
    print(f"service_user={service_user or 'unknown'}")
    print(f"notify_socket={socket_path}")
    print(f"database={database_path}")

    if not all(passed for _, passed in checks):
        print("Codex Dispatch PHASE 10 acceptance: FAIL", file=sys.stderr)
        return 1
    print("Codex Dispatch PHASE 10 acceptance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
