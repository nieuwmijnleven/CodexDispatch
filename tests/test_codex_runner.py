from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from codex_dispatch.codex_runner import (
    CodexBinaryNotFoundError,
    CodexCapabilityError,
    CodexQueueRejectedError,
    CodexQueueResult,
    CodexQueueRunner,
    CodexSessionUnavailableError,
    PromptValidationError,
    normalize_prompt,
)


class PromptNormalizationTests(unittest.TestCase):
    def test_normalizes_line_endings_and_outer_whitespace(self) -> None:
        self.assertEqual(
            normalize_prompt("  first\r\nsecond\r  ", max_chars=100),
            "first\nsecond",
        )

    def test_rejects_empty_nul_and_oversized_prompts(self) -> None:
        with self.assertRaises(PromptValidationError):
            normalize_prompt("   ", max_chars=100)
        with self.assertRaises(PromptValidationError):
            normalize_prompt("a\x00b", max_chars=100)
        with self.assertRaises(PromptValidationError):
            normalize_prompt("abcd", max_chars=3)


class CodexQueueRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_verify_capability_checks_thread_and_message_once(self) -> None:
        runner = CodexQueueRunner("codex")
        with patch.object(runner, "_resolve_binary", return_value="/usr/bin/codex"), patch.object(
            runner,
            "_run",
            AsyncMock(
                return_value=CodexQueueResult(
                    0,
                    "Usage: codex queue --thread <THREAD> --message <MESSAGE>",
                    "",
                )
            ),
        ) as run:
            await runner.verify_capability()
            await runner.verify_capability()

        run.assert_awaited_once_with(
            ("/usr/bin/codex", "queue", "--help"),
            timeout_seconds=5.0,
        )

    async def test_verify_capability_rejects_missing_contract(self) -> None:
        runner = CodexQueueRunner("codex")
        with patch.object(runner, "_resolve_binary", return_value="/usr/bin/codex"), patch.object(
            runner,
            "_run",
            AsyncMock(return_value=CodexQueueResult(0, "Usage: codex queue", "")),
        ):
            with self.assertRaises(CodexCapabilityError):
                await runner.verify_capability()

    async def test_queue_message_uses_exact_argv_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            runner = CodexQueueRunner("codex")
            runner._resolved_binary = "/usr/bin/codex"
            runner._capability_verified = True
            run = AsyncMock(return_value=CodexQueueResult(0, "queued", ""))
            with patch.object(runner, "_run", run):
                result = await runner.queue_message(
                    "thread-123",
                    "  next\nstep  ",
                    cwd=workspace,
                )

        self.assertEqual(result.returncode, 0)
        run.assert_awaited_once_with(
            (
                "/usr/bin/codex",
                "queue",
                "--thread",
                "thread-123",
                "--message",
                "next\nstep",
            ),
            timeout_seconds=10.0,
            cwd=workspace,
        )

    async def test_queue_message_classifies_unavailable_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runner = CodexQueueRunner("codex")
            runner._resolved_binary = "/usr/bin/codex"
            runner._capability_verified = True
            with patch.object(
                runner,
                "_run",
                AsyncMock(return_value=CodexQueueResult(1, "", "thread not found")),
            ):
                with self.assertRaises(CodexSessionUnavailableError):
                    await runner.queue_message("missing", "hello", cwd=Path(temp))

    async def test_queue_message_classifies_generic_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runner = CodexQueueRunner("codex")
            runner._resolved_binary = "/usr/bin/codex"
            runner._capability_verified = True
            with patch.object(
                runner,
                "_run",
                AsyncMock(return_value=CodexQueueResult(7, "", "permission denied")),
            ):
                with self.assertRaisesRegex(CodexQueueRejectedError, "exit code 7"):
                    await runner.queue_message("thread-123", "hello", cwd=Path(temp))

    async def test_run_uses_create_subprocess_exec_without_shell(self) -> None:
        process = Mock()
        process.returncode = 0
        process.communicate = AsyncMock(return_value=(b"ok", b""))
        runner = CodexQueueRunner("codex")
        with patch.dict(
            "os.environ",
            {
                "PATH": "/usr/bin",
                "CODEX_HOME": "/tmp/codex-home",
                "DISCORD_BOT_TOKEN": "must-not-leak",
                "CODEX_DISPATCH_DB_PATH": "/secret/state.db",
                "CODEX_ALLOWED_ROOTS": "/tmp/work",
            },
            clear=True,
        ), patch(
            "codex_dispatch.codex_runner.asyncio.create_subprocess_exec",
            AsyncMock(return_value=process),
        ) as create:
            result = await runner._run(
                ("/usr/bin/codex", "queue", "--help"),
                timeout_seconds=1.0,
                cwd=Path("/tmp"),
            )

        self.assertEqual(result.stdout, "ok")
        create.assert_awaited_once_with(
            "/usr/bin/codex",
            "queue",
            "--help",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/tmp",
            env={"PATH": "/usr/bin", "CODEX_HOME": "/tmp/codex-home"},
        )

    async def test_run_timeout_kills_process(self) -> None:
        class TimeoutProcess:
            returncode = None

            def __init__(self) -> None:
                self.killed = False

            def communicate(self):
                async def finish():
                    return (b"", b"")

                return finish()

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

        process = TimeoutProcess()

        async def timeout_once(awaitable, *, timeout):
            awaitable.close()
            raise TimeoutError

        runner = CodexQueueRunner("codex")
        with patch(
            "codex_dispatch.codex_runner.asyncio.create_subprocess_exec",
            AsyncMock(return_value=process),
        ), patch(
            "codex_dispatch.codex_runner.asyncio.wait_for",
            side_effect=timeout_once,
        ):
            from codex_dispatch.codex_runner import CodexQueueTimeoutError

            with self.assertRaises(CodexQueueTimeoutError):
                await runner._run(
                    ("/usr/bin/codex", "queue", "--help"),
                    timeout_seconds=0.01,
                )

        self.assertTrue(process.killed)

    async def test_missing_binary_is_reported_clearly(self) -> None:
        runner = CodexQueueRunner("codex")
        with patch("codex_dispatch.codex_runner.shutil.which", return_value=None):
            with self.assertRaises(CodexBinaryNotFoundError):
                await runner.verify_capability()

    async def test_relative_binary_path_is_rejected(self) -> None:
        runner = CodexQueueRunner("bin/codex")
        with self.assertRaises(CodexBinaryNotFoundError):
            await runner.verify_capability()


if __name__ == "__main__":
    unittest.main()
