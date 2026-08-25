from __future__ import annotations

import ast
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from codex_dispatch.notify_server import NotifyValidationError, parse_notify_payload
from codex_dispatch.security import (
    RuntimeSecurityError,
    WorkspaceSecurityError,
    redact_sensitive_text,
    resolve_allowed_workspace,
    validate_runtime_security,
)


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "codex_dispatch"


class WorkspaceSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.allowed = self.base / "allowed"
        self.workspace = self.allowed / "project"
        self.outside = self.base / "outside"
        self.workspace.mkdir(parents=True)
        self.outside.mkdir()

    def test_realpath_workspace_inside_root_is_allowed(self) -> None:
        resolved = resolve_allowed_workspace(self.workspace, (self.allowed,))
        self.assertEqual(resolved, self.workspace.resolve())

    def test_workspace_outside_root_is_rejected(self) -> None:
        with self.assertRaisesRegex(WorkspaceSecurityError, "outside"):
            resolve_allowed_workspace(self.outside, (self.allowed,))

    def test_symlink_escape_is_rejected(self) -> None:
        link = self.allowed / "escape"
        link.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaisesRegex(WorkspaceSecurityError, "outside"):
            resolve_allowed_workspace(link, (self.allowed,))

    def test_relative_and_missing_workspace_are_rejected(self) -> None:
        with self.assertRaisesRegex(WorkspaceSecurityError, "absolute"):
            resolve_allowed_workspace("relative/path", (self.allowed,))
        with self.assertRaisesRegex(WorkspaceSecurityError, "existing"):
            resolve_allowed_workspace(self.allowed / "missing", (self.allowed,))


class RuntimeBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "service" / "codex-dispatch"
        self.project.mkdir(parents=True)
        self.workspaces = self.base / "workspaces"
        self.workspaces.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()
        self.binary = self.base / "bin" / "codex"
        self.binary.parent.mkdir()
        self.binary.write_text("fake", encoding="utf-8")
        self.database = self.base / "state" / "codex-dispatch.db"

    def test_safe_separate_workspace_root_is_allowed(self) -> None:
        roots = validate_runtime_security(
            allowed_roots=(self.workspaces,),
            database_path=self.database,
            codex_binary=str(self.binary),
            project_root=self.project,
            home=self.home,
        )
        self.assertEqual(roots, (self.workspaces.resolve(),))

    def test_root_covering_dispatcher_or_credentials_is_rejected(self) -> None:
        for allowed, label in (
            (self.base, "Codex Dispatch project"),
            (self.home, "Codex credentials"),
        ):
            with self.subTest(allowed=allowed), self.assertRaisesRegex(
                RuntimeSecurityError, label
            ):
                validate_runtime_security(
                    allowed_roots=(allowed,),
                    database_path=self.database,
                    codex_binary=str(self.binary),
                    project_root=self.project,
                    home=self.home,
                )

    def test_workspace_root_covering_database_or_codex_binary_is_rejected(self) -> None:
        state = self.database.parent
        state.mkdir()
        with self.assertRaisesRegex(RuntimeSecurityError, "database"):
            validate_runtime_security(
                allowed_roots=(state,),
                database_path=self.database,
                codex_binary=str(self.binary),
                project_root=self.project,
                home=self.home,
            )
        with self.assertRaisesRegex(RuntimeSecurityError, "executable"):
            validate_runtime_security(
                allowed_roots=(self.binary.parent,),
                database_path=self.database,
                codex_binary=str(self.binary),
                project_root=self.project,
                home=self.home,
            )

    def test_filesystem_root_is_never_allowed(self) -> None:
        with self.assertRaisesRegex(RuntimeSecurityError, "filesystem root"):
            validate_runtime_security(
                allowed_roots=(Path("/"),),
                database_path=self.database,
                codex_binary=str(self.binary),
                project_root=self.project,
                home=self.home,
            )

    def test_sensitive_system_directory_is_never_allowed(self) -> None:
        with self.assertRaisesRegex(RuntimeSecurityError, "system directory"):
            validate_runtime_security(
                allowed_roots=(Path("/etc"),),
                database_path=self.database,
                codex_binary=str(self.binary),
                project_root=self.project,
                home=self.home,
            )


