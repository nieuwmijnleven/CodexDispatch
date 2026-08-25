#!/usr/bin/env python3
"""Read-only PHASE 11 host security acceptance for a deployed Codex Dispatch service."""

from __future__ import annotations

import argparse
import grp
import os
from pathlib import Path
import pwd
import socket
import stat
import subprocess
import sys

from codex_dispatch.security import RuntimeSecurityError, validate_runtime_security


SERVICE_NAME = "codex-dispatch.service"
DEFAULT_ENV_FILE = Path("/etc/codex-dispatch/codex-dispatch.env")
DEFAULT_NOTIFY_ENV_FILE = Path("/etc/codex-dispatch/notify.env")
DEFAULT_SECRET_ENV_FILE = Path("/etc/codex-dispatch/secret.env")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
_NOTIFY_KEYS = {
    "CODEX_DISPATCH_NOTIFY_SOCKET",
    "CODEX_DISPATCH_NOTIFY_MAX_BYTES",
    "CODEX_DISPATCH_NOTIFY_TIMEOUT_SECONDS",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify PHASE 11 deployment permissions, isolation, and secret hygiene."
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--notify-env-file", type=Path, default=DEFAULT_NOTIFY_ENV_FILE)
    parser.add_argument("--secret-env-file", type=Path, default=DEFAULT_SECRET_ENV_FILE)
    parser.add_argument("--journal-lines", type=int, default=500)
    parser.add_argument("--skip-journal", action="store_true")
    return parser.parse_args()


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def service_properties() -> dict[str, str]:
    names = (
        "User",
        "Group",
        "NoNewPrivileges",
        "PrivateTmp",
        "ProtectSystem",
        "ProtectKernelTunables",
        "ProtectKernelModules",
        "ProtectControlGroups",
        "ProtectHostname",
        "RestrictSUIDSGID",
        "LockPersonality",
    )
    command = ["systemctl", "show", SERVICE_NAME]
    for name in names:
        command.append(f"--property={name}")
    result = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def owned_private_file(path: Path, uid: int, expected_mode: int) -> bool:
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_uid == uid and stat.S_IMODE(info.st_mode) == expected_mode


def source_shell_audit() -> bool:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((PROJECT_ROOT / "src").rglob("*.py"))
    )
    forbidden = (
        "create_subprocess_shell",
        "shell=True",
        "os.system(",
        "subprocess.call(",
        "subprocess.Popen(",
    )
    return all(value not in source for value in forbidden) and "create_subprocess_exec" in source


