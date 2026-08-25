from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from codex_dispatch.managed_runner import (
    CodexManagedCancelledError,
    CodexManagedRunner,
    CodexManagedTimeoutError,
)


class FakeStream:
    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.chunks = list(chunks or [])

    async def read(self, _size: int) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        return b""


class FakeStdin:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(
        self,
        *,
        stdout: list[bytes] | None = None,
        stderr: list[bytes] | None = None,
        returncode: int = 0,
        block: bool = False,
    ) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)
        self.returncode: int | None = None if block else returncode
        self._final_returncode = returncode
        self._event = asyncio.Event()
        if not block:
            self._event.set()
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        await self._event.wait()
        if self.returncode is None:
            self.returncode = self._final_returncode
        return int(self.returncode)

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._event.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._event.set()


class ManagedRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.workspace = Path(self.temp.name)

    async def _cleanup(self) -> None:
        self.temp.cleanup()

    def ready_runner(self, **kwargs) -> CodexManagedRunner:
        runner = CodexManagedRunner("codex", **kwargs)
        runner._resolved_binary = "/usr/bin/codex"  # type: ignore[attr-defined]
        runner._capability_verified = True  # type: ignore[attr-defined]
        return runner

    async def test_verify_capability_requires_resume_session_contract(self) -> None:
        runner = CodexManagedRunner("codex")
        runner._resolved_binary = "/usr/bin/codex"  # type: ignore[attr-defined]
        runner._run_help = AsyncMock(  # type: ignore[method-assign]
            return_value=(0, "Resume a previous thread by SESSION_ID")
        )
        await runner.verify_capability()
        self.assertTrue(runner._capability_verified)  # type: ignore[attr-defined]

    async def test_resume_uses_stdin_not_argv_for_prompt(self) -> None:
        runner = self.ready_runner()
        process = FakeProcess(stdout=[b"done\n"])
        create = AsyncMock(return_value=process)
        with patch.dict(
            "os.environ",
            {
                "PATH": "/usr/bin",
                "CODEX_HOME": "/tmp/codex-home",
                "DISCORD_BOT_TOKEN": "must-not-leak",
                "CODEX_DISPATCH_NOTIFY_SOCKET": "/run/private.sock",
                "CODEX_ALLOWED_ROOTS": "/tmp/work",
            },
            clear=True,
        ), patch(
            "codex_dispatch.managed_runner.asyncio.create_subprocess_exec",
            create,
        ):
            result = await runner.resume(
                "thread-123",
                "secret prompt",
                cwd=self.workspace,
            )

        args = create.await_args.args
        self.assertEqual(
            args,
            ("/usr/bin/codex", "exec", "resume", "thread-123", "-"),
        )
        self.assertNotIn("secret prompt", args)
        child_env = create.await_args.kwargs["env"]
        self.assertEqual(child_env, {"PATH": "/usr/bin", "CODEX_HOME": "/tmp/codex-home"})
        self.assertEqual(bytes(process.stdin.data), b"secret prompt")
        self.assertTrue(process.stdin.closed)
        self.assertEqual(result.stdout, "done\n")
        self.assertEqual(runner.active_thread_ids, ())

    async def test_output_is_bounded_while_stream_is_fully_drained(self) -> None:
        runner = self.ready_runner(output_max_bytes=5)
        process = FakeProcess(stdout=[b"1234", b"5678"], stderr=[b"abcdef"])
        with patch(
            "codex_dispatch.managed_runner.asyncio.create_subprocess_exec",
            AsyncMock(return_value=process),
        ):
            result = await runner.resume("thread-123", "work", cwd=self.workspace)

        self.assertEqual(result.stdout, "12345")
        self.assertEqual(result.stderr, "abcde")
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)
        self.assertEqual(process.stdout.chunks, [])
        self.assertEqual(process.stderr.chunks, [])

    async def test_cancel_terminates_active_process(self) -> None:
        runner = self.ready_runner()
        process = FakeProcess(block=True)
        with patch(
            "codex_dispatch.managed_runner.asyncio.create_subprocess_exec",
            AsyncMock(return_value=process),
        ):
            task = asyncio.create_task(
                runner.resume("thread-123", "work", cwd=self.workspace)
            )
            await asyncio.sleep(0)
            self.assertIn("thread-123", runner.active_thread_ids)
            self.assertTrue(await runner.cancel("thread-123"))
            with self.assertRaises(CodexManagedCancelledError):
                await task

        self.assertTrue(process.terminated)
        self.assertEqual(runner.active_thread_ids, ())

    async def test_timeout_kills_process(self) -> None:
        runner = self.ready_runner(timeout_seconds=0.001)
        process = FakeProcess(block=True)
        with patch(
            "codex_dispatch.managed_runner.asyncio.create_subprocess_exec",
            AsyncMock(return_value=process),
        ):
            with self.assertRaises(CodexManagedTimeoutError):
                await runner.resume("thread-123", "work", cwd=self.workspace)
        self.assertTrue(process.killed)

    async def test_close_cancels_all_active_threads(self) -> None:
        runner = self.ready_runner()
        first = FakeProcess(block=True)
        second = FakeProcess(block=True)
        create = AsyncMock(side_effect=[first, second])
        with patch(
            "codex_dispatch.managed_runner.asyncio.create_subprocess_exec",
            create,
        ):
            tasks = [
                asyncio.create_task(runner.resume("thread-a", "a", cwd=self.workspace)),
                asyncio.create_task(runner.resume("thread-b", "b", cwd=self.workspace)),
            ]
            await asyncio.sleep(0)
            affected = await runner.close()
            self.assertEqual(set(affected), {"thread-a", "thread-b"})
            for task in tasks:
                with self.assertRaises(CodexManagedCancelledError):
                    await task

        self.assertTrue(first.terminated)
        self.assertTrue(second.terminated)


if __name__ == "__main__":
    unittest.main()
