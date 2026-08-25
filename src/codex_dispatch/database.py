"""SQLite persistence and repositories for Codex Dispatch."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Iterator

from .models import (
    DiscordDeliveryRecord,
    JobRecord,
    NotifyRegistration,
    SessionRecord,
    WorkspaceRecord,
)
from .notify_server import NotifyEvent


LOGGER = logging.getLogger(__name__)
_SCHEMA_VERSION = 4


class DatabaseError(RuntimeError):
    """Raised when the Codex Dispatch database cannot be used safely."""


class DatabaseInvariantError(DatabaseError):
    """Raised when persistent state conflicts with an incoming trusted event."""


class DatabaseBusyError(DatabaseError):
    """Raised when SQLite cannot acquire its write lock within the busy timeout."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _workspace_from_row(row: sqlite3.Row) -> WorkspaceRecord:
    return WorkspaceRecord(
        id=int(row["id"]),
        name=str(row["name"]),
        path=Path(str(row["path"])),
        enabled=bool(row["enabled"]),
        created_at=_parse_datetime(str(row["created_at"])),
        updated_at=_parse_datetime(str(row["updated_at"])),
    )


def _session_from_row(row: sqlite3.Row) -> SessionRecord:
    discord_thread_id = row["discord_thread_id"]
    return SessionRecord(
        thread_id=str(row["thread_id"]),
        workspace_id=int(row["workspace_id"]),
        client=str(row["client"]) if row["client"] is not None else None,
        mode=str(row["mode"]),
        discord_thread_id=(
            int(discord_thread_id) if discord_thread_id is not None else None
        ),
        status=str(row["status"]),
        auto_run=bool(row["auto_run"]),
        last_turn_id=(
            str(row["last_turn_id"]) if row["last_turn_id"] is not None else None
        ),
        last_seen_at=_parse_datetime(str(row["last_seen_at"])),
        created_at=_parse_datetime(str(row["created_at"])),
        updated_at=_parse_datetime(str(row["updated_at"])),
    )