class SecretRedactionTests(unittest.TestCase):
    def test_known_and_header_credentials_are_redacted(self) -> None:
        secret = "secret-token-value"
        text = (
            f"raw={secret} DISCORD_BOT_TOKEN={secret} "
            "Authorization: Bearer bearer-token Authorization: Bot bot-token"
        )
        redacted = redact_sensitive_text(text, (secret,))
        self.assertNotIn(secret, redacted)
        self.assertNotIn("bearer-token", redacted)
        self.assertNotIn("bot-token", redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 3)


class SubprocessSecurityAuditTests(unittest.TestCase):
    def test_source_has_no_shell_execution_primitives(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(SRC.rglob("*.py"))
        )
        for forbidden in (
            "create_subprocess_shell",
            "shell=True",
            "os.system(",
            "subprocess.call(",
            "subprocess.Popen(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, combined)
        self.assertIn("asyncio.create_subprocess_exec", combined)

    def test_logging_calls_do_not_directly_format_prompt_or_result_text(self) -> None:
        forbidden_attributes = {"content", "prompt", "last_assistant_message", "input_messages"}
        violations: list[str] = []

        def sensitive(node: ast.AST, *, length_only: bool = False) -> list[str]:
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
                found: list[str] = []
                for argument in node.args:
                    found.extend(sensitive(argument, length_only=True))
                return found
            if isinstance(node, ast.Attribute) and node.attr in forbidden_attributes:
                return [] if length_only else [node.attr]
            found = []
            for child in ast.iter_child_nodes(node):
                found.extend(sensitive(child, length_only=length_only))
            return found

        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in {"debug", "info", "warning", "error", "exception", "critical"}:
                    continue
                for argument in (*node.args, *(keyword.value for keyword in node.keywords)):
                    for attribute in sensitive(argument):
                        violations.append(f"{path.name}:{node.lineno}:{attribute}")
        self.assertEqual(violations, [])


class SecurityAssetTests(unittest.TestCase):
    def test_security_host_acceptance_cli_loads(self) -> None:
        env = dict(__import__("os").environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/security-host-acceptance.py"), "--help"],
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertIn("PHASE 11", result.stdout)

    def test_local_secret_files_are_gitignored(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env", gitignore)
        self.assertIn(".env.*", gitignore)
        self.assertIn("*.db", gitignore)
        self.assertIn("*.sock", gitignore)


class NotifyFuzzTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.allowed = self.base / "allowed"
        self.workspace = self.allowed / "project"
        self.workspace.mkdir(parents=True)

    def valid_payload(self) -> dict[str, object]:
        return {
            "type": "agent-turn-complete",
            "thread-id": "00000000-1111-2222-3333-444444444444",
            "turn-id": "turn-1",
            "cwd": str(self.workspace),
            "client": "codex-tui",
            "input-messages": ["safe"],
            "last-assistant-message": "done",
        }

    def test_deterministic_malformed_payload_corpus_is_rejected_cleanly(self) -> None:
        rng = random.Random(0xC0DE)
        fields = ("type", "thread-id", "turn-id", "cwd", "client", "input-messages", "last-assistant-message")
        bad_values: tuple[object, ...] = (
            None,
            0,
            -1,
            True,
            {},
            [1, 2],
            "",
            "\x00",
            "x" * 5000,
        )
        rejected = 0
        for _ in range(250):
            payload = self.valid_payload()
            field = rng.choice(fields)
            payload[field] = rng.choice(bad_values)
            try:
                parse_notify_payload(payload, (self.allowed,))
            except NotifyValidationError:
                rejected += 1
            except Exception as exc:  # parser must fail closed with its public error type
                self.fail(f"unexpected exception {type(exc).__name__} for field {field}")
        self.assertGreater(rejected, 150)

    def test_path_traversal_like_cwd_values_are_rejected(self) -> None:
        for cwd in ("../outside", "/tmp/../etc", str(self.base / "outside")):
            payload = self.valid_payload()
            payload["cwd"] = cwd
            with self.subTest(cwd=cwd), self.assertRaises(NotifyValidationError):
                parse_notify_payload(payload, (self.allowed,))
