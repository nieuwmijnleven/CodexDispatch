"""Safe subprocess adapter for queuing prompts into live Codex sessions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import shutil
from typing import Sequence

from .security import sanitized_codex_environment


LOGGER = logging.getLogger(__name__)
_MAX_DIAGNOSTIC_CHARS = 4096
_SESSION_UNAVAILABLE_MARKERS = (
    "session not found",
    "thread not found",
    "no such session",
    "no such thread",
    "no matching session",
    "no matching thread",
    "unknown session",
    "unknown thread",
    "session is not running",
    "thread is not running",
    "session is stopped",
    "thread is stopped",
    "session unavailable",
    "thread unavailable",
    "could not find session",
    "could not find thread",
)


class CodexRunnerError(RuntimeError):
    """Base class for Codex CLI dispatch failures."""


class CodexBinaryNotFoundError(CodexRunnerError):
    """Raised when the configured Codex executable cannot be resolved."""


class CodexCapabilityError(CodexRunnerError):
    """Raised when the installed Codex lacks the required queue interface."""


class CodexQueueTimeoutError(CodexRunnerError):
    """Raised when a Codex queue command does not return within the timeout."""


class CodexQueueRejectedError(CodexRunnerError):
    """Raised when Codex rejects a queue request for a non-session-specific reason."""


class CodexSessionUnavailableError(CodexRunnerError):
    """Raised when Codex reports that the target live session is unavailable."""


class PromptValidationError(ValueError):
    """Raised when an inbound Discord prompt cannot safely be dispatched."""


@dataclass(frozen=True, slots=True)
class CodexQueueResult:
    returncode: int
    stdout: str
    stderr: str


def normalize_prompt(value: str, *, max_chars: int) -> str:
    """Normalize a Discord prompt without changing its semantic line structure."""

    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not isinstance(value, str):
        raise PromptValidationError("prompt must be text")

    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise PromptValidationError("prompt must not be empty")
    if "\x00" in normalized:
        raise PromptValidationError("prompt contains a NUL character")
    if len(normalized) > max_chars:
        raise PromptValidationError(
            f"prompt exceeds the configured maximum of {max_chars} characters"
        )
    return normalized


def _trim_diagnostic(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")[:_MAX_DIAGNOSTIC_CHARS]


def _looks_like_unavailable_session(parts: Sequence[str]) -> bool:
    combined = "\n".join(parts).lower()
    return any(marker in combined for marker in _SESSION_UNAVAILABLE_MARKERS)


class CodexQueueRunner:
    """Queue messages into existing Codex sessions using the official CLI surface.

    The executable is invoked with ``asyncio.create_subprocess_exec`` only. Prompt
    text is never interpolated into a shell command.
    """

    def __init__(
        self,
        binary: str = "codex",
        *,
        queue_timeout_seconds: float = 10.0,
        capability_timeout_seconds: float = 5.0,
        prompt_max_chars: int = 8_000,
    ) -> None:
        if not binary.strip():
            raise ValueError("binary must not be empty")
        if queue_timeout_seconds <= 0:
            raise ValueError("queue_timeout_seconds must be positive")
        if capability_timeout_seconds <= 0:
            raise ValueError("capability_timeout_seconds must be positive")
        if prompt_max_chars <= 0:
            raise ValueError("prompt_max_chars must be positive")

        self._binary_setting = binary.strip()
        self._queue_timeout_seconds = queue_timeout_seconds
        self._capability_timeout_seconds = capability_timeout_seconds
        self._prompt_max_chars = prompt_max_chars
        self._resolved_binary: str | None = None
        self._capability_verified = False
        self._capability_lock = asyncio.Lock()

    @property
    def resolved_binary(self) -> str | None:
        return self._resolved_binary

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

    async def _run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        cwd: Path | None = None,
    ) -> CodexQueueResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd is not None else None,
                env=sanitized_codex_environment(),
            )
        except FileNotFoundError as exc:
            self._resolved_binary = None
            raise CodexBinaryNotFoundError("Codex executable could not be started") from exc
        except OSError as exc:
            raise CodexRunnerError("Codex subprocess could not be started") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise CodexQueueTimeoutError("Codex CLI command timed out") from exc

        return CodexQueueResult(
            returncode=int(process.returncode or 0),
            stdout=_trim_diagnostic(stdout),
            stderr=_trim_diagnostic(stderr),
        )

    async def verify_capability(self) -> None:
        """Verify once that the installed Codex supports queue --thread --message."""

        if self._capability_verified:
            return
        async with self._capability_lock:
            if self._capability_verified:
                return
            binary = self._resolve_binary()
            result = await self._run(
                (binary, "queue", "--help"),
                timeout_seconds=self._capability_timeout_seconds,
            )
            help_text = f"{result.stdout}\n{result.stderr}"
            if result.returncode != 0:
                raise CodexCapabilityError("Codex queue capability check failed")
            if "--thread" not in help_text or "--message" not in help_text:
                raise CodexCapabilityError(
                    "installed Codex does not expose queue --thread --message"
                )
            self._capability_verified = True
            LOGGER.info("codex.queue_capability_verified binary=%s", binary)

    async def queue_message(
        self,
        thread_id: str,
        prompt: str,
        *,
        cwd: Path,
    ) -> CodexQueueResult:
        """Queue one validated prompt into an existing live Codex session."""

        if not thread_id or any(ord(ch) < 32 or ord(ch) == 127 for ch in thread_id):
            raise ValueError("thread_id must be a non-empty printable string")
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ValueError("cwd must be an existing absolute directory")

        normalized_prompt = normalize_prompt(prompt, max_chars=self._prompt_max_chars)
        await self.verify_capability()
        binary = self._resolve_binary()
        result = await self._run(
            (
                binary,
                "queue",
                "--thread",
                thread_id,
                "--message",
                normalized_prompt,
            ),
            timeout_seconds=self._queue_timeout_seconds,
            cwd=cwd,
        )
        if result.returncode == 0:
            LOGGER.info(
                "codex.queue_accepted thread_id=%s cwd=%s prompt_length=%d",
                thread_id,
                cwd,
                len(normalized_prompt),
            )
            return result

        if _looks_like_unavailable_session((result.stdout, result.stderr)):
            raise CodexSessionUnavailableError("Codex live session is unavailable")
        raise CodexQueueRejectedError(
            f"Codex queue command failed with exit code {result.returncode}"
        )
