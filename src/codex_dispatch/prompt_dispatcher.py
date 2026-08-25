"""Dispatch authorized Discord thread messages into mapped live Codex sessions."""

from __future__ import annotations

import logging
from typing import Protocol

from .adapters.discord import DiscordIncomingMessage
from .codex_runner import (
    CodexCapabilityError,
    CodexQueueRejectedError,
    CodexQueueRunner,
    CodexQueueTimeoutError,
    CodexRunnerError,
    CodexSessionUnavailableError,
    PromptValidationError,
    normalize_prompt,
)
from .database import Database, DatabaseInvariantError


LOGGER = logging.getLogger(__name__)


class DiscordPromptFeedback(Protocol):
    async def send_text(
        self,
        channel_id: int,
        content: str,
        *,
        nonce: int | None = None,
    ) -> int: ...


class PromptDispatchError(RuntimeError):
    """Raised when an authorized Discord prompt cannot be dispatched."""


class DiscordPromptDispatcher:
    """Resolve Discord thread -> Codex session and invoke the live queue adapter."""

    def __init__(
        self,
        database: Database,
        runner: CodexQueueRunner,
        discord: DiscordPromptFeedback,
        *,
        prompt_max_chars: int = 8_000,
    ) -> None:
        if prompt_max_chars <= 0:
            raise ValueError("prompt_max_chars must be positive")
        self._database = database
        self._runner = runner
        self._discord = discord
        self._prompt_max_chars = prompt_max_chars

    async def _feedback(self, message: DiscordIncomingMessage, text: str) -> None:
        try:
            await self._discord.send_text(
                message.channel_id,
                text,
                nonce=message.message_id,
            )
        except Exception as exc:
            LOGGER.warning(
                "discord.prompt_feedback_failed message_id=%s error_type=%s",
                message.message_id,
                type(exc).__name__,
            )

    async def dispatch(self, message: DiscordIncomingMessage) -> None:
        """Dispatch one authorized Discord message to its mapped live session."""

        session = self._database.get_session_by_discord_thread(message.channel_id)
        if session is None:
            LOGGER.info(
                "discord.prompt_rejected reason=unmapped_thread message_id=%s channel_id=%s",
                message.message_id,
                message.channel_id,
            )
            await self._feedback(
                message,
                "⚠️ 이 Discord Thread에 연결된 Codex 세션이 없습니다.",
            )
            return

        if session.mode != "LIVE":
            LOGGER.info(
                "discord.prompt_rejected reason=non_live_session message_id=%s thread_id=%s mode=%s",
                message.message_id,
                session.thread_id,
                session.mode,
            )
            await self._feedback(
                message,
                "⚠️ 이 세션은 LIVE Codex 세션이 아니어서 현재 단계에서는 전달할 수 없습니다.",
            )
            return

        workspace = self._database.get_workspace(session.workspace_id)
        if workspace is None or not workspace.enabled:
            self._database.set_session_status(session.thread_id, "ERROR")
            LOGGER.error(
                "discord.prompt_rejected reason=workspace_unavailable message_id=%s thread_id=%s",
                message.message_id,
                session.thread_id,
            )
            await self._feedback(
                message,
                "⚠️ 연결된 workspace를 사용할 수 없습니다.",
            )
            return

        try:
            prompt = normalize_prompt(
                message.content,
                max_chars=self._prompt_max_chars,
            )
        except PromptValidationError as exc:
            LOGGER.info(
                "discord.prompt_rejected reason=invalid_prompt message_id=%s thread_id=%s",
                message.message_id,
                session.thread_id,
            )
            await self._feedback(message, f"⚠️ 프롬프트를 전달할 수 없습니다: {exc}")
            return

        try:
            await self._runner.queue_message(
                session.thread_id,
                prompt,
                cwd=workspace.path,
            )
        except CodexSessionUnavailableError:
            self._database.set_session_status(session.thread_id, "OFFLINE")
            LOGGER.warning(
                "discord.prompt_dispatch_failed reason=session_unavailable "
                "message_id=%s thread_id=%s",
                message.message_id,
                session.thread_id,
            )
            await self._feedback(
                message,
                "⚠️ Codex live 세션을 찾을 수 없거나 현재 사용할 수 없습니다.",
            )
            return
        except CodexQueueTimeoutError:
            self._database.set_session_status(session.thread_id, "ERROR")
            LOGGER.warning(
                "discord.prompt_dispatch_failed reason=queue_timeout message_id=%s thread_id=%s",
                message.message_id,
                session.thread_id,
            )
            await self._feedback(
                message,
                "⚠️ Codex queue 명령이 시간 안에 완료되지 않았습니다.",
            )
            return
        except CodexCapabilityError:
            self._database.set_session_status(session.thread_id, "ERROR")
            LOGGER.error(
                "discord.prompt_dispatch_failed reason=missing_queue_capability "
                "message_id=%s thread_id=%s",
                message.message_id,
                session.thread_id,
            )
            await self._feedback(
                message,
                "⚠️ 설치된 Codex CLI가 필요한 queue 기능을 지원하지 않습니다.",
            )
            return
        except (CodexQueueRejectedError, CodexRunnerError) as exc:
            self._database.set_session_status(session.thread_id, "ERROR")
            LOGGER.warning(
                "discord.prompt_dispatch_failed reason=codex_error message_id=%s "
                "thread_id=%s error_type=%s",
                message.message_id,
                session.thread_id,
                type(exc).__name__,
            )
            await self._feedback(
                message,
                "⚠️ Codex가 프롬프트 전달 요청을 처리하지 못했습니다.",
            )
            return
        except DatabaseInvariantError:
            LOGGER.exception(
                "discord.prompt_state_failed message_id=%s thread_id=%s",
                message.message_id,
                session.thread_id,
            )
            raise

        self._database.set_session_status(session.thread_id, "RUNNING")
        LOGGER.info(
            "discord.prompt_dispatched message_id=%s thread_id=%s prompt_length=%d",
            message.message_id,
            session.thread_id,
            len(prompt),
        )
        await self._feedback(message, "▶️ Codex에 전달했습니다.")
