from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_dispatch.database import (
    Database,
    DatabaseBusyError,
    DatabaseError,
    DatabaseInvariantError,
)
from codex_dispatch.notify_server import NotifyEvent


def event(
    cwd: Path,
    *,
    thread_id: str = "thread-001",
    turn_id: str = "turn-001",
    client: str | None = "codex-tui",
) -> NotifyEvent:
    return NotifyEvent(
        event_type="agent-turn-complete",
        thread_id=thread_id,
        turn_id=turn_id,
        cwd=cwd,
        client=client,
        input_messages=("do work",),
        last_assistant_message="done",
    )


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.db_path = self.root / "state" / "codex-dispatch.db"
        self.db = Database(self.db_path)
        self.db.open()
        self.addCleanup(self.db.close)

    def test_open_creates_schema_and_private_database_file(self) -> None:
        self.assertEqual(self.db.schema_version, 4)
        self.assertEqual(os.stat(self.db_path).st_mode & 0o777, 0o600)

        connection = sqlite3.connect(self.db_path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
        self.assertTrue(
            {"workspaces", "sessions", "notify_events", "discord_deliveries", "jobs"} <= tables
        )

    def test_write_lock_timeout_is_reported_as_database_busy(self) -> None:
        self.db.register_notify(event(self.workspace))
        assert self.db._connection is not None
        self.db._connection.execute("PRAGMA busy_timeout = 25")
        blocker = sqlite3.connect(self.db_path, timeout=0.1, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with self.assertRaises(DatabaseBusyError):
                self.db.create_job("thread-001", 9001, "locked write")
        finally:
            blocker.rollback()
            blocker.close()

    def test_register_notify_creates_workspace_session_and_event_atomically(self) -> None:
        registration = self.db.register_notify(event(self.workspace))

        self.assertTrue(registration.event_inserted)
        self.assertTrue(registration.session_created)
        self.assertEqual(registration.workspace.path, self.workspace.resolve())
        self.assertEqual(registration.session.thread_id, "thread-001")
        self.assertEqual(registration.session.status, "IDLE")
        self.assertEqual(registration.session.mode, "LIVE")
        self.assertFalse(registration.session.auto_run)
        self.assertEqual(self.db.session_count(), 1)
        self.assertEqual(self.db.notify_event_count(), 1)
        delivery = self.db.get_discord_delivery(registration.notify_event_id)
        assert delivery is not None
        self.assertEqual(delivery.status, "PENDING")
        self.assertEqual(delivery.attempts, 0)

    def test_duplicate_notify_is_persistently_idempotent(self) -> None:
        first = self.db.register_notify(event(self.workspace))
        second = self.db.register_notify(event(self.workspace))

        self.assertTrue(first.event_inserted)
        self.assertFalse(second.event_inserted)
        self.assertEqual(self.db.notify_event_count(), 1)
        self.assertEqual(self.db.session_count(), 1)

    def test_new_turn_updates_existing_session_without_creating_another(self) -> None:
        self.db.register_notify(event(self.workspace, turn_id="turn-001"))
        second = self.db.register_notify(event(self.workspace, turn_id="turn-002"))

        self.assertFalse(second.session_created)
        session = self.db.get_session("thread-001")
        assert session is not None
        self.assertEqual(session.last_turn_id, "turn-002")
        self.assertEqual(self.db.notify_event_count(), 2)
        self.assertEqual(self.db.session_count(), 1)

    def test_same_thread_cannot_move_to_different_workspace(self) -> None:
        other = self.root / "other"
        other.mkdir()
        self.db.register_notify(event(self.workspace))

        with self.assertRaisesRegex(DatabaseInvariantError, "different workspace"):
            self.db.register_notify(event(other, turn_id="turn-002"))

        self.assertEqual(self.db.session_count(), 1)
        self.assertEqual(self.db.notify_event_count(), 1)

    def test_discord_delivery_lifecycle_is_persistent(self) -> None:
        registration = self.db.register_notify(event(self.workspace))
        delivery = self.db.begin_discord_delivery(registration.notify_event_id)
        self.assertEqual(delivery.status, "SENDING")
        self.assertEqual(delivery.attempts, 1)

        failed = self.db.mark_discord_delivery_failed(
            registration.notify_event_id,
            "temporary failure",
        )
        self.assertEqual(failed.status, "FAILED")
        self.assertEqual(failed.last_error, "temporary failure")

        sending = self.db.begin_discord_delivery(registration.notify_event_id)
        self.assertEqual(sending.attempts, 2)
        sent = self.db.mark_discord_delivery_sent(
            registration.notify_event_id,
            9001,
        )
        self.assertEqual(sent.status, "SENT")
        self.assertEqual(sent.discord_message_id, 9001)
        self.assertIsNotNone(sent.delivered_at)

        self.db.close()
        reopened = Database(self.db_path)
        reopened.open()
        self.addCleanup(reopened.close)
        persisted = reopened.get_discord_delivery(registration.notify_event_id)
        assert persisted is not None
        self.assertEqual(persisted.status, "SENT")
        self.assertEqual(persisted.discord_message_id, 9001)

    def test_discord_thread_mapping_is_unique_and_persistent(self) -> None:
        self.db.register_notify(event(self.workspace, thread_id="thread-a"))
        self.db.register_notify(
            event(self.workspace, thread_id="thread-b", turn_id="turn-b")
        )

        mapped = self.db.set_discord_thread("thread-a", 7001)
        self.assertEqual(mapped.discord_thread_id, 7001)
        self.assertEqual(
            self.db.get_session_by_discord_thread(7001).thread_id,  # type: ignore[union-attr]
            "thread-a",
        )

        with self.assertRaisesRegex(DatabaseInvariantError, "another Codex session"):
            self.db.set_discord_thread("thread-b", 7001)

        self.db.close()
        reopened = Database(self.db_path)
        reopened.open()
        self.addCleanup(reopened.close)
        session = reopened.get_session("thread-a")
        assert session is not None
        self.assertEqual(session.discord_thread_id, 7001)
        self.assertEqual(reopened.notify_event_count(), 2)

    def test_session_status_can_be_updated_persistently(self) -> None:
        self.db.register_notify(event(self.workspace))
        updated = self.db.set_session_status("thread-001", "RUNNING")
        self.assertEqual(updated.status, "RUNNING")
        self.assertEqual(self.db.get_session("thread-001").status, "RUNNING")  # type: ignore[union-attr]

        with self.assertRaises(ValueError):
            self.db.set_session_status("thread-001", "BROKEN")
        with self.assertRaisesRegex(DatabaseInvariantError, "unknown Codex session"):
            self.db.set_session_status("missing", "ERROR")

    def test_existing_session_cannot_be_remapped(self) -> None:
        self.db.register_notify(event(self.workspace))
        self.db.set_discord_thread("thread-001", 7001)

        with self.assertRaisesRegex(DatabaseInvariantError, "different Discord thread"):
            self.db.set_discord_thread("thread-001", 7002)

    def test_unknown_session_cannot_be_mapped(self) -> None:
        with self.assertRaisesRegex(DatabaseInvariantError, "unknown Codex session"):
            self.db.set_discord_thread("missing", 7001)

    def test_schema_v1_is_migrated_with_pending_deliveries(self) -> None:
        legacy_path = self.root / "legacy" / "codex-dispatch.db"
        legacy_path.parent.mkdir()
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(
                """
                CREATE TABLE workspaces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE sessions (
                    thread_id TEXT PRIMARY KEY,
                    workspace_id INTEGER NOT NULL,
                    client TEXT,
                    mode TEXT NOT NULL,
                    discord_thread_id INTEGER UNIQUE,
                    status TEXT NOT NULL,
                    last_turn_id TEXT,
                    last_seen_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE notify_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    UNIQUE (thread_id, turn_id, event_type)
                );
                INSERT INTO workspaces VALUES (
                    1, 'project', '/tmp/project', 1,
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
                );
                INSERT INTO sessions VALUES (
                    'thread-v1', 1, 'codex-tui', 'LIVE', 7001, 'IDLE', 'turn-v1',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                    '2026-01-01T00:00:00+00:00'
                );
                INSERT INTO notify_events VALUES (
                    1, 'agent-turn-complete', 'thread-v1', 'turn-v1',
                    '2026-01-01T00:00:00+00:00', 'hash'
                );
                PRAGMA user_version = 1;
                """
            )
            connection.commit()
        finally:
            connection.close()

        migrated = Database(legacy_path)
        migrated.open()
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.schema_version, 4)
        delivery = migrated.get_discord_delivery(1)
        assert delivery is not None
        self.assertEqual(delivery.status, "PENDING")
        self.assertEqual(delivery.attempts, 0)
        migrated_session = migrated.get_session("thread-v1")
        assert migrated_session is not None
        self.assertFalse(migrated_session.auto_run)

    def test_auto_run_setting_is_persistent_and_failure_pauses_it(self) -> None:
        self.db.register_notify(event(self.workspace))
        enabled = self.db.set_session_auto_run("thread-001", True)
        self.assertTrue(enabled.auto_run)
        self.assertEqual(len(self.db.list_auto_run_sessions()), 1)

        job, _ = self.db.create_job("thread-001", 8999, "work")
        self.db.begin_job_dispatch(job.id)
        self.db.mark_job_failed(job.id, "boom", session_status="ERROR")
        paused = self.db.get_session("thread-001")
        assert paused is not None
        self.assertFalse(paused.auto_run)

        with self.assertRaisesRegex(DatabaseInvariantError, "unknown Codex session"):
            self.db.set_session_auto_run("missing", True)

    def test_job_repository_is_idempotent_and_serialized(self) -> None:
        self.db.register_notify(event(self.workspace))
        first, created = self.db.create_job("thread-001", 9001, "first")
        duplicate, duplicate_created = self.db.create_job("thread-001", 9001, "first")
        second, second_created = self.db.create_job("thread-001", 9002, "second")
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertTrue(second_created)
        self.assertEqual(first.id, duplicate.id)
        self.assertEqual(self.db.job_count(), 2)

        dispatched = self.db.begin_job_dispatch(first.id)
        self.assertEqual(dispatched.status, "DISPATCHED")
        with self.assertRaisesRegex(DatabaseInvariantError, "already has a dispatched job"):
            self.db.begin_job_dispatch(second.id)

        accepted = self.db.mark_job_queue_accepted(first.id, 0)
        self.assertEqual(accepted.exit_code, 0)
        completed = self.db.complete_dispatched_job("thread-001")
        assert completed is not None
        self.assertEqual(completed.status, "COMPLETED")
        cancelled = self.db.cancel_queued_job("thread-001", second.id)
        self.assertEqual(cancelled.status, "CANCELLED")

    def test_job_failure_and_recovery_state(self) -> None:
        self.db.register_notify(event(self.workspace))
        job, _ = self.db.create_job("thread-001", 9001, "work")
        self.db.begin_job_dispatch(job.id)
        self.db.set_session_status("thread-001", "IDLE")
        self.assertEqual(self.db.recover_job_session_state(), 1)
        self.assertEqual(self.db.get_session("thread-001").status, "RUNNING")  # type: ignore[union-attr]
        failed = self.db.mark_job_failed(job.id, "gone", session_status="OFFLINE")
        self.assertEqual(failed.status, "FAILED")
        self.assertEqual(failed.error, "gone")
        self.assertEqual(self.db.get_session("thread-001").status, "OFFLINE")  # type: ignore[union-attr]

    def test_session_mode_switch_requires_idle_empty_queue(self) -> None:
        self.db.register_notify(event(self.workspace))
        managed = self.db.set_session_mode("thread-001", "MANAGED")
        self.assertEqual(managed.mode, "MANAGED")
        self.assertEqual(managed.status, "IDLE")

        job, _ = self.db.create_job("thread-001", 9101, "queued")
        with self.assertRaisesRegex(DatabaseInvariantError, "queued or dispatched"):
            self.db.set_session_mode("thread-001", "LIVE")
        self.db.cancel_queued_job("thread-001", job.id)
        live = self.db.set_session_mode("thread-001", "LIVE")
        self.assertEqual(live.mode, "LIVE")

    def test_managed_dispatched_job_is_failed_on_restart_recovery(self) -> None:
        self.db.register_notify(event(self.workspace))
        self.db.set_session_mode("thread-001", "MANAGED")
        self.db.set_session_auto_run("thread-001", True)
        job, _ = self.db.create_job("thread-001", 9201, "managed work")
        self.db.begin_job_dispatch(job.id)

        self.assertEqual(self.db.recover_job_session_state(), 1)
        recovered = self.db.get_job(job.id)
        assert recovered is not None
        self.assertEqual(recovered.status, "FAILED")
        self.assertIn("restarted during managed execution", recovered.error or "")
        recovered_session = self.db.get_session("thread-001")
        assert recovered_session is not None
        self.assertEqual(recovered_session.status, "ERROR")
        self.assertFalse(recovered_session.auto_run)

    def test_active_managed_job_can_be_cancelled_after_process_stops(self) -> None:
        self.db.register_notify(event(self.workspace))
        self.db.set_session_mode("thread-001", "MANAGED")
        job, _ = self.db.create_job("thread-001", 9301, "managed work")
        self.db.begin_job_dispatch(job.id)
        cancelled = self.db.cancel_dispatched_job("thread-001", job.id)
        self.assertEqual(cancelled.status, "CANCELLED")
        self.assertEqual(self.db.get_session("thread-001").status, "IDLE")  # type: ignore[union-attr]

    def test_newer_schema_is_rejected(self) -> None:
        self.db.close()
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("PRAGMA user_version = 99")
            connection.commit()
        finally:
            connection.close()

        newer = Database(self.db_path)
        with self.assertRaisesRegex(DatabaseError, "newer than supported"):
            newer.open()
        self.assertFalse(newer.is_open)


if __name__ == "__main__":
    unittest.main()
