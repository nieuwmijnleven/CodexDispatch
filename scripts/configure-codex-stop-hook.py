#!/usr/bin/env python3
"""Install the Codex Dispatch Stop hook into the user's Codex hooks.json."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Merge the Codex Dispatch Stop hook into ~/.codex/hooks.json without "
            "removing unrelated user hooks."
        )
    )
    parser.add_argument(
        "--hooks-file",
        type=Path,
        default=None,
        help="override hooks.json path (primarily for testing)",
    )
    return parser.parse_args()


def default_hooks_file() -> Path:
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        return Path(codex_home).expanduser() / "hooks.json"
    return Path.home() / ".codex" / "hooks.json"


def hook_command(project_dir: Path) -> str:
    python = project_dir / ".venv" / "bin" / "python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError(f"Codex Dispatch virtualenv Python is unavailable: {python}")
    return f"{shlex.quote(str(python))} -m codex_dispatch.stop_hook_client"


def load_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"description": "Codex lifecycle hooks", "hooks": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"existing hooks.json is invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("existing hooks.json must contain a JSON object")
    hooks = raw.get("hooks")
    if hooks is None:
        raw["hooks"] = {}
    elif not isinstance(hooks, dict):
        raise RuntimeError("existing hooks.json 'hooks' value must be an object")
    return raw


def merge_stop_hook(document: dict[str, Any], command: str) -> tuple[dict[str, Any], bool]:
    hooks = document["hooks"]
    assert isinstance(hooks, dict)
    stop_groups = hooks.setdefault("Stop", [])
    if not isinstance(stop_groups, list):
        raise RuntimeError("existing Stop hooks must be a list")

    changed = False
    found = False
    for group in stop_groups:
        if not isinstance(group, dict):
            continue
        commands = group.get("hooks")
        if not isinstance(commands, list):
            continue
        for handler in commands:
            if not isinstance(handler, dict):
                continue
            existing = handler.get("command")
            if isinstance(existing, str) and "codex_dispatch.stop_hook_client" in existing:
                found = True
                desired = {"type": "command", "command": command}
                if handler != desired:
                    handler.clear()
                    handler.update(desired)
                    changed = True

    if not found:
        stop_groups.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": command,
                    }
                ]
            }
        )
        changed = True
    return document, changed


def atomic_write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    if os.geteuid() == 0:
        print("ERROR: run this command as the Codex user, not root", file=sys.stderr)
        return 77

    args = parse_args()
    project_dir = Path(__file__).resolve().parents[1]
    path = (args.hooks_file or default_hooks_file()).expanduser()
    if not path.is_absolute():
        path = path.resolve()

    try:
        command = hook_command(project_dir)
        document = load_document(path)
        document, changed = merge_stop_hook(document, command)
        if changed or not path.exists():
            atomic_write(path, document)
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Codex Dispatch Stop hook: {'UPDATED' if changed else 'ALREADY_CONFIGURED'}")
    print(f"hooks_file={path}")
    print("Restart any running Codex TUI, then review/trust the hook when Codex prompts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
