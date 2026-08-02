#!/usr/bin/env python3
"""SQLite schema and connection lifecycle for the X watcher."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts import watcher_constants


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        _migrate_database(connection)
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
        raise
    return connection


def _database_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _require_known_identity(
    connection: sqlite3.Connection,
    application_id: int,
) -> None:
    if application_id == watcher_constants.DATABASE_APPLICATION_ID:
        return
    if application_id != 0:
        raise RuntimeError(
            "SQLite file belongs to another application: "
            f"application_id={application_id}"
        )
    tables = _database_tables(connection)
    legacy_core = {"events", "meta", "poll_runs"}
    if tables and not legacy_core.issubset(tables):
        raise RuntimeError(
            "Unidentified SQLite file lacks the watcher legacy schema"
        )


def _require_wal(connection: sqlite3.Connection) -> None:
    current = str(
        connection.execute("PRAGMA journal_mode").fetchone()[0]
    ).lower()
    if current == "wal":
        return
    enabled = str(
        connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    ).lower()
    if enabled != "wal":
        raise RuntimeError(f"Unable to enable SQLite WAL mode: {enabled}")


def _migrate_database(connection: sqlite3.Connection) -> None:
    application_id = int(
        connection.execute("PRAGMA application_id").fetchone()[0]
    )
    _require_known_identity(connection, application_id)
    current_version = int(
        connection.execute("PRAGMA user_version").fetchone()[0]
    )
    if current_version > watcher_constants.SCHEMA_VERSION:
        raise RuntimeError(
            "Database schema is newer than this runtime: "
            f"{current_version} > {watcher_constants.SCHEMA_VERSION}"
        )
    _require_wal(connection)
    if current_version == watcher_constants.SCHEMA_VERSION:
        if application_id != watcher_constants.DATABASE_APPLICATION_ID:
            raise RuntimeError("Current database schema lacks application ID")
        return
    connection.executescript(
        """
        BEGIN IMMEDIATE;

        CREATE TABLE IF NOT EXISTS events (
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

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS poll_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            new_count INTEGER NOT NULL,
            returned_count INTEGER NOT NULL DEFAULT 0,
            request_count INTEGER NOT NULL DEFAULT 0,
            error_class TEXT,
            error_message TEXT
        );

        CREATE TABLE IF NOT EXISTS conversation_chains (
            chain_id TEXT PRIMARY KEY,
            root_status_id TEXT NOT NULL,
            provenance TEXT NOT NULL,
            chatgpt_conversation_url TEXT,
            ledger_reference TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversation_turns (
            status_id TEXT PRIMARY KEY,
            chain_id TEXT NOT NULL,
            parent_status_id TEXT,
            actor TEXT NOT NULL,
            author TEXT,
            url TEXT NOT NULL,
            exact_text TEXT NOT NULL,
            media_json TEXT NOT NULL DEFAULT '[]',
            posted_at TEXT,
            observed_at TEXT NOT NULL,
            provenance TEXT,
            FOREIGN KEY(chain_id) REFERENCES conversation_chains(chain_id)
        );

        CREATE INDEX IF NOT EXISTS conversation_turns_chain_idx
        ON conversation_turns(chain_id, posted_at, status_id);

        CREATE INDEX IF NOT EXISTS conversation_turns_parent_actor_idx
        ON conversation_turns(parent_status_id, actor, posted_at, status_id);

        CREATE INDEX IF NOT EXISTS events_author_idx
        ON events(author_id, created_at, event_id);

        CREATE INDEX IF NOT EXISTS events_username_idx
        ON events(username, created_at, event_id);

        CREATE TABLE IF NOT EXISTS conversation_sources (
            status_id TEXT NOT NULL,
            source_url TEXT NOT NULL,
            PRIMARY KEY(status_id, source_url),
            FOREIGN KEY(status_id) REFERENCES conversation_turns(status_id)
        );

        CREATE TABLE IF NOT EXISTS chatgpt_conversation_migrations (
            migration_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain_id TEXT NOT NULL,
            source_url TEXT NOT NULL,
            target_url TEXT NOT NULL,
            source_model TEXT NOT NULL,
            target_model TEXT NOT NULL,
            branch_from_status_id TEXT NOT NULL,
            method TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            verified_at TEXT NOT NULL,
            UNIQUE(chain_id, source_url, target_url),
            UNIQUE(chain_id, target_url),
            FOREIGN KEY(chain_id) REFERENCES conversation_chains(chain_id),
            FOREIGN KEY(branch_from_status_id)
                REFERENCES conversation_turns(status_id)
        );

        CREATE TABLE IF NOT EXISTS event_resolutions (
            event_id TEXT PRIMARY KEY,
            disposition TEXT NOT NULL,
            reason TEXT NOT NULL,
            reply_url TEXT,
            blocker_code TEXT,
            stance TEXT,
            stance_detail TEXT,
            confidence TEXT,
            media_meaning TEXT,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            resolved_at TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES events(event_id)
        );

        CREATE TABLE IF NOT EXISTS event_resolution_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            previous_json TEXT NOT NULL,
            replacement_json TEXT NOT NULL,
            revision_reason TEXT NOT NULL,
            revised_at TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES events(event_id)
        );

        CREATE TABLE IF NOT EXISTS response_policy_requeues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            previous_resolution_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            requeued_at TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES events(event_id)
        );

        CREATE TABLE IF NOT EXISTS archive_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            archive_fingerprint TEXT NOT NULL UNIQUE,
            account_user_id TEXT NOT NULL,
            username TEXT NOT NULL,
            source_name TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            post_count INTEGER NOT NULL,
            inserted_post_count INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS archive_posts (
            status_id TEXT PRIMARY KEY,
            first_import_id INTEGER NOT NULL,
            author_id TEXT NOT NULL,
            username_at_import TEXT NOT NULL,
            posted_at TEXT NOT NULL,
            exact_text TEXT NOT NULL,
            parent_status_id TEXT,
            counterparty_user_id TEXT,
            counterparty_username TEXT,
            conversation_id TEXT,
            canonical_url TEXT NOT NULL,
            source_member TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            FOREIGN KEY(first_import_id) REFERENCES archive_imports(id)
        );

        CREATE INDEX IF NOT EXISTS archive_posts_counterparty_idx
        ON archive_posts(counterparty_user_id, posted_at, status_id);

        CREATE INDEX IF NOT EXISTS archive_posts_parent_idx
        ON archive_posts(parent_status_id, posted_at, status_id);

        CREATE TABLE IF NOT EXISTS archive_account_aliases (
            account_user_id TEXT NOT NULL,
            username TEXT NOT NULL COLLATE NOCASE,
            first_import_id INTEGER NOT NULL,
            last_import_id INTEGER NOT NULL,
            PRIMARY KEY(account_user_id, username),
            FOREIGN KEY(first_import_id) REFERENCES archive_imports(id),
            FOREIGN KEY(last_import_id) REFERENCES archive_imports(id)
        );

        CREATE TABLE IF NOT EXISTS candidate_corpus_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            corpus_fingerprint TEXT NOT NULL UNIQUE,
            subject_user_id TEXT NOT NULL,
            expected_handle TEXT NOT NULL,
            source_name TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            record_count INTEGER NOT NULL,
            inserted_record_count INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS candidate_public_posts (
            status_id TEXT PRIMARY KEY,
            first_import_id INTEGER NOT NULL,
            subject_user_id TEXT NOT NULL,
            account_handle TEXT NOT NULL,
            created_at TEXT,
            exact_text TEXT,
            text_state TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            source_verification_state TEXT NOT NULL,
            trust_state TEXT NOT NULL
                CHECK(trust_state = 'unverified_candidate'),
            source_file TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            FOREIGN KEY(first_import_id)
                REFERENCES candidate_corpus_imports(id)
        );

        CREATE INDEX IF NOT EXISTS candidate_public_posts_subject_idx
        ON candidate_public_posts(subject_user_id, created_at, status_id);

        CREATE TABLE IF NOT EXISTS candidate_post_verifications (
            status_id TEXT PRIMARY KEY,
            exact_text TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            verification_method TEXT NOT NULL,
            verified_by TEXT NOT NULL,
            exact_text_sha256 TEXT NOT NULL,
            FOREIGN KEY(status_id) REFERENCES candidate_public_posts(status_id)
        );
        """
    )
    resolution_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(event_resolutions)")
    }
    for column, declaration in {
        "blocker_code": "TEXT",
        "stance": "TEXT",
        "stance_detail": "TEXT",
        "confidence": "TEXT",
        "media_meaning": "TEXT",
        "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
    }.items():
        if column not in resolution_columns:
            connection.execute(
                f"ALTER TABLE event_resolutions ADD COLUMN {column} {declaration}"
            )
    turn_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(conversation_turns)")
    }
    if "media_json" not in turn_columns:
        connection.execute(
            "ALTER TABLE conversation_turns "
            "ADD COLUMN media_json TEXT NOT NULL DEFAULT '[]'"
        )
    poll_run_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(poll_runs)")
    }
    for column in ("returned_count", "request_count"):
        if column not in poll_run_columns:
            connection.execute(
                f"ALTER TABLE poll_runs ADD COLUMN {column} "
                "INTEGER NOT NULL DEFAULT 0"
            )
    connection.execute(
        """
        UPDATE conversation_turns AS turn
        SET parent_status_id = (
            SELECT resolution.event_id
            FROM event_resolutions AS resolution
            WHERE resolution.disposition = 'published'
              AND resolution.reply_url = turn.url
              AND turn.status_id = substr(
                  resolution.reply_url,
                  length('https://x.com/axrbarsic/status/') + 1
              )
        )
        WHERE turn.actor = 'alex'
          AND turn.parent_status_id IS NULL
          AND EXISTS (
              SELECT 1
              FROM event_resolutions AS resolution
              WHERE resolution.disposition = 'published'
                AND resolution.reply_url = turn.url
                AND turn.status_id = substr(
                    resolution.reply_url,
                    length('https://x.com/axrbarsic/status/') + 1
                )
          )
        """
    )
    connection.execute(
        f"PRAGMA application_id={watcher_constants.DATABASE_APPLICATION_ID}"
    )
    connection.execute(
        f"PRAGMA user_version={watcher_constants.SCHEMA_VERSION}"
    )
    connection.commit()
