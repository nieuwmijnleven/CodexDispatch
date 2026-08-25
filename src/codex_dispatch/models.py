"""Persistent domain records used by Codex Dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    id: int
    name: str
    path: Path
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SessionRecord:
    thread_id: str
    workspace_id: int
    client: str | None
    mode: str
    discord_thread_id: int | None
    status: str
    auto_run: bool
    last_turn_id: str | None
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: int
    thread_id: str
    discord_message_id: int
    prompt: str
    status: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    exit_code: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class NotifyRegistration:
    workspace: WorkspaceRecord
    session: SessionRecord
    notify_event_id: int
    event_inserted: bool
    session_created: bool


@dataclass(frozen=True, slots=True)
class DiscordDeliveryRecord:
    notify_event_id: int
    status: str
    attempts: int
    discord_message_id: int | None
    delivered_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
