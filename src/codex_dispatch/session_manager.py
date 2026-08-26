"""Persistent Codex session registration and Discord thread mapping."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Protocol

from .database import Database
from .models import NotifyRegistration, SessionRecord
from .notify_server import NotifyEvent


LOGGER = logging.getLogger(__name__)
_DISCORD_THREAD_NAME_LIMIT = 100
_NAME_WHITESPACE = re.compile(r"\s+")


class DiscordThreadCreator(Protocol):
    async def create_thread(self, parent_channel_id: int, name: str) -> int: ...

    async def send_text(
        self,
        channel_id: int,
        content: str,
        *,
        nonce: int | None = None,
        mention_allowed_users: bool = False,
    ) -> int: ...


class SessionManager:
    """Register Codex notify events and ensure durable Discord thread mappings."""

    def __init__(
        self,
        database: Database,
        discord: DiscordThreadCreator,
        control_channel_id: int,
    ) -> None:
        if control_channel_id <= 0:
            raise ValueError("control_channel_id must be positive")
        self._database = database
        self._discord = discord
        self._control_channel_id = control_channel_id
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _session_lock(self, thread_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[thread_id] = lock
            return lock

    @staticmethod
    def _thread_name(registration: NotifyRegistration) -> str:
        workspace = _NAME_WHITESPACE.sub(" ", registration.workspace.name).strip()
        if not workspace:
            workspace = "workspace"
        short_id = registration.session.thread_id[:8]
        name = f"{workspace} / {short_id}"
        return name[:_DISCORD_THREAD_NAME_LIMIT]

    async def handle_notify(self, event: NotifyEvent) -> NotifyRegistration:
        """Persist one completion event and ensure its session has a Discord thread.

        The SQLite transaction completes before any Discord API call. A per-session
        asyncio lock prevents concurrent completion events from creating duplicate
        Discord threads in the same process.
        """

        registration = self._database.register_notify(event)
        new_mapping = False
        lock = await self._session_lock(event.thread_id)
        async with lock:
            session = self._database.get_session(event.thread_id)
            if session is None:
                raise RuntimeError("session disappeared after notify registration")
            if session.discord_thread_id is None:
                discord_thread_id = await self._discord.create_thread(
                    self._control_channel_id,
                    self._thread_name(registration),
                )
                session = self._database.set_discord_thread(
                    event.thread_id,
                    discord_thread_id,
                )
                new_mapping = True
                LOGGER.info(
                    "session.discord_thread_created thread_id=%s discord_thread_id=%s "
                    "workspace_id=%s",
                    event.thread_id,
                    discord_thread_id,
                    session.workspace_id,
                )

        if registration.event_inserted or new_mapping:
            persisted_session = self._database.get_session(event.thread_id)
            if persisted_session is not None and persisted_session.discord_thread_id is not None:
                alert_text = (
                    "🧵 새 Codex 세션 완료"
                    if registration.session_created or new_mapping
                    else "✅ Codex 작업 완료"
                )
                try:
                    await self._discord.send_text(
                        self._control_channel_id,
                        f"{alert_text}: <#{persisted_session.discord_thread_id}>",
                        mention_allowed_users=True,
                    )
                except Exception as exc:
                    # The durable mapping and in-thread completion delivery are
                    # authoritative. Parent-channel mentions are best-effort UX.
                    LOGGER.warning(
                        "session.discord_completion_alert_failed thread_id=%s "
                        "discord_thread_id=%s error_type=%s",
                        event.thread_id,
                        persisted_session.discord_thread_id,
                        type(exc).__name__,
                    )
            LOGGER.info(
                "session.notify_persisted thread_id=%s turn_id=%s session_created=%s",
                event.thread_id,
                event.turn_id,
                registration.session_created,
            )
        else:
            LOGGER.info(
                "session.notify_duplicate_persistent thread_id=%s turn_id=%s",
                event.thread_id,
                event.turn_id,
            )

        persisted = self._database.get_session(event.thread_id)
        assert persisted is not None
        return NotifyRegistration(
            workspace=registration.workspace,
            session=persisted,
            notify_event_id=registration.notify_event_id,
            event_inserted=registration.event_inserted,
            session_created=registration.session_created,
        )

    def get_session(self, thread_id: str) -> SessionRecord | None:
        return self._database.get_session(thread_id)

    def get_session_for_discord_thread(
        self, discord_thread_id: int
    ) -> SessionRecord | None:
        return self._database.get_session_by_discord_thread(discord_thread_id)
