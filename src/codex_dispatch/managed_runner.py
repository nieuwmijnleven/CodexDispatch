"""Managed/headless Codex execution using ``codex exec resume``.

Managed prompts are supplied through stdin so the prompt body is not placed in the
process argument vector. The runner owns and tracks subprocesses for each Codex
thread so cancellation and graceful shutdown are explicit and bounded.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import shutil
from typing import Sequence

from .codex_runner import (
    CodexBinaryNotFoundError,
    CodexCapabilityError,
    CodexRunnerError,
    CodexSessionUnavailableError,
    PromptValidationError,
    normalize_prompt,
)
from .security import sanitized_codex_environment


LOGGER = logging.getLogger(__name__)
_READ_CHUNK = 64 * 1024
_SESSION_UNAVAILABLE_MARKERS = (
    "thread not found",
    "session not found",
    "no such thread",
    "no such session",
    "unknown thread",
    "unknown session",
    "could not find thread",
    "could not find session",
    "missing rollout path",
)


class CodexManagedError(CodexRunnerError):
    """Base class for managed Codex execution failures."""


class CodexManagedTimeoutError(CodexManagedError):
    """Raised when a managed execution exceeds its configured wall clock limit."""


class CodexManagedRejectedError(CodexManagedError):
    """Raised when ``codex exec resume`` returns a non-zero exit code."""


class CodexManagedCancelledError(CodexManagedError):
    """Raised when Codex Dispatch explicitly cancels a managed subprocess."""


class CodexManagedAlreadyRunningError(CodexManagedError):
    """Raised when a second managed process is requested for the same thread."""


@dataclass(frozen=True, slots=True)
class CodexManagedResult:
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


def _looks_like_unavailable(parts: Sequence[str]) -> bool:
    combined = "\n".join(parts).lower()
    return any(marker in combined for marker in _SESSION_UNAVAILABLE_MARKERS)


async def _read_bounded(
    stream: asyncio.StreamReader | None,
    limit: int,
) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    captured = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        remaining = limit - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0) or remaining <= 0:
            truncated = True
    return bytes(captured), truncated


class CodexManagedRunner:
    """Run one non-interactive ``exec resume`` subprocess per managed thread."""

    def __init__(
        self,
        binary: str = "codex",
        *,
        timeout_seconds: float = 21_600.0,
        capability_timeout_seconds: float = 5.0,
        prompt_max_chars: int = 8_000,
        output_max_bytes: int = 262_144,
    ) -> None:
        if not binary.strip():
            raise ValueError("binary must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if capability_timeout_seconds <= 0:
            raise ValueError("capability_timeout_seconds must be positive")
        if prompt_max_chars <= 0:
            raise ValueError("prompt_max_chars must be positive")
        if output_max_bytes <= 0:
            raise ValueError("output_max_bytes must be positive")

        self._binary_setting = binary.strip()
        self._timeout_seconds = timeout_seconds
        self._capability_timeout_seconds = capability_timeout_seconds
        self._prompt_max_chars = prompt_max_chars
        self._output_max_bytes = output_max_bytes
        self._resolved_binary: str | None = None
        self._capability_verified = False
        self._capability_lock = asyncio.Lock()
        self._active: dict[str, asyncio.subprocess.Process] = {}
        self._cancel_requested: set[str] = set()
        self._active_lock = asyncio.Lock()

    @property
    def active_thread_ids(self) -> tuple[str, ...]:
        return tuple(self._active)

    def _resolve_binary(self) -> str:
        if self._resolved_binary is not None:
            return self._resolved_binary
        configured = self._binary_setting
        if "/" in configured:
            path = Path(configured).expanduser()
            if not path.is_absolute():
                raise CodexBinaryNotFoundError(
                    "CODEX_DISPATCH_CODEX_BIN must be absolute when it contains a path"
                )
            if not path.is_file() or not path.stat().st_mode & 0o111:
                raise CodexBinaryNotFoundError("configured Codex executable is unavailable")
            resolved = str(path.resolve())
        else:
            found = shutil.which(configured)
            if found is None:
                raise CodexBinaryNotFoundError(
                    "Codex executable was not found in the service PATH"
                )
            resolved = found
        self._resolved_binary = resolved
        return resolved

    async def _run_help(self) -> tuple[int, str]:
        binary = self._resolve_binary()
        try:
            process = await asyncio.create_subprocess_exec(
                binary,
                "exec",
                "resume",
                "--help",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=sanitized_codex_environment(),
            )
        except FileNotFoundError as exc:
            self._resolved_binary = None
            raise CodexBinaryNotFoundError("Codex executable could not be started") from exc
        except OSError as exc:
            raise CodexManagedError("Codex capability subprocess could not be started") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._capability_timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise CodexCapabilityError("Codex exec resume capability check timed out") from exc
        text = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")[:8192]
        return int(process.returncode or 0), text

    async def verify_capability(self) -> None:
        """Verify once that this host exposes the non-interactive resume surface."""

        if self._capability_verified:
            return
        async with self._capability_lock:
            if self._capability_verified:
                return
            returncode, help_text = await self._run_help()
            lowered = help_text.lower()
            if returncode != 0 or "resume" not in lowered:
                raise CodexCapabilityError("Codex exec resume capability check failed")
            if "session" not in lowered and "thread" not in lowered:
                raise CodexCapabilityError(
                    "installed Codex exec resume help does not describe a session/thread target"
                )
            self._capability_verified = True
            LOGGER.info("codex.managed_capability_verified binary=%s", self._resolve_binary())

    async def resume(
        self,
        thread_id: str,
        prompt: str,
        *,
        cwd: Path,
    ) -> CodexManagedResult:
        """Resume a durable Codex thread using stdin for the new prompt."""

        if not thread_id or any(ord(ch) < 32 or ord(ch) == 127 for ch in thread_id):
            raise ValueError("thread_id must be a non-empty printable string")
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ValueError("cwd must be an existing absolute directory")
        normalized = normalize_prompt(prompt, max_chars=self._prompt_max_chars)
        await self.verify_capability()
        binary = self._resolve_binary()

        async with self._active_lock:
            if thread_id in self._active:
                raise CodexManagedAlreadyRunningError(
                    "a managed Codex process is already running for this thread"
                )
            self._cancel_requested.discard(thread_id)
            try:
                process = await asyncio.create_subprocess_exec(
                    binary,
                    "exec",
                    "resume",
                    thread_id,
                    "-",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(cwd),
                    env=sanitized_codex_environment(),
                )
            except FileNotFoundError as exc:
                self._resolved_binary = None
                raise CodexBinaryNotFoundError("Codex executable could not be started") from exc
            except OSError as exc:
                raise CodexManagedError("Codex managed subprocess could not be started") from exc
            self._active[thread_id] = process

        stdout_task = asyncio.create_task(_read_bounded(process.stdout, self._output_max_bytes))
        stderr_task = asyncio.create_task(_read_bounded(process.stderr, self._output_max_bytes))
        try:
            assert process.stdin is not None
            try:
                process.stdin.write(normalized.encode("utf-8"))
                await process.stdin.drain()
                process.stdin.close()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                raise CodexManagedError("Codex managed stdin closed unexpectedly") from exc
            try:
                await asyncio.wait_for(process.wait(), timeout=self._timeout_seconds)
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise CodexManagedTimeoutError("Codex managed execution timed out") from exc
            stdout_data, stdout_truncated = await stdout_task
            stderr_data, stderr_truncated = await stderr_task
        except asyncio.CancelledError:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except TimeoutError:
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    await process.wait()
            raise
        finally:
            if not stdout_task.done():
                stdout_task.cancel()
            if not stderr_task.done():
                stderr_task.cancel()
            async with self._active_lock:
                self._active.pop(thread_id, None)
                cancelled = thread_id in self._cancel_requested
                self._cancel_requested.discard(thread_id)

        if cancelled:
            raise CodexManagedCancelledError("Codex managed execution was cancelled")

        stdout = stdout_data.decode("utf-8", errors="replace")
        stderr = stderr_data.decode("utf-8", errors="replace")
        returncode = int(process.returncode or 0)
        if returncode != 0:
            if _looks_like_unavailable((stdout, stderr)):
                raise CodexSessionUnavailableError("Codex managed session is unavailable")
            raise CodexManagedRejectedError(
                f"Codex exec resume failed with exit code {returncode}"
            )

        LOGGER.info(
            "codex.managed_completed thread_id=%s cwd=%s prompt_length=%d "
            "stdout_bytes=%d stderr_bytes=%d stdout_truncated=%s stderr_truncated=%s",
            thread_id,
            cwd,
            len(normalized),
            len(stdout_data),
            len(stderr_data),
            stdout_truncated,
            stderr_truncated,
        )
        return CodexManagedResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    async def cancel(self, thread_id: str) -> bool:
        """Terminate an active managed process and wait for it to exit."""

        async with self._active_lock:
            process = self._active.get(thread_id)
            if process is None:
                return False
            self._cancel_requested.add(thread_id)
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
        return True

    async def close(self) -> tuple[str, ...]:
        """Terminate all managed processes and return affected thread IDs."""

        threads = tuple(self._active)
        if threads:
            await asyncio.gather(*(self.cancel(thread_id) for thread_id in threads))
        return threads
