"""Reliable Codex completion delivery to the mapped Discord thread."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
import logging
import re
from typing import Protocol

from .adapters.discord import DISCORD_MESSAGE_LIMIT
from .database import Database
from .models import DiscordDeliveryRecord, NotifyRegistration
from .notify_server import NotifyEvent
from .security import redact_sensitive_text


LOGGER = logging.getLogger(__name__)
_WHITESPACE = re.compile(r"\s+")
_TRUNCATION_SUFFIX = "\n\n… (truncated)"
_MAX_DISPLAY_FIELD = 80
_MAX_BACKOFF_SECONDS = 30.0


class CompletionDeliveryError(RuntimeError):
    """Raised when a completion notification cannot be confirmed as delivered."""


class DiscordCompletionSender(Protocol):
    async def send_text(
        self,
        channel_id: int,
        content: str,
        *,
        nonce: int | None = None,
    ) -> int: ...


def _display_field(value: str, fallback: str) -> str:
    normalized = _WHITESPACE.sub(" ", value).strip()
    if not normalized:
        normalized = fallback
    return normalized[:_MAX_DISPLAY_FIELD]


def format_completion_message(
    event: NotifyEvent,
    registration: NotifyRegistration,
) -> str:
    """Build a Discord-safe completion summary within the 2,000 character limit."""

    project = _display_field(registration.workspace.name, "workspace")
    session = _display_field(registration.session.thread_id[:8], "unknown")
    header = (
        "✅ Codex 작업 완료\n\n"
        f"Project: {project}\n"
        f"Session: {session}\n\n"
        "Result:\n"
    )
    raw_result = event.last_assistant_message or ""
    result = raw_result.replace("\x00", "").strip() or "(결과 메시지 없음)"
    content = header + result
    if len(content) <= DISCORD_MESSAGE_LIMIT:
        return content

    available = DISCORD_MESSAGE_LIMIT - len(header) - len(_TRUNCATION_SUFFIX)
    if available <= 0:
        return (header[: DISCORD_MESSAGE_LIMIT - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX)
    shortened = result[:available].rstrip()
    return header + shortened + _TRUNCATION_SUFFIX


class CompletionNotifier:
    """Deliver each persisted notify event to its mapped Discord thread once.

    The SQLite delivery row is the durable source of truth. Confirmed ``SENT`` rows
    are never sent again. ``SENDING`` rows are intentionally not retried after an
    interrupted process because the external send result is ambiguous; this favors
    duplicate prevention over silently creating a second Discord message.
    """

    def __init__(
        self,
        database: Database,
        discord: DiscordCompletionSender,
        *,
        retry_attempts: int = 3,
        retry_base_seconds: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        secrets: Iterable[str] = (),
    ) -> None:
        if retry_attempts <= 0:
            raise ValueError("retry_attempts must be positive")
        if retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds must be positive")
        self._database = database
        self._discord = discord
        self._retry_attempts = retry_attempts
        self._retry_base_seconds = retry_base_seconds
        self._sleep = sleep
        self._secrets = tuple(value for value in secrets if value)
        self._locks: dict[int, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _delivery_lock(self, notify_event_id: int) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(notify_event_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[notify_event_id] = lock
            return lock

    def _error_summary(self, exc: Exception) -> str:
        message = redact_sensitive_text(str(exc), self._secrets).replace("\n", " ").strip()
        summary = type(exc).__name__
        if message:
            summary += f": {message}"
        return summary[:512]

    async def deliver(
        self,
        event: NotifyEvent,
        registration: NotifyRegistration,
    ) -> DiscordDeliveryRecord:
        """Send or resume delivery for one persisted completion event."""

        discord_thread_id = registration.session.discord_thread_id
        if discord_thread_id is None:
            raise CompletionDeliveryError("Codex session has no Discord thread mapping")

        notify_event_id = registration.notify_event_id
        lock = await self._delivery_lock(notify_event_id)
        async with lock:
            delivery = self._database.get_discord_delivery(notify_event_id)
            if delivery is None:
                raise CompletionDeliveryError("Discord delivery state is missing")
            if delivery.status == "SENT":
                LOGGER.info(
                    "discord.completion_duplicate_suppressed notify_event_id=%s "
                    "discord_message_id=%s",
                    notify_event_id,
                    delivery.discord_message_id,
                )
                return delivery
            if delivery.status == "SENDING":
                LOGGER.error(
                    "discord.completion_unconfirmed notify_event_id=%s attempts=%s",
                    notify_event_id,
                    delivery.attempts,
                )
                return delivery

            content = format_completion_message(event, registration)
            while delivery.attempts < self._retry_attempts:
                delivery = self._database.begin_discord_delivery(notify_event_id)
                if delivery.status == "SENT":
                    return delivery
                if delivery.status != "SENDING":
                    raise CompletionDeliveryError(
                        f"unexpected Discord delivery state: {delivery.status}"
                    )

                try:
                    discord_message_id = await self._discord.send_text(
                        discord_thread_id,
                        content,
                        nonce=notify_event_id,
                    )
                except Exception as exc:
                    delivery = self._database.mark_discord_delivery_failed(
                        notify_event_id,
                        self._error_summary(exc),
                    )
                    LOGGER.warning(
                        "discord.completion_failed notify_event_id=%s attempt=%s/%s "
                        "error_type=%s",
                        notify_event_id,
                        delivery.attempts,
                        self._retry_attempts,
                        type(exc).__name__,
                    )
                    if delivery.attempts >= self._retry_attempts:
                        raise CompletionDeliveryError(
                            "Discord completion delivery exhausted retry attempts"
                        ) from exc
                    delay = min(
                        self._retry_base_seconds * (2 ** (delivery.attempts - 1)),
                        _MAX_BACKOFF_SECONDS,
                    )
                    await self._sleep(delay)
                    continue

                delivery = self._database.mark_discord_delivery_sent(
                    notify_event_id,
                    discord_message_id,
                )
                LOGGER.info(
                    "discord.completion_sent notify_event_id=%s discord_thread_id=%s "
                    "discord_message_id=%s attempts=%s",
                    notify_event_id,
                    discord_thread_id,
                    discord_message_id,
                    delivery.attempts,
                )
                return delivery

            raise CompletionDeliveryError(
                "Discord completion delivery exhausted retry attempts"
            )