def journal_has_secret(token: str, lines: int) -> bool:
    result = subprocess.run(
        [
            "journalctl",
            "-u",
            SERVICE_NAME,
            "-n",
            str(lines),
            "--no-pager",
            "--output=cat",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return bool(token) and token in result.stdout


def main() -> int:
    args = parse_args()
    if args.journal_lines <= 0:
        print("FAIL: --journal-lines must be positive", file=sys.stderr)
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
        env = read_env(env_file)
        notify_env = read_env(notify_env_file)
        secret_env = read_env(secret_env_file)
        properties = service_properties()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"FAIL: cannot inspect deployment: {type(exc).__name__}", file=sys.stderr)
        return 2

    service_user = properties.get("User", "")
    service_group = properties.get("Group", "")
    if not service_user or service_user == "root" or not service_group:
        print("FAIL: service must have an explicit non-root user/group", file=sys.stderr)
        return 1
    try:
        account = pwd.getpwnam(service_user)
        group = grp.getgrnam(service_group)
    except KeyError:
        print("FAIL: service user/group does not exist", file=sys.stderr)
        return 1

    token = secret_env.get("DISCORD_BOT_TOKEN", "")
    database = Path(env.get("CODEX_DISPATCH_DB_PATH", ""))
    notify_socket = Path(notify_env.get("CODEX_DISPATCH_NOTIFY_SOCKET", ""))
    codex_binary = env.get("CODEX_DISPATCH_CODEX_BIN", "")
    raw_roots = env.get("CODEX_ALLOWED_ROOTS", "")
    allowed_roots = tuple(
        Path(item.strip()) for item in raw_roots.split(os.pathsep) if item.strip()
    )

    env_stat = env_file.stat()
    env_mode = stat.S_IMODE(env_stat.st_mode)
    env_permission_ok = (
        env_stat.st_uid == 0
        and env_stat.st_gid == group.gr_gid
        and env_mode == 0o640
        and "DISCORD_BOT_TOKEN" not in env
    )
    notify_env_stat = notify_env_file.stat()
    notify_env_mode = stat.S_IMODE(notify_env_stat.st_mode)
    notify_env_permission_ok = (
        notify_env_stat.st_uid == 0
        and notify_env_stat.st_gid == group.gr_gid
        and notify_env_mode == 0o640
    )
    notify_env_nonsecret = (
        bool(notify_env)
        and set(notify_env) <= _NOTIFY_KEYS
        and "DISCORD_BOT_TOKEN" not in notify_env
    )
    secret_env_stat = secret_env_file.stat()
    secret_env_permission_ok = (
        secret_env_stat.st_uid == 0
        and secret_env_stat.st_gid == 0
        and stat.S_IMODE(secret_env_stat.st_mode) == 0o600
        and set(secret_env) <= {"DISCORD_BOT_TOKEN"}
    )

    socket_ok = False
    runtime_ok = False
    try:
        socket_stat = notify_socket.stat()
        socket_ok = (
            stat.S_ISSOCK(socket_stat.st_mode)
            and socket_stat.st_uid == account.pw_uid
            and stat.S_IMODE(socket_stat.st_mode) == 0o600
        )
        runtime_stat = notify_socket.parent.stat()
        runtime_ok = (
            stat.S_ISDIR(runtime_stat.st_mode)
            and runtime_stat.st_uid == account.pw_uid
            and stat.S_IMODE(runtime_stat.st_mode) == 0o700
        )
    except OSError:
        pass

    codex_path = Path(codex_binary).expanduser()
    codex_binary_ok = False
    if codex_path.is_absolute():
        try:
            binary_stat = codex_path.stat()
            codex_binary_ok = (
                stat.S_ISREG(binary_stat.st_mode)
                and bool(binary_stat.st_mode & 0o111)
                and not bool(binary_stat.st_mode & 0o022)
                and binary_stat.st_uid in {0, account.pw_uid}
            )
        except OSError:
            pass

    runtime_boundary_ok = True
    runtime_boundary_error = ""
    try:
        validate_runtime_security(
            allowed_roots=allowed_roots,
            database_path=database,
            codex_binary=codex_binary,
            project_root=PROJECT_ROOT,
            home=Path(account.pw_dir),
        )
    except RuntimeSecurityError as exc:
        runtime_boundary_ok = False
        runtime_boundary_error = str(exc)

    sandbox_expectations = {
        "NoNewPrivileges": "yes",
        "PrivateTmp": "yes",
        "ProtectSystem": "full",
        "ProtectKernelTunables": "yes",
        "ProtectKernelModules": "yes",
        "ProtectControlGroups": "yes",
        "ProtectHostname": "yes",
        "RestrictSUIDSGID": "yes",
        "LockPersonality": "yes",
    }
    sandbox_ok = all(properties.get(key) == value for key, value in sandbox_expectations.items())

    journal_ok = True
    if not args.skip_journal:
        try:
            journal_ok = not journal_has_secret(token, args.journal_lines)
        except (OSError, subprocess.CalledProcessError):
            journal_ok = False

    checks = [
        ("service_non_root", service_user != "root"),
        ("discord_token_present", bool(token)),
        ("config_env_permissions", env_permission_ok),
        ("secret_env_permissions", secret_env_permission_ok),
        ("notify_env_permissions", notify_env_permission_ok),
        ("notify_env_nonsecret", notify_env_nonsecret),
        ("database_permissions", owned_private_file(database, account.pw_uid, 0o600)),
        ("runtime_directory_permissions", runtime_ok),
        ("notify_socket_permissions", socket_ok),
        ("codex_binary_integrity", codex_binary_ok),
        ("workspace_runtime_boundaries", runtime_boundary_ok),
        ("systemd_sandbox", sandbox_ok),
        ("source_shell_audit", source_shell_audit()),
        ("journal_secret_scan", journal_ok),
    ]

    for name, passed in checks:
        print(f"{name}={'PASS' if passed else 'FAIL'}")
    if runtime_boundary_error:
        print(f"workspace_runtime_boundary_error={runtime_boundary_error}")
    print(f"service_user={service_user}")
    print(f"allowed_roots={len(allowed_roots)}")
    print(f"journal_lines_checked={0 if args.skip_journal else args.journal_lines}")

    if not all(passed for _, passed in checks):
        print("Codex Dispatch PHASE 11 acceptance: FAIL", file=sys.stderr)
        return 1
    print("Codex Dispatch PHASE 11 acceptance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
