#!/usr/bin/env python3
"""PHASE 8 host probe for real ``codex exec resume`` behavior."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

from codex_dispatch.config import SettingsError, load_settings
from codex_dispatch.managed_runner import CodexManagedRunner


PROBE = (
    "Do not modify any files. Reply with exactly CODEX_DISPATCH_PHASE8_OK and "
    "nothing else."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resume one real Codex thread through the PHASE 8 managed runner."
    )
    parser.add_argument("--thread", required=True, help="existing durable Codex thread ID")
    parser.add_argument("--cwd", required=True, help="absolute workspace path for the thread")
    return parser.parse_args()


def allowed_workspace(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve(strict=True)
    for root in roots:
        try:
            resolved.relative_to(root.resolve(strict=True))
            return True
        except ValueError:
            continue
    return False


async def run() -> int:
    args = parse_args()
    try:
        settings = load_settings()
        settings.require_notify()
    except (SettingsError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    cwd = Path(args.cwd).expanduser()
    if not cwd.is_absolute() or not cwd.is_dir():
        print("FAIL: --cwd must be an existing absolute directory", file=sys.stderr)
        return 64
    try:
        if not allowed_workspace(cwd, settings.allowed_roots):
            print("FAIL: --cwd is outside CODEX_ALLOWED_ROOTS", file=sys.stderr)
            return 64
    except OSError as exc:
        print(f"FAIL: cannot resolve workspace: {exc}", file=sys.stderr)
        return 64

    runner = CodexManagedRunner(
        settings.codex_binary,
        timeout_seconds=settings.managed_exec_timeout_seconds,
        capability_timeout_seconds=settings.codex_capability_timeout_seconds,
        prompt_max_chars=settings.codex_prompt_max_chars,
        output_max_bytes=settings.managed_output_max_bytes,
    )
    try:
        await runner.verify_capability()
        result = await runner.resume(args.thread, PROBE, cwd=cwd.resolve())
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await runner.close()

    print("managed_capability=PASS")
    print(f"returncode={result.returncode}")
    print(f"stdout_truncated={str(result.stdout_truncated).lower()}")
    print(f"stderr_truncated={str(result.stderr_truncated).lower()}")
    print("stdout:")
    print(result.stdout.strip())
    if "CODEX_DISPATCH_PHASE8_OK" not in result.stdout:
        print(
            "FAIL: expected CODEX_DISPATCH_PHASE8_OK was not observed in stdout",
            file=sys.stderr,
        )
        return 1
    print("Codex Dispatch PHASE 8 managed runner acceptance: PASS")
    print("NEXT: verify external notify and mapped Discord completion for the same thread.")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