def _delivery_from_row(row: sqlite3.Row) -> DiscordDeliveryRecord:
    delivered_at = row["delivered_at"]
    discord_message_id = row["discord_message_id"]
    return DiscordDeliveryRecord(
        notify_event_id=int(row["notify_event_id"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        discord_message_id=(
            int(discord_message_id) if discord_message_id is not None else None
        ),
        delivered_at=(
            _parse_datetime(str(delivered_at)) if delivered_at is not None else None
        ),
        last_error=(str(row["last_error"]) if row["last_error"] is not None else None),
        created_at=_parse_datetime(str(row["created_at"])),
        updated_at=_parse_datetime(str(row["updated_at"])),
    )


def _job_from_row(row: sqlite3.Row) -> JobRecord:
    started_at = row["started_at"]
    completed_at = row["completed_at"]
    exit_code = row["exit_code"]
    return JobRecord(
        id=int(row["id"]),
        thread_id=str(row["thread_id"]),
        discord_message_id=int(row["discord_message_id"]),
        prompt=str(row["prompt"]),
        status=str(row["status"]),
        created_at=_parse_datetime(str(row["created_at"])),
        started_at=(
            _parse_datetime(str(started_at)) if started_at is not None else None
        ),
        completed_at=(
            _parse_datetime(str(completed_at)) if completed_at is not None else None
        ),
        exit_code=int(exit_code) if exit_code is not None else None,
        error=str(row["error"]) if row["error"] is not None else None,
    )


def _event_hash(event: NotifyEvent) -> str:
    """Fingerprint event metadata without prompt or assistant-message contents."""

    serialized = json.dumps(
        {
            "type": event.event_type,
            "thread_id": event.thread_id,
            "turn_id": event.turn_id,
            "cwd": str(event.cwd),
            "client": event.client,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


class Database:
    """Single-process SQLite store with explicit transaction boundaries.

    Operations are deliberately small and synchronous. Codex Dispatch has one event
    loop and low write volume, while SQLite WAL mode keeps reads and writes robust.
    No network await occurs while a transaction is open.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None

    @property
    def is_open(self) -> bool:
        return self._connection is not None

    def open(self) -> None:
        if self._connection is not None:
            raise DatabaseError("database is already open")

        parent = self.path.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise DatabaseError("cannot create database directory") from exc

        try:
            connection = sqlite3.connect(
                self.path,
                timeout=5.0,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA busy_timeout = 5000")
            self._connection = connection
            self._migrate()
            try:
                os.chmod(self.path, 0o600)
            except OSError as exc:
                raise DatabaseError("cannot secure database file permissions") from exc
        except DatabaseError:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            raise
        except sqlite3.Error as exc:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            raise DatabaseError("cannot open SQLite database") from exc

    def close(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.close()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise DatabaseError("database is not open")
        return self._connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._require_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
        except sqlite3.OperationalError as exc:
            connection.rollback()
            lowered = str(exc).lower()
            if "locked" in lowered or "busy" in lowered:
                raise DatabaseBusyError("SQLite database is temporarily busy") from exc
            raise DatabaseError("SQLite transaction failed") from exc
        except sqlite3.Error as exc:
            connection.rollback()
            raise DatabaseError("SQLite transaction failed") from exc
        except Exception:
            connection.rollback()
            raise
        else:
            try:
                connection.commit()
            except sqlite3.OperationalError as exc:
                connection.rollback()
                lowered = str(exc).lower()
                if "locked" in lowered or "busy" in lowered:
                    raise DatabaseBusyError("SQLite database is temporarily busy") from exc
                raise DatabaseError("SQLite transaction commit failed") from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise DatabaseError("SQLite transaction commit failed") from exc

    def _migrate(self) -> None:
        connection = self._require_connection()
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current > _SCHEMA_VERSION:
            raise DatabaseError(
                f"database schema version {current} is newer than supported "
                f"version {_SCHEMA_VERSION}"
            )
        if current == 0:
            with self.transaction() as tx:
                tx.executescript(
                    """
                    CREATE TABLE workspaces (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        path TEXT NOT NULL UNIQUE,
                        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE sessions (
                        thread_id TEXT PRIMARY KEY,
                        workspace_id INTEGER NOT NULL,
                        client TEXT,
                        mode TEXT NOT NULL DEFAULT 'LIVE'
                            CHECK (mode IN ('LIVE', 'MANAGED')),
                        discord_thread_id INTEGER UNIQUE,
                        status TEXT NOT NULL DEFAULT 'IDLE'
                            CHECK (status IN ('IDLE', 'RUNNING', 'WAITING', 'OFFLINE', 'ERROR')),
                        auto_run INTEGER NOT NULL DEFAULT 0 CHECK (auto_run IN (0, 1)),
                        last_turn_id TEXT,
                        last_seen_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
                    );

                    CREATE TABLE notify_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        received_at TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        UNIQUE (thread_id, turn_id, event_type),
                        FOREIGN KEY (thread_id) REFERENCES sessions(thread_id) ON DELETE CASCADE
                    );

                    CREATE TABLE discord_deliveries (
                        notify_event_id INTEGER PRIMARY KEY,
                        status TEXT NOT NULL DEFAULT 'PENDING'
                            CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'FAILED')),
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                        discord_message_id INTEGER UNIQUE,
                        delivered_at TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY (notify_event_id) REFERENCES notify_events(id)
                            ON DELETE CASCADE
                    );

                    CREATE INDEX idx_sessions_workspace_id
                        ON sessions(workspace_id);
                    CREATE INDEX idx_notify_events_thread_id
                        ON notify_events(thread_id);
                    CREATE INDEX idx_discord_deliveries_status
                        ON discord_deliveries(status);

                    CREATE TABLE jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        thread_id TEXT NOT NULL,
                        discord_message_id INTEGER NOT NULL UNIQUE,
                        prompt TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'QUEUED'
                            CHECK (status IN ('QUEUED', 'DISPATCHED', 'COMPLETED', 'FAILED', 'CANCELLED')),
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT,
                        exit_code INTEGER,
                        error TEXT,
                        FOREIGN KEY (thread_id) REFERENCES sessions(thread_id) ON DELETE CASCADE
                    );
                    CREATE INDEX idx_jobs_thread_status
                        ON jobs(thread_id, status, id);
                    CREATE UNIQUE INDEX idx_jobs_one_dispatched_per_session
                        ON jobs(thread_id) WHERE status = 'DISPATCHED';
                    """
                )
                tx.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            current = _SCHEMA_VERSION

        if current == 1:
            now = _utc_now()
            with self.transaction() as tx:
                tx.executescript(
                    """
                    CREATE TABLE discord_deliveries (
                        notify_event_id INTEGER PRIMARY KEY,
                        status TEXT NOT NULL DEFAULT 'PENDING'
                            CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'FAILED')),
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                        discord_message_id INTEGER UNIQUE,
                        delivered_at TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY (notify_event_id) REFERENCES notify_events(id)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX idx_discord_deliveries_status
                        ON discord_deliveries(status);
                    """
                )
                tx.execute(
                    """
                    INSERT INTO discord_deliveries(
                        notify_event_id, status, attempts, discord_message_id,
                        delivered_at, last_error, created_at, updated_at
                    )
                    SELECT id, 'PENDING', 0, NULL, NULL, NULL, ?, ?
                    FROM notify_events
                    """,
                    (now, now),
                )
                tx.execute("PRAGMA user_version = 2")
            current = 2

        if current == 2:
            with self.transaction() as tx:
                tx.executescript(
                    """
                    CREATE TABLE jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        thread_id TEXT NOT NULL,
                        discord_message_id INTEGER NOT NULL UNIQUE,
                        prompt TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'QUEUED'
                            CHECK (status IN ('QUEUED', 'DISPATCHED', 'COMPLETED', 'FAILED', 'CANCELLED')),
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT,
                        exit_code INTEGER,
                        error TEXT,
                        FOREIGN KEY (thread_id) REFERENCES sessions(thread_id) ON DELETE CASCADE
                    );
                    CREATE INDEX idx_jobs_thread_status
                        ON jobs(thread_id, status, id);
                    CREATE UNIQUE INDEX idx_jobs_one_dispatched_per_session
                        ON jobs(thread_id) WHERE status = 'DISPATCHED';
                    """
                )
                tx.execute("PRAGMA user_version = 3")
            current = 3

        if current == 3:
            with self.transaction() as tx:
                tx.execute(
                    """
                    ALTER TABLE sessions
                    ADD COLUMN auto_run INTEGER NOT NULL DEFAULT 0
                        CHECK (auto_run IN (0, 1))
                    """
                )
                tx.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            current = _SCHEMA_VERSION

        if current != _SCHEMA_VERSION:
            raise DatabaseError(
                f"unsupported database schema version {current}; expected {_SCHEMA_VERSION}"
            )

    @property
    def schema_version(self) -> int:
        connection = self._require_connection()
        return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def get_workspace(self, workspace_id: int) -> WorkspaceRecord | None:
        row = self._require_connection().execute(
            "SELECT * FROM workspaces WHERE id = ?",
            (workspace_id,),
        ).fetchone()
        return _workspace_from_row(row) if row is not None else None

    def get_workspace_by_path(self, path: Path) -> WorkspaceRecord | None:
        row = self._require_connection().execute(
            "SELECT * FROM workspaces WHERE path = ?",
            (str(path),),
        ).fetchone()
        return _workspace_from_row(row) if row is not None else None

    def get_session(self, thread_id: str) -> SessionRecord | None:
        row = self._require_connection().execute(
            "SELECT * FROM sessions WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        return _session_from_row(row) if row is not None else None

    def get_session_by_discord_thread(self, discord_thread_id: int) -> SessionRecord | None:
        row = self._require_connection().execute(
            "SELECT * FROM sessions WHERE discord_thread_id = ?",
            (discord_thread_id,),
        ).fetchone()
        return _session_from_row(row) if row is not None else None

    def set_session_status(self, thread_id: str, status: str) -> SessionRecord:
        allowed = {"IDLE", "RUNNING", "WAITING", "OFFLINE", "ERROR"}
        if status not in allowed:
            raise ValueError(f"unsupported session status: {status}")
        now = _utc_now()
        with self.transaction() as tx:
            cursor = tx.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE thread_id = ?",
                (status, now, thread_id),
            )
            if cursor.rowcount != 1:
                raise DatabaseInvariantError("cannot update an unknown Codex session")
            row = tx.execute(
                "SELECT * FROM sessions WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            assert row is not None
        return _session_from_row(row)

    def set_session_mode(self, thread_id: str, mode: str) -> SessionRecord:
        """Switch LIVE/MANAGED only while the session has no active or queued work."""

        if mode not in {"LIVE", "MANAGED"}:
            raise ValueError("mode must be LIVE or MANAGED")
        now = _utc_now()
        with self.transaction() as tx:
            row = tx.execute(
                "SELECT * FROM sessions WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                raise DatabaseInvariantError("cannot update an unknown Codex session")
            if str(row["status"]) in {"RUNNING", "WAITING"}:
                raise DatabaseInvariantError("session is busy")
            pending = tx.execute(
                """
                SELECT 1 FROM jobs
                WHERE thread_id = ? AND status IN ('QUEUED', 'DISPATCHED')
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
            if pending is not None:
                raise DatabaseInvariantError("session has queued or dispatched jobs")
            tx.execute(
                "UPDATE sessions SET mode = ?, status = 'IDLE', updated_at = ? WHERE thread_id = ?",
                (mode, now, thread_id),
            )
            updated = tx.execute(
                "SELECT * FROM sessions WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            assert updated is not None
        return _session_from_row(updated)

    def set_session_auto_run(self, thread_id: str, enabled: bool) -> SessionRecord:
        now = _utc_now()
        with self.transaction() as tx:
            cursor = tx.execute(
                "UPDATE sessions SET auto_run = ?, updated_at = ? WHERE thread_id = ?",
                (1 if enabled else 0, now, thread_id),
            )
            if cursor.rowcount != 1:
                raise DatabaseInvariantError("cannot update an unknown Codex session")
            row = tx.execute(
                "SELECT * FROM sessions WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            assert row is not None
        return _session_from_row(row)

    def list_auto_run_sessions(self) -> tuple[SessionRecord, ...]:
        rows = self._require_connection().execute(
            "SELECT * FROM sessions WHERE auto_run = 1 ORDER BY created_at, thread_id"
        ).fetchall()
        return tuple(_session_from_row(row) for row in rows)

    def list_sessions(self) -> tuple[SessionRecord, ...]:
        rows = self._require_connection().execute(
            "SELECT * FROM sessions ORDER BY created_at, thread_id"
        ).fetchall()
        return tuple(_session_from_row(row) for row in rows)

    def register_notify(self, event: NotifyEvent) -> NotifyRegistration:
        """Persist workspace/session state and notify idempotency in one transaction."""

        now = _utc_now()
        path = str(event.cwd)
        workspace_name = event.cwd.name or event.cwd.anchor or "workspace"
        payload_hash = _event_hash(event)

        with self.transaction() as tx:
            workspace_row = tx.execute(
                "SELECT * FROM workspaces WHERE path = ?",
                (path,),
            ).fetchone()
            if workspace_row is None:
                cursor = tx.execute(
                    """
                    INSERT INTO workspaces(name, path, enabled, created_at, updated_at)
                    VALUES (?, ?, 1, ?, ?)
                    """,
                    (workspace_name, path, now, now),
                )
                workspace_id = int(cursor.lastrowid)
                workspace_row = tx.execute(
                    "SELECT * FROM workspaces WHERE id = ?",
                    (workspace_id,),
                ).fetchone()
            else:
                workspace_id = int(workspace_row["id"])
                if not bool(workspace_row["enabled"]):
                    raise DatabaseInvariantError("workspace is disabled")

            existing_session = tx.execute(
                "SELECT * FROM sessions WHERE thread_id = ?",
                (event.thread_id,),
            ).fetchone()
            session_created = existing_session is None
            if existing_session is None:
                tx.execute(
                    """
                    INSERT INTO sessions(
                        thread_id, workspace_id, client, mode, discord_thread_id,
                        status, auto_run, last_turn_id, last_seen_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 'LIVE', NULL, 'IDLE', 0, ?, ?, ?, ?)
                    """,
                    (
                        event.thread_id,
                        workspace_id,
                        event.client,
                        event.turn_id,
                        now,
                        now,
                        now,
                    ),
                )
            else:
                if int(existing_session["workspace_id"]) != workspace_id:
                    raise DatabaseInvariantError(
                        "Codex thread-id is already bound to a different workspace"
                    )
                tx.execute(
                    """
                    UPDATE sessions
                    SET client = COALESCE(?, client),
                        status = 'IDLE',
                        last_turn_id = ?,
                        last_seen_at = ?,
                        updated_at = ?
                    WHERE thread_id = ?
                    """,
                    (event.client, event.turn_id, now, now, event.thread_id),
                )

            cursor = tx.execute(
                """
                INSERT OR IGNORE INTO notify_events(
                    event_type, thread_id, turn_id, received_at, payload_hash
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.event_type,
                    event.thread_id,
                    event.turn_id,
                    now,
                    payload_hash,
                ),
            )
            event_inserted = cursor.rowcount == 1
            event_row = tx.execute(
                """
                SELECT id FROM notify_events
                WHERE thread_id = ? AND turn_id = ? AND event_type = ?
                """,
                (event.thread_id, event.turn_id, event.event_type),
            ).fetchone()
            assert event_row is not None
            notify_event_id = int(event_row["id"])

            tx.execute(
                """
                INSERT OR IGNORE INTO discord_deliveries(
                    notify_event_id, status, attempts, discord_message_id,
                    delivered_at, last_error, created_at, updated_at
                ) VALUES (?, 'PENDING', 0, NULL, NULL, NULL, ?, ?)
                """,
                (notify_event_id, now, now),
            )

            session_row = tx.execute(
                "SELECT * FROM sessions WHERE thread_id = ?",
                (event.thread_id,),
            ).fetchone()
            assert workspace_row is not None
            assert session_row is not None

        return NotifyRegistration(
            workspace=_workspace_from_row(workspace_row),
            session=_session_from_row(session_row),
            notify_event_id=notify_event_id,
            event_inserted=event_inserted,
            session_created=session_created,
        )

    def set_discord_thread(self, thread_id: str, discord_thread_id: int) -> SessionRecord:
        if discord_thread_id <= 0:
            raise ValueError("discord_thread_id must be positive")

        now = _utc_now()
        with self.transaction() as tx:
            row = tx.execute(
                "SELECT * FROM sessions WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                raise DatabaseInvariantError("cannot map an unknown Codex session")

            current = row["discord_thread_id"]
            if current is not None and int(current) != discord_thread_id:
                raise DatabaseInvariantError(
                    "Codex session is already mapped to a different Discord thread"
                )

            try:
                tx.execute(
                    """
                    UPDATE sessions
                    SET discord_thread_id = ?, updated_at = ?
                    WHERE thread_id = ?
                    """,
                    (discord_thread_id, now, thread_id),
                )
            except sqlite3.IntegrityError as exc:
                raise DatabaseInvariantError(
                    "Discord thread is already mapped to another Codex session"
                ) from exc

            updated = tx.execute(
                "SELECT * FROM sessions WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            assert updated is not None

        return _session_from_row(updated)

    def get_discord_delivery(
        self, notify_event_id: int
    ) -> DiscordDeliveryRecord | None:
        row = self._require_connection().execute(
            "SELECT * FROM discord_deliveries WHERE notify_event_id = ?",
            (notify_event_id,),
        ).fetchone()
        return _delivery_from_row(row) if row is not None else None

    def begin_discord_delivery(self, notify_event_id: int) -> DiscordDeliveryRecord:
        """Claim a pending/failed delivery before the external Discord API call."""

        now = _utc_now()
        with self.transaction() as tx:
            row = tx.execute(
                "SELECT * FROM discord_deliveries WHERE notify_event_id = ?",
                (notify_event_id,),
            ).fetchone()
            if row is None:
                raise DatabaseInvariantError("Discord delivery does not exist")
            status = str(row["status"])
            if status not in {"PENDING", "FAILED"}:
                return _delivery_from_row(row)

            tx.execute(
                """
                UPDATE discord_deliveries
                SET status = 'SENDING', attempts = attempts + 1,
                    last_error = NULL, updated_at = ?
                WHERE notify_event_id = ?
                """,
                (now, notify_event_id),
            )
            updated = tx.execute(
                "SELECT * FROM discord_deliveries WHERE notify_event_id = ?",
                (notify_event_id,),
            ).fetchone()
            assert updated is not None
        return _delivery_from_row(updated)

    def mark_discord_delivery_sent(
        self,
        notify_event_id: int,
        discord_message_id: int,
    ) -> DiscordDeliveryRecord:
        if discord_message_id <= 0:
            raise ValueError("discord_message_id must be positive")

        now = _utc_now()
        with self.transaction() as tx:
            row = tx.execute(
                "SELECT * FROM discord_deliveries WHERE notify_event_id = ?",
                (notify_event_id,),
            ).fetchone()
            if row is None:
                raise DatabaseInvariantError("Discord delivery does not exist")
            if str(row["status"]) == "SENT":
                current = row["discord_message_id"]
                if current is not None and int(current) == discord_message_id:
                    return _delivery_from_row(row)
                raise DatabaseInvariantError(
                    "Discord delivery is already bound to another message"
                )
            if str(row["status"]) != "SENDING":
                raise DatabaseInvariantError(
                    "Discord delivery must be SENDING before it can be marked SENT"
                )

            try:
                tx.execute(
                    """
                    UPDATE discord_deliveries
                    SET status = 'SENT', discord_message_id = ?, delivered_at = ?,
                        last_error = NULL, updated_at = ?
                    WHERE notify_event_id = ?
                    """,
                    (discord_message_id, now, now, notify_event_id),
                )
            except sqlite3.IntegrityError as exc:
                raise DatabaseInvariantError(
                    "Discord message is already linked to another notify event"
                ) from exc
            updated = tx.execute(
                "SELECT * FROM discord_deliveries WHERE notify_event_id = ?",
                (notify_event_id,),
            ).fetchone()
            assert updated is not None
        return _delivery_from_row(updated)

    def mark_discord_delivery_failed(
        self,
        notify_event_id: int,
        error: str,
    ) -> DiscordDeliveryRecord:
        message = error.strip()[:512] or "unknown Discord delivery error"
        now = _utc_now()
        with self.transaction() as tx:
            row = tx.execute(
                "SELECT * FROM discord_deliveries WHERE notify_event_id = ?",
                (notify_event_id,),
            ).fetchone()
            if row is None:
                raise DatabaseInvariantError("Discord delivery does not exist")
            if str(row["status"]) == "SENT":
                return _delivery_from_row(row)
            if str(row["status"]) != "SENDING":
                raise DatabaseInvariantError(
                    "Discord delivery must be SENDING before it can be marked FAILED"
                )
            tx.execute(
                """
                UPDATE discord_deliveries
                SET status = 'FAILED', last_error = ?, updated_at = ?
                WHERE notify_event_id = ?
                """,
                (message, now, notify_event_id),
            )
            updated = tx.execute(
                "SELECT * FROM discord_deliveries WHERE notify_event_id = ?",
                (notify_event_id,),
            ).fetchone()
            assert updated is not None
        return _delivery_from_row(updated)

    def list_discord_deliveries(self) -> tuple[DiscordDeliveryRecord, ...]:
        rows = self._require_connection().execute(
            "SELECT * FROM discord_deliveries ORDER BY notify_event_id"
        ).fetchall()
        return tuple(_delivery_from_row(row) for row in rows)

    def create_job(
        self,
        thread_id: str,
        discord_message_id: int,
        prompt: str,
    ) -> tuple[JobRecord, bool]:
        if discord_message_id <= 0:
            raise ValueError("discord_message_id must be positive")
        if not prompt:
            raise ValueError("prompt must not be empty")
        now = _utc_now()
        with self.transaction() as tx:
            session = tx.execute(
                "SELECT 1 FROM sessions WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if session is None:
                raise DatabaseInvariantError("cannot create a job for an unknown session")
            existing = tx.execute(
                "SELECT * FROM jobs WHERE discord_message_id = ?",
                (discord_message_id,),
            ).fetchone()
            if existing is not None:
                return _job_from_row(existing), False
            cursor = tx.execute(
                """
                INSERT INTO jobs(
                    thread_id, discord_message_id, prompt, status, created_at,
                    started_at, completed_at, exit_code, error
                ) VALUES (?, ?, ?, 'QUEUED', ?, NULL, NULL, NULL, NULL)
                """,
                (thread_id, discord_message_id, prompt, now),
            )
            row = tx.execute("SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)).fetchone()
            assert row is not None
        return _job_from_row(row), True

    def get_job(self, job_id: int) -> JobRecord | None:
        row = self._require_connection().execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return _job_from_row(row) if row is not None else None

    def get_job_by_discord_message(self, discord_message_id: int) -> JobRecord | None:
        row = self._require_connection().execute(
            "SELECT * FROM jobs WHERE discord_message_id = ?", (discord_message_id,)
        ).fetchone()
        return _job_from_row(row) if row is not None else None

    def list_jobs(
        self,
        thread_id: str,
        *,
        statuses: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> tuple[JobRecord, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        connection = self._require_connection()
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE thread_id = ? AND status IN ({placeholders}) "
                "ORDER BY id LIMIT ?",
                (thread_id, *statuses, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE thread_id = ? ORDER BY id LIMIT ?",
                (thread_id, limit),
            ).fetchall()
        return tuple(_job_from_row(row) for row in rows)

    def get_dispatched_job(self, thread_id: str) -> JobRecord | None:
        row = self._require_connection().execute(
            "SELECT * FROM jobs WHERE thread_id = ? AND status = 'DISPATCHED' ORDER BY id LIMIT 1",
            (thread_id,),
        ).fetchone()
        return _job_from_row(row) if row is not None else None

    def get_next_queued_job(self, thread_id: str) -> JobRecord | None:
        row = self._require_connection().execute(
            "SELECT * FROM jobs WHERE thread_id = ? AND status = 'QUEUED' ORDER BY id LIMIT 1",
            (thread_id,),
        ).fetchone()
        return _job_from_row(row) if row is not None else None

    def begin_job_dispatch(self, job_id: int) -> JobRecord:
        now = _utc_now()
        with self.transaction() as tx:
            row = tx.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise DatabaseInvariantError("job does not exist")
            if str(row["status"]) != "QUEUED":
                raise DatabaseInvariantError("job must be QUEUED before dispatch")
            thread_id = str(row["thread_id"])
            active = tx.execute(
                "SELECT id FROM jobs WHERE thread_id = ? AND status = 'DISPATCHED'",
                (thread_id,),
            ).fetchone()
            if active is not None:
                raise DatabaseInvariantError("session already has a dispatched job")
            tx.execute(
                "UPDATE jobs SET status = 'DISPATCHED', started_at = ?, error = NULL WHERE id = ?",
                (now, job_id),
            )
            tx.execute(
                "UPDATE sessions SET status = 'RUNNING', updated_at = ? WHERE thread_id = ?",
                (now, thread_id),
            )
            updated = tx.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert updated is not None
        return _job_from_row(updated)

    def mark_job_queue_accepted(self, job_id: int, exit_code: int = 0) -> JobRecord:
        with self.transaction() as tx:
            row = tx.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise DatabaseInvariantError("job does not exist")
            if str(row["status"]) not in {"DISPATCHED", "COMPLETED"}:
                raise DatabaseInvariantError("job is not in a dispatched state")
            tx.execute("UPDATE jobs SET exit_code = ? WHERE id = ?", (exit_code, job_id))
            updated = tx.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert updated is not None
        return _job_from_row(updated)

    def mark_job_failed(
        self,
        job_id: int,
        error: str,
        *,
        session_status: str = "ERROR",
        exit_code: int | None = None,
    ) -> JobRecord:
        allowed_session = {"IDLE", "RUNNING", "WAITING", "OFFLINE", "ERROR"}
        if session_status not in allowed_session:
            raise ValueError("unsupported session status")
        now = _utc_now()
        message = error.replace("\n", " ").strip()[:512] or "unknown job error"
        with self.transaction() as tx:
            row = tx.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise DatabaseInvariantError("job does not exist")
            if str(row["status"]) == "COMPLETED":
                return _job_from_row(row)
            if str(row["status"]) not in {"QUEUED", "DISPATCHED"}:
                raise DatabaseInvariantError("job cannot be marked failed from its current state")
            thread_id = str(row["thread_id"])
            tx.execute(
                """
                UPDATE jobs SET status = 'FAILED', completed_at = ?, exit_code = ?, error = ?
                WHERE id = ?
                """,
                (now, exit_code, message, job_id),
            )
            tx.execute(
                "UPDATE sessions SET status = ?, auto_run = 0, updated_at = ? WHERE thread_id = ?",
                (session_status, now, thread_id),
            )
            updated = tx.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert updated is not None
        return _job_from_row(updated)

    def complete_dispatched_job(self, thread_id: str) -> JobRecord | None:
        now = _utc_now()
        with self.transaction() as tx:
            row = tx.execute(
                "SELECT * FROM jobs WHERE thread_id = ? AND status = 'DISPATCHED' ORDER BY id LIMIT 1",
                (thread_id,),
            ).fetchone()
            if row is None:
                return None
            job_id = int(row["id"])
            tx.execute(
                "UPDATE jobs SET status = 'COMPLETED', completed_at = ?, error = NULL WHERE id = ?",
                (now, job_id),
            )
            updated = tx.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert updated is not None
        return _job_from_row(updated)

    def cancel_queued_job(self, thread_id: str, job_id: int) -> JobRecord:
        now = _utc_now()
        with self.transaction() as tx:
            row = tx.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or str(row["thread_id"]) != thread_id:
                raise DatabaseInvariantError("job does not belong to this session")
            if str(row["status"]) != "QUEUED":
                raise DatabaseInvariantError("only QUEUED jobs can be cancelled")
            tx.execute(
                "UPDATE jobs SET status = 'CANCELLED', completed_at = ? WHERE id = ?",
                (now, job_id),
            )
            updated = tx.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert updated is not None
        return _job_from_row(updated)

    def cancel_dispatched_job(self, thread_id: str, job_id: int) -> JobRecord:
        """Mark one active managed job cancelled after its process has stopped."""

        now = _utc_now()
        with self.transaction() as tx:
            row = tx.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or str(row["thread_id"]) != thread_id:
                raise DatabaseInvariantError("job does not belong to this session")
            if str(row["status"]) == "CANCELLED":
                return _job_from_row(row)
            if str(row["status"]) != "DISPATCHED":
                raise DatabaseInvariantError("only a DISPATCHED job can be cancelled here")
            tx.execute(
                """
                UPDATE jobs SET status = 'CANCELLED', completed_at = ?, error = NULL
                WHERE id = ?
                """,
                (now, job_id),
            )
            tx.execute(
                "UPDATE sessions SET status = 'IDLE', auto_run = 0, updated_at = ? WHERE thread_id = ?",
                (now, thread_id),
            )
            updated = tx.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert updated is not None
        return _job_from_row(updated)

    def recover_job_session_state(self) -> int:
        """Recover LIVE work conservatively and fail orphaned MANAGED processes."""

        now = _utc_now()
        with self.transaction() as tx:
            rows = tx.execute(
                """
                SELECT j.id, j.thread_id, s.mode
                FROM jobs j
                JOIN sessions s ON s.thread_id = j.thread_id
                WHERE j.status = 'DISPATCHED'
                ORDER BY j.id
                """
            ).fetchall()
            for row in rows:
                thread_id = str(row["thread_id"])
                if str(row["mode"]) == "MANAGED":
                    tx.execute(
                        """
                        UPDATE jobs
                        SET status = 'FAILED', completed_at = ?,
                            error = 'orchestrator restarted during managed execution'
                        WHERE id = ?
                        """,
                        (now, int(row["id"])),
                    )
                    tx.execute(
                        "UPDATE sessions SET status = 'ERROR', auto_run = 0, updated_at = ? WHERE thread_id = ?",
                        (now, thread_id),
                    )
                else:
                    tx.execute(
                        "UPDATE sessions SET status = 'RUNNING', updated_at = ? WHERE thread_id = ?",
                        (now, thread_id),
                    )
        return len(rows)

    def job_count(self) -> int:
        row = self._require_connection().execute("SELECT COUNT(*) AS count FROM jobs").fetchone()
        assert row is not None
        return int(row["count"])

    def notify_event_count(self) -> int:
        row = self._require_connection().execute(
            "SELECT COUNT(*) AS count FROM notify_events"
        ).fetchone()
        assert row is not None
        return int(row["count"])

    def session_count(self) -> int:
        row = self._require_connection().execute(
            "SELECT COUNT(*) AS count FROM sessions"
        ).fetchone()
        assert row is not None
        return int(row["count"])
