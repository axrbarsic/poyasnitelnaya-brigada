from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import watcher_constants, watcher_database


class WatcherDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "watcher.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_new_database_records_current_schema_version(self) -> None:
        connection = watcher_database.connect_database(self.database)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            application_id = connection.execute(
                "PRAGMA application_id"
            ).fetchone()[0]
            resolution_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(event_resolutions)"
                )
            }
        finally:
            connection.close()

        self.assertEqual(version, watcher_constants.SCHEMA_VERSION)
        self.assertEqual(
            application_id,
            watcher_constants.DATABASE_APPLICATION_ID,
        )
        self.assertTrue(
            {
                "blocker_code",
                "stance",
                "stance_detail",
                "confidence",
                "media_meaning",
                "evidence_json",
            }.issubset(resolution_columns)
        )

    def test_legacy_database_migrates_columns_and_parent_backfill(self) -> None:
        target_id = "7000000000000000000"
        reply_id = "7000000000000000001"
        reply_url = f"https://x.com/axrbarsic/status/{reply_id}"
        connection = sqlite3.connect(self.database)
        connection.executescript(
            """
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                author_id TEXT,
                username TEXT,
                created_at TEXT,
                conversation_id TEXT,
                in_reply_to_user_id TEXT,
                is_reply INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                delivery_state TEXT NOT NULL DEFAULT 'queued'
            );
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE conversation_chains (
                chain_id TEXT PRIMARY KEY,
                root_status_id TEXT NOT NULL,
                provenance TEXT NOT NULL,
                chatgpt_conversation_url TEXT,
                ledger_reference TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE conversation_turns (
                status_id TEXT PRIMARY KEY,
                chain_id TEXT NOT NULL,
                parent_status_id TEXT,
                actor TEXT NOT NULL,
                author TEXT,
                url TEXT NOT NULL,
                exact_text TEXT NOT NULL,
                posted_at TEXT,
                observed_at TEXT NOT NULL,
                provenance TEXT
            );
            CREATE TABLE event_resolutions (
                event_id TEXT PRIMARY KEY,
                disposition TEXT NOT NULL,
                reason TEXT NOT NULL,
                reply_url TEXT,
                resolved_at TEXT NOT NULL
            );
            CREATE TABLE poll_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                new_count INTEGER NOT NULL,
                error_class TEXT,
                error_message TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO conversation_chains VALUES(?, ?, 'short', NULL, "
            "NULL, '2026-08-02T00:00:00Z', '2026-08-02T00:00:00Z')",
            (target_id, target_id),
        )
        connection.execute(
            "INSERT INTO conversation_turns VALUES(?, ?, NULL, 'alex', "
            "'@axrbarsic', ?, 'reply', NULL, '2026-08-02T00:00:00Z', "
            "'self_authored_short_sol')",
            (reply_id, target_id, reply_url),
        )
        connection.execute(
            "INSERT INTO event_resolutions VALUES(?, 'published', 'done', "
            "?, '2026-08-02T00:00:00Z')",
            (target_id, reply_url),
        )
        connection.commit()
        connection.close()

        migrated = watcher_database.connect_database(self.database)
        try:
            version = migrated.execute("PRAGMA user_version").fetchone()[0]
            parent = migrated.execute(
                "SELECT parent_status_id FROM conversation_turns "
                "WHERE status_id = ?",
                (reply_id,),
            ).fetchone()[0]
            turn_columns = {
                str(row["name"])
                for row in migrated.execute(
                    "PRAGMA table_info(conversation_turns)"
                )
            }
            poll_columns = {
                str(row["name"])
                for row in migrated.execute("PRAGMA table_info(poll_runs)")
            }
        finally:
            migrated.close()

        self.assertEqual(version, watcher_constants.SCHEMA_VERSION)
        self.assertEqual(parent, target_id)
        self.assertIn("media_json", turn_columns)
        self.assertTrue({"returned_count", "request_count"}.issubset(poll_columns))

    def test_current_database_skips_legacy_backfill(self) -> None:
        connection = watcher_database.connect_database(self.database)
        connection.executescript(
            """
            CREATE TRIGGER reject_parent_backfill
            BEFORE UPDATE OF parent_status_id ON conversation_turns
            BEGIN
                SELECT RAISE(ABORT, 'legacy migration reran');
            END;
            """
        )
        connection.commit()
        connection.close()

        reopened = watcher_database.connect_database(self.database)
        reopened.close()

    def test_future_database_version_fails_closed(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute(
            f"PRAGMA user_version={watcher_constants.SCHEMA_VERSION + 1}"
        )
        connection.close()

        with self.assertRaisesRegex(RuntimeError, "newer than this runtime"):
            watcher_database.connect_database(self.database)

    def test_foreign_application_id_fails_closed(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA application_id=123456")
        connection.close()

        with self.assertRaisesRegex(RuntimeError, "another application"):
            watcher_database.connect_database(self.database)

    def test_unidentified_zero_application_id_fails_without_mutation(
        self,
    ) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(RuntimeError, "Unidentified SQLite"):
            watcher_database.connect_database(self.database)

        connection = sqlite3.connect(self.database)
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            journal_mode = connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(tables, {"unrelated"})
        self.assertNotEqual(str(journal_mode).lower(), "wal")


if __name__ == "__main__":
    unittest.main()
