#!/usr/bin/env python3
"""Token-free X mention polling, durable queueing, and health monitoring."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import subprocess
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 9
TOKEN_ENV_NAMES = ("X_BEARER_TOKEN", "X_API_BEARER_TOKEN", "TWITTER_BEARER_TOKEN")
INITIAL_AUDIT_EXPIRY_PROVENANCE = "stored_api_auto_expiry_v1"
TERMINAL_BLOCKER_CODES = {
    "account_unavailable",
    "missing_historical_pro_conversation",
    "reply_restricted",
    "required_pro_model_unavailable",
    "safety_restriction",
    "target_screenshot_unavailable",
    "target_unavailable",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Config:
    source_path: Path
    user_id: str
    database: Path
    lock_file: Path
    health_file: Path
    wake_file: Path
    alert_file: Path
    poll_interval_seconds: int
    stale_after_seconds: int
    failure_threshold: int
    silence_review_seconds: int
    watchdog_interval_seconds: int
    request_timeout_seconds: int
    max_pages_per_poll: int
    api_base: str
    keychain_helper: Path
    keychain_service: str
    keychain_account: str
    queue_direct_replies_only: bool
    mandatory_response_mode: bool
    conversation_tail_enabled: bool
    conversation_tail_poll_interval_seconds: int
    conversation_tail_watch_hours: int
    conversation_tail_initial_lookback_hours: int
    conversation_tail_overlap_seconds: int
    conversation_tail_max_conversations: int
    commenter_memory_limit: int
    notifications_enabled: bool


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def load_config(path: Path) -> Config:
    raw = read_json(path)
    base = path.resolve().parent
    config = Config(
        source_path=path.resolve(),
        user_id=str(raw.get("user_id", "")).strip(),
        database=_resolve(base, raw.get("database", "var/watcher.sqlite3")),
        lock_file=_resolve(base, raw.get("lock_file", "var/poll.lock")),
        health_file=_resolve(base, raw.get("health_file", "var/health.json")),
        wake_file=_resolve(base, raw.get("wake_file", "var/wake-request.json")),
        alert_file=_resolve(base, raw.get("alert_file", "var/watchdog-alert.json")),
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 300)),
        stale_after_seconds=int(raw.get("stale_after_seconds", 900)),
        failure_threshold=int(raw.get("failure_threshold", 2)),
        silence_review_seconds=int(raw.get("silence_review_seconds", 21600)),
        watchdog_interval_seconds=int(raw.get("watchdog_interval_seconds", 60)),
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 30)),
        max_pages_per_poll=int(raw.get("max_pages_per_poll", 20)),
        api_base=str(raw.get("api_base", "https://api.x.com/2")).rstrip("/"),
        keychain_helper=_resolve(base, raw.get("keychain_helper", "var/keychain-helper")),
        keychain_service=str(raw.get("keychain_service", "")).strip(),
        keychain_account=str(raw.get("keychain_account", "")).strip(),
        queue_direct_replies_only=bool(raw.get("queue_direct_replies_only", True)),
        mandatory_response_mode=bool(raw.get("mandatory_response_mode", False)),
        conversation_tail_enabled=bool(
            raw.get("conversation_tail_enabled", False)
        ),
        conversation_tail_poll_interval_seconds=int(
            raw.get("conversation_tail_poll_interval_seconds", 60)
        ),
        conversation_tail_watch_hours=int(
            raw.get("conversation_tail_watch_hours", 24)
        ),
        conversation_tail_initial_lookback_hours=int(
            raw.get("conversation_tail_initial_lookback_hours", 3)
        ),
        conversation_tail_overlap_seconds=int(
            raw.get("conversation_tail_overlap_seconds", 120)
        ),
        conversation_tail_max_conversations=int(
            raw.get("conversation_tail_max_conversations", 80)
        ),
        commenter_memory_limit=int(raw.get("commenter_memory_limit", 12)),
        notifications_enabled=bool(raw.get("notifications_enabled", True)),
    )
    positive_values = {
        "poll_interval_seconds": config.poll_interval_seconds,
        "stale_after_seconds": config.stale_after_seconds,
        "failure_threshold": config.failure_threshold,
        "silence_review_seconds": config.silence_review_seconds,
        "watchdog_interval_seconds": config.watchdog_interval_seconds,
        "request_timeout_seconds": config.request_timeout_seconds,
        "max_pages_per_poll": config.max_pages_per_poll,
        "conversation_tail_poll_interval_seconds": (
            config.conversation_tail_poll_interval_seconds
        ),
        "conversation_tail_watch_hours": config.conversation_tail_watch_hours,
        "conversation_tail_initial_lookback_hours": (
            config.conversation_tail_initial_lookback_hours
        ),
        "conversation_tail_overlap_seconds": (
            config.conversation_tail_overlap_seconds
        ),
        "conversation_tail_max_conversations": (
            config.conversation_tail_max_conversations
        ),
        "commenter_memory_limit": config.commenter_memory_limit,
    }
    invalid = [name for name, value in positive_values.items() if value <= 0]
    if invalid:
        raise ValueError("Config values must be positive: " + ", ".join(invalid))
    return config


def is_eligible_reply(
    connection: sqlite3.Connection,
    config: Config,
    *,
    author_id: str | None = None,
    is_reply: bool,
    in_reply_to_user_id: str | None,
    conversation_id: str | None,
) -> bool:
    if author_id == config.user_id:
        return False
    if not is_reply:
        return False
    if in_reply_to_user_id == config.user_id:
        return True
    if not conversation_id:
        return False
    return connection.execute(
        """
        SELECT 1
        FROM conversation_turns
        WHERE chain_id = ? AND actor = 'alex'
        LIMIT 1
        """,
        (conversation_id,),
    ).fetchone() is not None


def event_mentions_configured_account(
    config: Config,
    event: sqlite3.Row,
) -> bool:
    handle = config.keychain_account.strip().lstrip("@")
    if (
        not handle
        or not bool(event["is_reply"])
        or event["author_id"] == config.user_id
    ):
        return False
    try:
        payload = json.loads(str(event["payload_json"]))
    except (TypeError, ValueError):
        return False
    text = str(payload.get("text") or "")
    pattern = rf"(?<![A-Za-z0-9_])@{re.escape(handle)}(?![A-Za-z0-9_])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
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
    connection.commit()
    return connection


class AlreadyRunningError(RuntimeError):
    """Raised when another poller process owns the lock."""


@contextlib.contextmanager
def exclusive_process_lock(path: Path) -> Iterable[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AlreadyRunningError("Another poller process is already running") from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def get_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """
        INSERT INTO meta(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def delete_meta(connection: sqlite3.Connection, key: str) -> None:
    connection.execute("DELETE FROM meta WHERE key = ?", (key,))


def numeric_max(values: Iterable[str | None]) -> str | None:
    candidates = [value for value in values if value and value.isdigit()]
    return max(candidates, key=int) if candidates else None


def _reclassify_self_authored_events(
    connection: sqlite3.Connection,
    configured_user_id: str,
) -> list[str]:
    rows = connection.execute(
        """
        SELECT event_id
        FROM events
        WHERE author_id = ?
          AND delivery_state != 'self_authored'
          AND NOT EXISTS (
              SELECT 1 FROM event_resolutions r
              WHERE r.event_id = events.event_id
          )
        ORDER BY CAST(event_id AS INTEGER)
        """,
        (configured_user_id,),
    ).fetchall()
    event_ids = [str(row["event_id"]) for row in rows]
    if event_ids:
        connection.executemany(
            """
            UPDATE events
            SET delivery_state = 'self_authored'
            WHERE event_id = ?
            """,
            [(event_id,) for event_id in event_ids],
        )
    return event_ids


def reconcile_self_authored_events(
    config: Config,
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    with connection:
        set_meta(connection, "configured_user_id", config.user_id)
        event_ids = _reclassify_self_authored_events(
            connection,
            config.user_id,
        )
    wake = refresh_wake_file(config, connection)
    health = write_health(config, connection, last_new_count=0)
    history_missing_ids = [
        event_id
        for event_id in event_ids
        if connection.execute(
            """
            SELECT 1
            FROM conversation_turns
            WHERE status_id = ? AND actor = 'alex'
            """,
            (event_id,),
        ).fetchone()
        is None
    ]
    return {
        "status": "self_authored_reconciled",
        "reclassified_count": len(event_ids),
        "reclassified_event_ids": event_ids,
        "history_missing_event_ids": history_missing_ids,
        "pending_count": wake["pending_count"],
        "health": health["status"],
    }


def queued_events(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT event_id, author_id, username, created_at, conversation_id,
               in_reply_to_user_id, is_reply, first_seen_at
        FROM events
        WHERE delivery_state = 'queued'
        ORDER BY created_at DESC, CAST(event_id AS INTEGER) DESC
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        username = row["username"]
        event_id = str(row["event_id"])
        event_url = (
            f"https://x.com/{username}/status/{event_id}"
            if username
            else f"https://x.com/i/web/status/{event_id}"
        )
        result.append(
            {
                "event_id": event_id,
                "event_url": event_url,
                "author_id": row["author_id"],
                "username": username,
                "created_at": row["created_at"],
                "conversation_id": row["conversation_id"],
                "in_reply_to_user_id": row["in_reply_to_user_id"],
                "is_reply": bool(row["is_reply"]),
                "first_seen_at": row["first_seen_at"],
            }
        )
    return result


def _commenter_memory_excerpt(value: str, limit: int = 400) -> dict[str, Any]:
    code_points = list(value)
    truncated = len(code_points) > limit
    excerpt = "".join(code_points[:limit])
    return {
        "text": excerpt,
        "truncated": truncated,
    }


def commenter_history_for_event(
    connection: sqlite3.Connection,
    event_id: str,
    *,
    limit: int,
) -> dict[str, Any]:
    if limit <= 0:
        raise ValueError("Commenter history limit must be positive")
    event_id = _validate_status_id(str(event_id), "event_id")
    current = connection.execute(
        """
        SELECT event_id, author_id, username
        FROM events
        WHERE event_id = ?
        """,
        (event_id,),
    ).fetchone()
    if current is None:
        raise KeyError(f"Unknown event {event_id}")
    author_id = current["author_id"]
    username = current["username"]
    if author_id:
        identity_clause = "e.author_id = ?"
        identity_value = str(author_id)
        identity_kind = "x_user_id"
    elif username:
        identity_clause = "LOWER(e.username) = LOWER(?)"
        identity_value = str(username)
        identity_kind = "x_handle_fallback"
    else:
        return {
            "event_id": event_id,
            "identity_kind": "unavailable",
            "author_id": None,
            "username": None,
            "total_prior_interactions": 0,
            "first_interaction_at": None,
            "last_interaction_at": None,
            "returned_interactions": 0,
            "interactions": [],
            "total_prior_archive_alex_replies": 0,
            "first_archive_alex_reply_at": None,
            "last_archive_alex_reply_at": None,
            "returned_archive_alex_replies": 0,
            "archive_alex_replies": [],
            "total_candidate_public_posts": 0,
            "returned_candidate_public_posts": 0,
            "candidate_public_posts": [],
            "candidate_memory_contract": (
                "Unverified candidates are search hints only. Verify the "
                "exact live X post before quoting or using it as evidence."
            ),
        }

    aggregate = connection.execute(
        f"""
        SELECT COUNT(*) AS count,
               MIN(e.created_at) AS first_interaction_at,
               MAX(e.created_at) AS last_interaction_at
        FROM events e
        WHERE e.event_id != ? AND {identity_clause}
        """,
        (event_id, identity_value),
    ).fetchone()
    rows = connection.execute(
        f"""
        SELECT e.event_id, e.username, e.created_at, e.conversation_id,
               e.payload_json, r.disposition, r.stance, r.stance_detail
        FROM events e
        LEFT JOIN event_resolutions r ON r.event_id = e.event_id
        WHERE e.event_id != ? AND {identity_clause}
        ORDER BY e.created_at DESC, CAST(e.event_id AS INTEGER) DESC
        LIMIT ?
        """,
        (event_id, identity_value, limit),
    ).fetchall()
    interactions: list[dict[str, Any]] = []
    for row in rows:
        status_id = str(row["event_id"])
        payload = json.loads(str(row["payload_json"]))
        exact_text = str(payload.get("text") or "")
        prior_username = row["username"]
        target_url = (
            f"https://x.com/{prior_username}/status/{status_id}"
            if prior_username
            else f"https://x.com/i/web/status/{status_id}"
        )
        alex_rows = connection.execute(
            """
            SELECT status_id, url, exact_text, posted_at
            FROM conversation_turns
            WHERE parent_status_id = ? AND actor = 'alex'
            ORDER BY posted_at, CAST(status_id AS INTEGER)
            LIMIT 3
            """,
            (status_id,),
        ).fetchall()
        alex_replies = [
            {
                "status_id": str(reply["status_id"]),
                "url": str(reply["url"]),
                "posted_at": reply["posted_at"],
                **_commenter_memory_excerpt(str(reply["exact_text"])),
            }
            for reply in alex_rows
        ]
        interactions.append(
            {
                "status_id": status_id,
                "url": target_url,
                "created_at": row["created_at"],
                "conversation_id": row["conversation_id"],
                "disposition": row["disposition"],
                "stance": row["stance"],
                "stance_detail": row["stance_detail"],
                **_commenter_memory_excerpt(exact_text),
                "alex_replies": alex_replies,
            }
        )
    archive_replies: list[dict[str, Any]] = []
    archive_aggregate = {
        "count": 0,
        "first_reply_at": None,
        "last_reply_at": None,
    }
    candidate_posts: list[dict[str, Any]] = []
    candidate_total = 0
    if author_id:
        archive_aggregate_row = connection.execute(
            """
            SELECT COUNT(*) AS count,
                   MIN(post.posted_at) AS first_reply_at,
                   MAX(post.posted_at) AS last_reply_at
            FROM archive_posts AS post
            LEFT JOIN conversation_turns AS turn
              ON turn.status_id = post.status_id
            WHERE post.counterparty_user_id = ?
              AND turn.status_id IS NULL
            """,
            (str(author_id),),
        ).fetchone()
        archive_aggregate = {
            "count": int(archive_aggregate_row["count"]),
            "first_reply_at": archive_aggregate_row["first_reply_at"],
            "last_reply_at": archive_aggregate_row["last_reply_at"],
        }
        archive_limit = min(limit, max(3, math.ceil(limit / 3)))
        archive_rows = connection.execute(
            """
            SELECT post.status_id, post.parent_status_id, post.posted_at,
                   post.exact_text, post.canonical_url,
                   post.counterparty_username, post.source_member
            FROM archive_posts AS post
            LEFT JOIN conversation_turns AS turn
              ON turn.status_id = post.status_id
            WHERE post.counterparty_user_id = ?
              AND turn.status_id IS NULL
            ORDER BY post.posted_at DESC, CAST(post.status_id AS INTEGER) DESC
            LIMIT ?
            """,
            (str(author_id), archive_limit),
        ).fetchall()
        archive_replies = [
            {
                "status_id": str(reply["status_id"]),
                "parent_status_id": reply["parent_status_id"],
                "url": str(reply["canonical_url"]),
                "posted_at": reply["posted_at"],
                "counterparty_username": reply["counterparty_username"],
                "source_kind": "official_x_archive_alex_reply",
                "source_member": str(reply["source_member"]),
                **_commenter_memory_excerpt(str(reply["exact_text"])),
            }
            for reply in archive_rows
        ]
        candidate_total = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM candidate_public_posts
                WHERE subject_user_id = ?
                """,
                (str(author_id),),
            ).fetchone()[0]
        )
        candidate_limit = min(limit, 3)
        candidate_rows = connection.execute(
            """
            SELECT post.status_id, post.created_at,
                   post.exact_text AS candidate_text,
                   post.canonical_url AS candidate_url,
                   post.text_state, post.source_verification_state,
                   post.source_file,
                   verification.exact_text AS verified_exact_text,
                   verification.canonical_url AS verified_url,
                   verification.observed_at AS verified_at,
                   verification.verification_method
            FROM candidate_public_posts AS post
            LEFT JOIN candidate_post_verifications AS verification
              ON verification.status_id = post.status_id
            WHERE post.subject_user_id = ?
            ORDER BY post.created_at DESC,
                     CAST(post.status_id AS INTEGER) DESC
            LIMIT ?
            """,
            (str(author_id), candidate_limit),
        ).fetchall()
        for post in candidate_rows:
            verified = post["verified_exact_text"] is not None
            candidate_text = (
                str(post["verified_exact_text"])
                if verified
                else str(post["candidate_text"] or "")
            )
            candidate_posts.append(
                {
                    "status_id": str(post["status_id"]),
                    "url": str(
                        post["verified_url"]
                        if verified
                        else post["candidate_url"]
                    ),
                    "created_at": post["created_at"],
                    "text_state": str(post["text_state"]),
                    "source_verification_state": str(
                        post["source_verification_state"]
                    ),
                    "source_file": str(post["source_file"]),
                    "verification_method": post["verification_method"],
                    "verified_at": post["verified_at"],
                    "usable_as_evidence": verified,
                    **_commenter_memory_excerpt(candidate_text),
                }
            )
    return {
        "event_id": event_id,
        "identity_kind": identity_kind,
        "author_id": str(author_id) if author_id else None,
        "username": username,
        "total_prior_interactions": int(aggregate["count"]),
        "first_interaction_at": aggregate["first_interaction_at"],
        "last_interaction_at": aggregate["last_interaction_at"],
        "returned_interactions": len(interactions),
        "interactions": interactions,
        "total_prior_archive_alex_replies": archive_aggregate["count"],
        "first_archive_alex_reply_at": archive_aggregate["first_reply_at"],
        "last_archive_alex_reply_at": archive_aggregate["last_reply_at"],
        "returned_archive_alex_replies": len(archive_replies),
        "archive_alex_replies": archive_replies,
        "total_candidate_public_posts": candidate_total,
        "returned_candidate_public_posts": len(candidate_posts),
        "candidate_public_posts": candidate_posts,
        "candidate_memory_contract": (
            "Unverified candidates are search hints only. Verify the exact "
            "live X post before quoting or using it as evidence."
        ),
    }


def refresh_wake_file(config: Config, connection: sqlite3.Connection) -> dict[str, Any]:
    events = queued_events(connection)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": isoformat(),
        "pending_count": len(events),
        "events": events,
    }
    wake_lock = config.wake_file.with_suffix(config.wake_file.suffix + ".lock")
    wake_lock.parent.mkdir(parents=True, exist_ok=True)
    with wake_lock.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            atomic_write_json(config.wake_file, payload)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return payload


def ingest_response(
    config: Config,
    connection: sqlite3.Connection,
    response: dict[str, Any],
    *,
    source: str,
    started_at: str | None = None,
) -> dict[str, Any]:
    started = started_at or isoformat()
    observed_at = isoformat()
    users = {
        str(user.get("id")): str(user.get("username"))
        for user in response.get("includes", {}).get("users", [])
        if user.get("id") and user.get("username")
    }
    media = {
        str(item.get("media_key")): item
        for item in response.get("includes", {}).get("media", [])
        if item.get("media_key")
    }
    new_ids: list[str] = []
    observed_ids: list[str] = []
    seen_ids: list[str] = []
    live_bootstrap = (
        source == "x_api"
        and get_meta(connection, "first_success_at") is None
        and get_meta(connection, "since_id") is None
    )

    with connection:
        set_meta(connection, "configured_user_id", config.user_id)
        reclassified_self_authored_ids = _reclassify_self_authored_events(
            connection,
            config.user_id,
        )
        observed_self_authored_ids: list[str] = []
        for event in response.get("data", []) or []:
            event_id = str(event.get("id", "")).strip()
            if not event_id:
                continue
            stored_event = dict(event)
            media_keys = (
                (event.get("attachments") or {}).get("media_keys") or []
            )
            included_media = [
                media[str(media_key)]
                for media_key in media_keys
                if str(media_key) in media
            ]
            if included_media:
                stored_event["included_media"] = included_media
            seen_ids.append(event_id)
            referenced = event.get("referenced_tweets") or []
            is_reply = any(item.get("type") == "replied_to" for item in referenced)
            author_id = str(event.get("author_id", "")).strip() or None
            in_reply_to_user_id = (
                str(event.get("in_reply_to_user_id", "")).strip() or None
            )
            conversation_id = (
                str(event.get("conversation_id", "")).strip() or None
            )
            eligible_reply = is_eligible_reply(
                connection,
                config,
                author_id=author_id,
                is_reply=is_reply,
                in_reply_to_user_id=in_reply_to_user_id,
                conversation_id=conversation_id,
            )
            if (
                author_id != config.user_id
                and config.mandatory_response_mode
                and source == "x_api"
                and is_reply
            ):
                # The endpoint itself is the authenticated user's mentions
                # timeline. In mandatory mode, a reply to another participant
                # can still be an eligible continuation when X carries
                # @axrbarsic through the thread participant list. Queue it for
                # live Browser inspection instead of trusting an incomplete
                # local history backfill to prove the conversation route.
                eligible_reply = True
            if author_id == config.user_id:
                delivery_state = "self_authored"
            elif config.queue_direct_replies_only and not eligible_reply:
                delivery_state = "ignored"
            else:
                delivery_state = "queued"
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO events(
                    event_id, author_id, username, created_at, conversation_id,
                    in_reply_to_user_id, is_reply, payload_json, first_seen_at,
                    delivery_state
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    author_id,
                    users.get(author_id or ""),
                    event.get("created_at"),
                    conversation_id,
                    in_reply_to_user_id,
                    1 if is_reply else 0,
                    json.dumps(stored_event, ensure_ascii=False, sort_keys=True),
                    observed_at,
                    delivery_state,
                ),
            )
            if cursor.rowcount == 1:
                observed_ids.append(event_id)
                if delivery_state == "queued":
                    new_ids.append(event_id)
                elif delivery_state == "self_authored":
                    observed_self_authored_ids.append(event_id)

        if source != "x_api_conversation_tail":
            previous_cursor = get_meta(connection, "since_id")
            newest_id = numeric_max(
                [
                    previous_cursor,
                    response.get("meta", {}).get("newest_id"),
                    *seen_ids,
                ]
            )
            if newest_id:
                set_meta(connection, "since_id", newest_id)
        if source == "x_api_conversation_tail":
            set_meta(
                connection,
                "conversation_tail_last_success_at",
                observed_at,
            )
        if get_meta(connection, "first_success_at") is None:
            set_meta(connection, "first_success_at", observed_at)
        if live_bootstrap and get_meta(connection, "initial_audit_started_at") is None:
            set_meta(connection, "initial_audit_started_at", observed_at)
            delete_meta(connection, "initial_audit_completed_at")
        set_meta(connection, "last_success_at", observed_at)
        set_meta(connection, "consecutive_failures", "0")
        delete_meta(connection, "last_error_class")
        delete_meta(connection, "last_error_message")
        if new_ids:
            set_meta(connection, "last_new_event_at", observed_at)
        connection.execute(
            """
            INSERT INTO poll_runs(
                started_at, completed_at, source, status, new_count
            ) VALUES(?, ?, ?, 'success', ?)
            """,
            (started, observed_at, source, len(new_ids)),
        )

    health = write_health(config, connection, last_new_count=len(new_ids))
    wake = refresh_wake_file(config, connection)
    if new_ids:
        notify_macos(
            config,
            "Новые ответы в X",
            f"В очереди {wake['pending_count']}, новых {len(new_ids)}.",
        )
    return {
        "source": source,
        "bootstrap": live_bootstrap,
        "observed_count": len(observed_ids),
        "new_count": len(new_ids),
        "new_event_ids": sorted(new_ids, key=int),
        "self_authored_event_ids": sorted(
            set(
                reclassified_self_authored_ids
                + observed_self_authored_ids
            ),
            key=int,
        ),
        "since_id": get_meta(connection, "since_id"),
        "pending_count": wake["pending_count"],
        "health": health["status"],
    }


def baseline_existing_queue(
    config: Config,
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    with connection:
        cursor = connection.execute(
            "UPDATE events SET delivery_state = 'baseline' "
            "WHERE delivery_state = 'queued'"
        )
    wake = refresh_wake_file(config, connection)
    health = write_health(config, connection, last_new_count=0)
    return {
        "baselined_count": int(cursor.rowcount),
        "pending_count": wake["pending_count"],
        "since_id": get_meta(connection, "since_id"),
        "health": health["status"],
    }


def _blocked_resolution_contract_status(
    connection: sqlite3.Connection,
    configured_user_id: str,
) -> tuple[int, int]:
    rows = connection.execute(
        """
        SELECT r.blocker_code
        FROM event_resolutions r
        JOIN events e ON e.event_id = r.event_id
        WHERE e.is_reply = 1
          AND e.in_reply_to_user_id = ?
          AND COALESCE(e.author_id, '') != ?
          AND r.disposition = 'blocked'
        """,
        (configured_user_id, configured_user_id),
    ).fetchall()
    missing = sum(
        row["blocker_code"] not in TERMINAL_BLOCKER_CODES
        for row in rows
    )
    return len(rows), missing


def initial_audit_status(connection: sqlite3.Connection) -> dict[str, Any]:
    configured_user_id = get_meta(connection, "configured_user_id")
    if configured_user_id is None:
        return {
            "started_at": get_meta(connection, "initial_audit_started_at"),
            "cycle_started_at": get_meta(
                connection,
                "initial_audit_cycle_started_at",
            ),
            "cycle_expiry_as_of": get_meta(
                connection,
                "initial_audit_cycle_expiry_as_of",
            ),
            "cycle_expiry_hours": get_meta(
                connection,
                "initial_audit_cycle_expiry_hours",
            ),
            "completed_at": get_meta(connection, "initial_audit_completed_at"),
            "complete": get_meta(connection, "initial_audit_completed_at") is not None,
            "direct_events": 0,
            "pending_events": 0,
            "queued_events": 0,
            "resolved_events": 0,
            "history_events": 0,
            "history_missing_events": 0,
            "published_resolutions": 0,
            "blocked_resolutions": 0,
            "blocked_resolutions_missing_code": 0,
            "blocked_contract_complete": True,
            "published_alex_history_missing": 0,
            "published_alex_history_complete": True,
            "history_complete": True,
            "invariant_ok": True,
        }
    direct_total = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM events e
            WHERE e.is_reply = 1
              AND e.in_reply_to_user_id = ?
              AND COALESCE(e.author_id, '') != ?
            """,
            (configured_user_id, configured_user_id),
        ).fetchone()["count"]
    )
    unresolved = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM events e
            WHERE e.is_reply = 1
              AND e.in_reply_to_user_id = ?
              AND COALESCE(e.author_id, '') != ?
              AND (
                  NOT EXISTS (
                      SELECT 1 FROM event_resolutions r
                      WHERE r.event_id = e.event_id
                  )
                  OR (
                      e.delivery_state = 'queued'
                      AND EXISTS (
                          SELECT 1 FROM event_resolutions r
                          WHERE r.event_id = e.event_id
                            AND r.disposition = 'skip'
                      )
                  )
              )
            """,
            (configured_user_id, configured_user_id),
        ).fetchone()["count"]
    )
    queued = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM events e
            WHERE e.is_reply = 1
              AND e.in_reply_to_user_id = ?
              AND COALESCE(e.author_id, '') != ?
              AND e.delivery_state = 'queued'
              AND NOT EXISTS (
                  SELECT 1 FROM event_resolutions r
                  WHERE r.event_id = e.event_id
                    AND r.disposition IN ('published', 'blocked')
              )
            """,
            (configured_user_id, configured_user_id),
        ).fetchone()["count"]
    )
    resolved = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM event_resolutions r
            JOIN events e ON e.event_id = r.event_id
            WHERE e.is_reply = 1 AND e.in_reply_to_user_id = ?
              AND COALESCE(e.author_id, '') != ?
              AND NOT (
                  e.delivery_state = 'queued'
                  AND r.disposition = 'skip'
              )
            """,
            (configured_user_id, configured_user_id),
        ).fetchone()["count"]
    )
    history_missing = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM event_resolutions r
            JOIN events e ON e.event_id = r.event_id
            LEFT JOIN conversation_turns t ON t.status_id = r.event_id
            WHERE e.is_reply = 1
              AND e.in_reply_to_user_id = ?
              AND COALESCE(e.author_id, '') != ?
              AND NOT (
                  e.delivery_state = 'queued'
                  AND r.disposition = 'skip'
              )
              AND t.status_id IS NULL
            """,
            (configured_user_id, configured_user_id),
        ).fetchone()["count"]
    )
    published = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM event_resolutions r
            JOIN events e ON e.event_id = r.event_id
            WHERE e.is_reply = 1
              AND e.in_reply_to_user_id = ?
              AND COALESCE(e.author_id, '') != ?
              AND r.disposition = 'published'
            """,
            (configured_user_id, configured_user_id),
        ).fetchone()["count"]
    )
    blocked, blocked_missing_code = _blocked_resolution_contract_status(
        connection,
        configured_user_id,
    )
    published_alex_history_missing = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM event_resolutions r
            JOIN events e ON e.event_id = r.event_id
            LEFT JOIN conversation_turns alex
              ON alex.status_id = substr(
                  r.reply_url,
                  length('https://x.com/axrbarsic/status/') + 1
              )
             AND alex.actor = 'alex'
             AND alex.parent_status_id = r.event_id
             AND alex.url = r.reply_url
            WHERE e.is_reply = 1
              AND e.in_reply_to_user_id = ?
              AND COALESCE(e.author_id, '') != ?
              AND r.disposition = 'published'
              AND (
                  r.reply_url NOT LIKE
                      'https://x.com/axrbarsic/status/%'
                  OR alex.status_id IS NULL
              )
            """,
            (configured_user_id, configured_user_id),
        ).fetchone()["count"]
    )
    completed_at = get_meta(connection, "initial_audit_completed_at")
    history_complete = (
        history_missing == 0
        and published_alex_history_missing == 0
        and blocked_missing_code == 0
    )
    invariant_ok = (
        direct_total == unresolved + resolved
        and blocked_missing_code == 0
    )
    return {
        "started_at": get_meta(connection, "initial_audit_started_at"),
        "cycle_started_at": get_meta(
            connection,
            "initial_audit_cycle_started_at",
        ),
        "cycle_expiry_as_of": get_meta(
            connection,
            "initial_audit_cycle_expiry_as_of",
        ),
        "cycle_expiry_hours": get_meta(
            connection,
            "initial_audit_cycle_expiry_hours",
        ),
        "completed_at": completed_at,
        "complete": (
            completed_at is not None
            and unresolved == 0
            and history_complete
            and invariant_ok
        ),
        "direct_events": direct_total,
        "pending_events": unresolved,
        "queued_events": queued,
        "resolved_events": resolved,
        "history_events": resolved - history_missing,
        "history_missing_events": history_missing,
        "published_resolutions": published,
        "blocked_resolutions": blocked,
        "blocked_resolutions_missing_code": blocked_missing_code,
        "blocked_contract_complete": blocked_missing_code == 0,
        "published_alex_history_missing": published_alex_history_missing,
        "published_alex_history_complete": (
            published_alex_history_missing == 0
        ),
        "history_complete": history_complete,
        "invariant_ok": invariant_ok,
    }


def initial_audit_next(
    connection: sqlite3.Connection,
    *,
    conversation_limit: int = 1,
) -> dict[str, Any]:
    if conversation_limit <= 0:
        raise ValueError("conversation_limit must be positive")
    configured_user_id = get_meta(connection, "configured_user_id")
    if configured_user_id is None:
        raise ValueError("Initial audit has not started")
    groups = connection.execute(
        """
        SELECT COALESCE(e.conversation_id, e.event_id) AS conversation_key,
               MAX(e.created_at) AS latest_created_at,
               COUNT(*) AS queued_count
        FROM events e
        WHERE e.delivery_state = 'queued'
          AND e.is_reply = 1
          AND e.in_reply_to_user_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM event_resolutions r
              WHERE r.event_id = e.event_id
                AND r.disposition IN ('published', 'blocked')
          )
        GROUP BY COALESCE(e.conversation_id, e.event_id)
        ORDER BY latest_created_at DESC,
                 CAST(conversation_key AS INTEGER) DESC
        LIMIT ?
        """,
        (configured_user_id, conversation_limit),
    ).fetchall()
    result_groups: list[dict[str, Any]] = []
    for group in groups:
        conversation_key = str(group["conversation_key"])
        rows = connection.execute(
            """
            SELECT e.*
            FROM events e
            WHERE e.delivery_state = 'queued'
              AND e.is_reply = 1
              AND e.in_reply_to_user_id = ?
              AND COALESCE(e.conversation_id, e.event_id) = ?
              AND NOT EXISTS (
                  SELECT 1 FROM event_resolutions r
                  WHERE r.event_id = e.event_id
                    AND r.disposition IN ('published', 'blocked')
              )
            ORDER BY e.created_at, CAST(e.event_id AS INTEGER)
            """,
            (configured_user_id, conversation_key),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            event_id = str(row["event_id"])
            username = row["username"]
            references = payload.get("referenced_tweets") or []
            replied_to_status_id = next(
                (
                    str(reference["id"])
                    for reference in references
                    if reference.get("type") == "replied_to"
                    and reference.get("id")
                ),
                None,
            )
            exact_text = str(payload.get("text") or "")
            text_urls = re.findall(r"https?://[^\s]+", exact_text)
            attachments = payload.get("attachments") or {}
            events.append(
                {
                    "event_id": event_id,
                    "event_url": (
                        f"https://x.com/{username}/status/{event_id}"
                        if username
                        else f"https://x.com/i/web/status/{event_id}"
                    ),
                    "username": username,
                    "created_at": row["created_at"],
                    "conversation_id": row["conversation_id"],
                    "replied_to_status_id": replied_to_status_id,
                    "exact_text": exact_text,
                    "text_urls": text_urls,
                    "media_keys": attachments.get("media_keys") or [],
                    "included_media": payload.get("included_media") or [],
                }
            )
        result_groups.append(
            {
                "conversation_id": conversation_key,
                "latest_created_at": group["latest_created_at"],
                "queued_count": int(group["queued_count"]),
                "events": events,
            }
        )
    status = initial_audit_status(connection)
    return {
        "pending_events": status["pending_events"],
        "resolved_events": status["resolved_events"],
        "conversation_limit": conversation_limit,
        "groups": result_groups,
    }


def _expiry_history_snapshot(
    connection: sqlite3.Connection,
    event: sqlite3.Row,
) -> dict[str, Any]:
    event_id = _validate_status_id(str(event["event_id"]), "event_id")
    conversation_id = _validate_status_id(
        str(event["conversation_id"] or ""),
        "conversation_id",
    )
    try:
        payload = json.loads(str(event["payload_json"]))
    except json.JSONDecodeError as error:
        raise ValueError("payload_json must be valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("payload_json must contain an object")
    if str(payload.get("id") or "") != event_id:
        raise ValueError("payload_json id must match event_id")
    exact_text = payload.get("text")
    if not isinstance(exact_text, str):
        raise ValueError("payload_json text must be a string")
    references = payload.get("referenced_tweets")
    if not isinstance(references, list):
        raise ValueError("payload_json referenced_tweets must be an array")
    parent_ids = [
        str(reference.get("id") or "")
        for reference in references
        if isinstance(reference, dict)
        and reference.get("type") == "replied_to"
    ]
    if len(parent_ids) != 1:
        raise ValueError("payload_json must contain exactly one replied_to ID")
    parent_status_id = _validate_status_id(
        parent_ids[0],
        "parent_status_id",
    )
    attachments = payload.get("attachments") or {}
    if not isinstance(attachments, dict):
        raise ValueError("payload_json attachments must be an object")
    media_keys = attachments.get("media_keys") or []
    if not isinstance(media_keys, list):
        raise ValueError("payload_json media_keys must be an array")
    included_media = payload.get("included_media", [])
    if not isinstance(included_media, list):
        raise ValueError("payload_json included_media must be an array")
    requested_media_keys = [str(media_key).strip() for media_key in media_keys]
    if any(not media_key for media_key in requested_media_keys):
        raise ValueError("payload_json media_keys must not contain empty IDs")
    expanded_media_keys: set[str] = set()
    for media_item in included_media:
        if not isinstance(media_item, dict):
            raise ValueError("payload_json included_media items must be objects")
        media_key = str(media_item.get("media_key") or "").strip()
        if not media_key:
            raise ValueError(
                "payload_json included_media items require media_key"
            )
        expanded_media_keys.add(media_key)
    missing_media_keys = sorted(
        set(requested_media_keys) - expanded_media_keys
    )
    if missing_media_keys:
        raise ValueError(
            "media attachment metadata is incomplete for exact history: "
            + ", ".join(missing_media_keys)
        )
    username = str(event["username"] or "").strip()
    event_url = (
        f"https://x.com/{username}/status/{event_id}"
        if username
        else f"https://x.com/i/web/status/{event_id}"
    )
    existing_chain = connection.execute(
        "SELECT provenance FROM conversation_chains WHERE chain_id = ?",
        (conversation_id,),
    ).fetchone()
    chain_provenance = (
        str(existing_chain["provenance"])
        if existing_chain is not None
        else "short"
    )
    return {
        "chain_id": conversation_id,
        "root_status_id": conversation_id,
        "provenance": chain_provenance,
        "turns": [
            {
                "status_id": event_id,
                "parent_status_id": parent_status_id,
                "actor": "user",
                "author": username or None,
                "url": event_url,
                "exact_text": exact_text,
                "media": included_media,
                "posted_at": str(event["created_at"]),
                "provenance": INITIAL_AUDIT_EXPIRY_PROVENANCE,
            }
        ],
    }


def expire_initial_audit_events(
    config: Config,
    connection: sqlite3.Connection,
    *,
    response_window_hours: float,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if connection.in_transaction:
        raise ValueError("Expiry requires a top-level database transaction")
    window_hours = float(response_window_hours)
    if not math.isfinite(window_hours) or window_hours <= 0:
        raise ValueError("Response window hours must be a positive number")
    hours_text = (
        str(int(window_hours))
        if window_hours.is_integer()
        else format(window_hours, ".6g")
    )
    expiry_reason = (
        f"Outside Alex's requested {hours_text}-hour start window; "
        "no Browser review or publication attempted."
    )
    current = now or utc_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Expiry clock must include a timezone")
    current_utc = current.astimezone(timezone.utc)
    cutoff = current_utc - timedelta(
        hours=window_hours
    )
    cutoff_text = isoformat(cutoff)
    resolved_at = isoformat(current_utc)
    counts = {
        "examined": 0,
        "within_window": 0,
        "future_timestamp": 0,
        "invalid_timestamp": 0,
        "invalid_stored_event": 0,
        "expired": 0,
    }
    blocked_candidates: list[dict[str, str]] = []
    expired_sample: list[str] = []
    transaction_started = False
    try:
        connection.execute("BEGIN IMMEDIATE" if not dry_run else "BEGIN")
        transaction_started = True
        configured_user_id = get_meta(connection, "configured_user_id")
        if configured_user_id is None:
            raise ValueError("Initial audit has not started")
        if configured_user_id != config.user_id:
            raise ValueError(
                "Initial audit user does not match configured user"
            )
        if get_meta(connection, "initial_audit_started_at") is None:
            raise ValueError("Initial audit has not started")
        if get_meta(connection, "initial_audit_cycle_started_at") is None:
            raise ValueError(
                "Response cycle has not started; "
                "run initial-audit-start first"
            )
        applied_as_of = get_meta(
            connection,
            "initial_audit_cycle_expiry_as_of",
        )
        applied_hours = get_meta(
            connection,
            "initial_audit_cycle_expiry_hours",
        )
        if (
            applied_as_of is not None
            and (
                applied_as_of != resolved_at
                or applied_hours != hours_text
            )
        ):
            raise ValueError(
                "This response cycle already has a different fixed cutoff; "
                "run initial-audit-start only when Alex starts a new cycle"
            )
        cursor = connection.execute(
            """
            SELECT e.*
            FROM events e
            WHERE e.is_reply = 1
              AND e.in_reply_to_user_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM event_resolutions r
                  WHERE r.event_id = e.event_id
              )
            ORDER BY e.created_at, CAST(e.event_id AS INTEGER)
            """,
            (config.user_id,),
        )
        while True:
            rows = cursor.fetchmany(100)
            if not rows:
                break
            for event in rows:
                counts["examined"] += 1
                event_id = str(event["event_id"])
                try:
                    created_at = parse_time(event["created_at"])
                except ValueError:
                    created_at = None
                if (
                    created_at is None
                    or created_at.tzinfo is None
                    or created_at.utcoffset() is None
                ):
                    counts["invalid_timestamp"] += 1
                    if len(blocked_candidates) < 20:
                        blocked_candidates.append(
                            {
                                "event_id": event_id,
                                "reason": "invalid_or_naive_created_at",
                            }
                        )
                    continue
                created_at_utc = created_at.astimezone(timezone.utc)
                if created_at_utc > current_utc:
                    counts["future_timestamp"] += 1
                    continue
                if created_at_utc >= cutoff:
                    counts["within_window"] += 1
                    continue
                savepoint = "expire_event"
                connection.execute(f"SAVEPOINT {savepoint}")
                try:
                    snapshot = _expiry_history_snapshot(connection, event)
                    import_history_snapshot(
                        connection,
                        snapshot,
                        within_transaction=True,
                    )
                    evidence = json.dumps(
                        [
                            "policy:requested_start_window_no_browser_review",
                            f"response_window_hours:{hours_text}",
                            f"created_at:{isoformat(created_at_utc)}",
                            f"cutoff:{cutoff_text}",
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    connection.execute(
                        """
                        INSERT INTO event_resolutions(
                            event_id, disposition, reason, reply_url, stance,
                            stance_detail, confidence, media_meaning,
                            evidence_json, resolved_at
                        ) VALUES(?, 'skip', ?, NULL, 'ambiguous', ?, 'high',
                                 NULL, ?, ?)
                        """,
                        (
                            event_id,
                            expiry_reason,
                            "outside_requested_start_window_not_classified",
                            evidence,
                            resolved_at,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE events
                        SET delivery_state = 'acknowledged'
                        WHERE event_id = ?
                        """,
                        (event_id,),
                    )
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                except (KeyError, ValueError, json.JSONDecodeError) as error:
                    connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                    counts["invalid_stored_event"] += 1
                    if len(blocked_candidates) < 20:
                        blocked_candidates.append(
                            {
                                "event_id": event_id,
                                "reason": str(error),
                            }
                        )
                    continue
                counts["expired"] += 1
                if len(expired_sample) < 20:
                    expired_sample.append(event_id)
        if dry_run:
            connection.rollback()
        else:
            set_meta(
                connection,
                "initial_audit_response_window_hours",
                hours_text,
            )
            set_meta(
                connection,
                "initial_audit_last_expire_cutoff",
                cutoff_text,
            )
            set_meta(
                connection,
                "initial_audit_last_expire_at",
                resolved_at,
            )
            set_meta(
                connection,
                "initial_audit_cycle_expiry_as_of",
                resolved_at,
            )
            set_meta(
                connection,
                "initial_audit_cycle_expiry_hours",
                hours_text,
            )
            connection.commit()
        transaction_started = False
    except Exception:
        if transaction_started:
            connection.rollback()
        raise
    status = initial_audit_status(connection)
    health_status = evaluate_health(config)["status"]
    if not dry_run:
        refresh_wake_file(config, connection)
        health_status = write_health(
            config,
            connection,
            last_new_count=0,
        )["status"]
    return {
        "status": "dry_run" if dry_run else "applied",
        "response_window_hours": window_hours,
        "cutoff": cutoff_text,
        **counts,
        "blocked_candidates": blocked_candidates,
        "expired_sample": expired_sample,
        "pending_events": status["pending_events"],
        "history_complete": status["history_complete"],
        "invariant_ok": status["invariant_ok"],
        "health": health_status,
    }


def start_initial_audit(
    config: Config,
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    cycle_started_at = isoformat()
    started_at = (
        get_meta(connection, "initial_audit_started_at")
        or cycle_started_at
    )
    with connection:
        set_meta(connection, "configured_user_id", config.user_id)
        set_meta(connection, "initial_audit_started_at", started_at)
        set_meta(
            connection,
            "initial_audit_cycle_started_at",
            cycle_started_at,
        )
        delete_meta(connection, "initial_audit_cycle_expiry_as_of")
        delete_meta(connection, "initial_audit_cycle_expiry_hours")
        delete_meta(connection, "initial_audit_completed_at")
        cursor = connection.execute(
            """
            UPDATE events
            SET delivery_state = 'queued'
            WHERE is_reply = 1
              AND in_reply_to_user_id = ?
              AND COALESCE(author_id, '') != ?
              AND delivery_state != 'queued'
              AND NOT EXISTS (
                  SELECT 1 FROM event_resolutions r
                  WHERE r.event_id = events.event_id
              )
            """,
            (config.user_id, config.user_id),
        )
    wake = refresh_wake_file(config, connection)
    health = write_health(config, connection, last_new_count=0)
    return {
        "requeued": int(cursor.rowcount),
        **initial_audit_status(connection),
        "queue": wake["pending_count"],
        "health": health["status"],
    }


def requeue_unanswered_skips(
    config: Config,
    connection: sqlite3.Connection,
    *,
    response_window_hours: float,
    now: datetime,
    dry_run: bool,
) -> dict[str, Any]:
    if not config.mandatory_response_mode:
        raise ValueError(
            "mandatory_response_mode must be enabled before policy requeue"
        )
    if response_window_hours <= 0 or not math.isfinite(response_window_hours):
        raise ValueError("Response window hours must be a positive number")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Policy requeue --as-of must be timezone-aware")
    current = now.astimezone(timezone.utc)
    cutoff = current - timedelta(hours=response_window_hours)
    candidates: list[sqlite3.Row] = []
    rows = connection.execute(
        """
        SELECT e.*, r.*
        FROM events e
        LEFT JOIN event_resolutions r ON r.event_id = e.event_id
        WHERE e.delivery_state != 'queued'
          AND (
              r.disposition = 'skip'
              OR (
                  r.event_id IS NULL
                  AND e.delivery_state = 'ignored'
                  AND e.is_reply = 1
              )
          )
        ORDER BY e.created_at, CAST(e.event_id AS INTEGER)
        """
    ).fetchall()
    for row in rows:
        if row["author_id"] == config.user_id:
            continue
        created_at = parse_time(row["created_at"])
        if (
            created_at is None
            or created_at.tzinfo is None
            or created_at.utcoffset() is None
        ):
            continue
        created_at_utc = created_at.astimezone(timezone.utc)
        if created_at_utc < cutoff or created_at_utc > current:
            continue
        is_ignored_mention_reply = (
            row["disposition"] is None
            and row["delivery_state"] == "ignored"
            and bool(row["is_reply"])
        )
        if not is_ignored_mention_reply:
            if not is_eligible_reply(
                connection,
                config,
                author_id=row["author_id"],
                is_reply=bool(row["is_reply"]),
                in_reply_to_user_id=row["in_reply_to_user_id"],
                conversation_id=row["conversation_id"],
            ):
                continue
        if _matching_alex_reply_turns(connection, str(row["event_id"])):
            continue
        candidates.append(row)

    requeued_at = isoformat(current)
    if not dry_run and candidates:
        with connection:
            delete_meta(connection, "initial_audit_completed_at")
            for row in candidates:
                event_id = str(row["event_id"])
                previous_resolution = (
                    _resolution_row_payload(row)
                    if row["disposition"] is not None
                    else {
                        "delivery_state": "ignored",
                        "disposition": None,
                    }
                )
                connection.execute(
                    """
                    INSERT INTO response_policy_requeues(
                        event_id, previous_resolution_json, reason, requeued_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        json.dumps(
                            previous_resolution,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        (
                            "mandatory_response_ignored_mention_reconciliation"
                            if row["disposition"] is None
                            else "mandatory_response_policy_migration"
                        ),
                        requeued_at,
                    ),
                )
                connection.execute(
                    """
                    UPDATE events
                    SET delivery_state = 'queued'
                    WHERE event_id = ?
                    """,
                    (event_id,),
                )
    wake = (
        refresh_wake_file(config, connection)
        if not dry_run
        else {"pending_count": len(queued_events(connection))}
    )
    if not dry_run:
        write_health(config, connection, last_new_count=0)
    return {
        "dry_run": dry_run,
        "as_of": isoformat(current),
        "cutoff": isoformat(cutoff),
        "hours": response_window_hours,
        "candidate_count": len(candidates),
        "candidate_event_ids": [
            str(row["event_id"]) for row in candidates
        ],
        "pending_count": wake["pending_count"],
    }


def complete_initial_audit(
    config: Config,
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    unresolved = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM events e
            WHERE e.is_reply = 1
              AND e.in_reply_to_user_id = ?
              AND COALESCE(e.author_id, '') != ?
              AND (
                  NOT EXISTS (
                      SELECT 1 FROM event_resolutions r
                      WHERE r.event_id = e.event_id
                  )
                  OR (
                      e.delivery_state = 'queued'
                      AND EXISTS (
                          SELECT 1 FROM event_resolutions r
                          WHERE r.event_id = e.event_id
                            AND r.disposition = 'skip'
                      )
                  )
              )
            """,
            (config.user_id, config.user_id),
        ).fetchone()["count"]
    )
    if unresolved:
        raise ValueError(
            "Initial audit cannot complete with "
            f"{unresolved} unresolved direct events"
        )
    history_missing = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM event_resolutions r
            JOIN events e ON e.event_id = r.event_id
            LEFT JOIN conversation_turns t ON t.status_id = r.event_id
            WHERE e.is_reply = 1
              AND e.in_reply_to_user_id = ?
              AND COALESCE(e.author_id, '') != ?
              AND NOT (
                  e.delivery_state = 'queued'
                  AND r.disposition = 'skip'
              )
              AND t.status_id IS NULL
            """,
            (config.user_id, config.user_id),
        ).fetchone()["count"]
    )
    if history_missing:
        raise ValueError(
            "Initial audit cannot complete with "
            f"{history_missing} resolved events missing exact history turns"
        )
    published_alex_history_missing = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM event_resolutions r
            JOIN events e ON e.event_id = r.event_id
            LEFT JOIN conversation_turns alex
              ON alex.status_id = substr(
                  r.reply_url,
                  length('https://x.com/axrbarsic/status/') + 1
              )
             AND alex.actor = 'alex'
             AND alex.parent_status_id = r.event_id
             AND alex.url = r.reply_url
            WHERE e.is_reply = 1
              AND e.in_reply_to_user_id = ?
              AND COALESCE(e.author_id, '') != ?
              AND r.disposition = 'published'
              AND (
                  r.reply_url NOT LIKE
                      'https://x.com/axrbarsic/status/%'
                  OR alex.status_id IS NULL
              )
            """,
            (config.user_id, config.user_id),
        ).fetchone()["count"]
    )
    if published_alex_history_missing:
        raise ValueError(
            "Initial audit cannot complete with "
            f"{published_alex_history_missing} published replies missing "
            "matching exact Alex history turns"
        )
    _, blocked_missing_code = _blocked_resolution_contract_status(
        connection,
        config.user_id,
    )
    if blocked_missing_code:
        raise ValueError(
            "Initial audit cannot complete with "
            f"{blocked_missing_code} blocked resolutions missing a valid "
            "terminal blocker_code"
        )
    completed_at = isoformat()
    with connection:
        set_meta(connection, "configured_user_id", config.user_id)
        if get_meta(connection, "initial_audit_started_at") is None:
            set_meta(connection, "initial_audit_started_at", completed_at)
        set_meta(connection, "initial_audit_completed_at", completed_at)
    refresh_wake_file(config, connection)
    write_health(config, connection, last_new_count=0)
    return initial_audit_status(connection)


def _require_matching_published_alex_turn(
    connection: sqlite3.Connection,
    *,
    event: sqlite3.Row,
    event_id: str,
    reply_url: str | None,
) -> sqlite3.Row:
    match = (
        re.fullmatch(
            r"https://x\.com/axrbarsic/status/([0-9]{1,19})",
            reply_url,
        )
        if reply_url is not None
        else None
    )
    if match is None:
        raise ValueError("Published resolution requires a canonical reply_url")
    turn = connection.execute(
        "SELECT * FROM conversation_turns WHERE status_id = ?",
        (match.group(1),),
    ).fetchone()
    conversation_id = str(event["conversation_id"] or event_id)
    if (
        turn is None
        or turn["chain_id"] != conversation_id
        or turn["parent_status_id"] != event_id
        or turn["actor"] != "alex"
        or turn["url"] != reply_url
    ):
        raise ValueError(
            f"Published resolution {event_id} requires a matching exact "
            "Alex history turn"
        )
    return turn


def _matching_alex_reply_turns(
    connection: sqlite3.Connection,
    event_id: str,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT *
        FROM conversation_turns
        WHERE parent_status_id = ?
          AND actor = 'alex'
          AND url LIKE 'https://x.com/axrbarsic/status/%'
        ORDER BY posted_at, CAST(status_id AS INTEGER)
        """,
        (event_id,),
    ).fetchall()


def _enforce_mandatory_response_resolution(
    config: Config,
    connection: sqlite3.Connection,
    *,
    event_id: str,
    disposition: str,
    blocker_code: str | None,
) -> None:
    if blocker_code is not None and blocker_code not in TERMINAL_BLOCKER_CODES:
        allowed = ", ".join(sorted(TERMINAL_BLOCKER_CODES))
        raise ValueError("Unknown blocker_code. Allowed values: " + allowed)
    if disposition != "blocked" and blocker_code is not None:
        raise ValueError("blocker_code is valid only for blocked resolutions")
    if not config.mandatory_response_mode:
        return
    if disposition == "skip" and not _matching_alex_reply_turns(
        connection,
        event_id,
    ):
        raise ValueError(
            "Mandatory response mode forbids content-based skip. Publish a "
            "reply or import the exact existing Alex child reply first."
        )
    if disposition == "blocked" and blocker_code not in TERMINAL_BLOCKER_CODES:
        allowed = ", ".join(sorted(TERMINAL_BLOCKER_CODES))
        raise ValueError(
            "Mandatory response mode requires a terminal blocker_code: "
            + allowed
        )


def _resolution_row_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "disposition": row["disposition"],
        "reason": row["reason"],
        "reply_url": row["reply_url"],
        "blocker_code": row["blocker_code"],
        "stance": row["stance"],
        "stance_detail": row["stance_detail"],
        "confidence": row["confidence"],
        "media_meaning": row["media_meaning"],
        "evidence": json.loads(row["evidence_json"] or "[]"),
        "resolved_at": row["resolved_at"],
    }


def resolve_event(
    config: Config,
    connection: sqlite3.Connection,
    event_id: str,
    *,
    disposition: str,
    reason: str,
    reply_url: str | None,
    blocker_code: str | None = None,
    stance: str | None = None,
    stance_detail: str | None = None,
    confidence: str | None = None,
    media_meaning: str | None = None,
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    event = connection.execute(
        "SELECT * FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if event is None:
        raise KeyError(f"Unknown event {event_id}")
    if disposition not in {"published", "skip", "blocked"}:
        raise ValueError("Disposition must be published, skip, or blocked")
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError("Resolution reason must not be empty")
    clean_reply_url = reply_url.strip() if reply_url else None
    clean_blocker_code = blocker_code.strip() if blocker_code else None
    clean_stance = stance.strip() if stance else None
    if clean_stance not in {None, "supportive", "opposing", "neutral", "ambiguous"}:
        raise ValueError("Stance must be supportive, opposing, neutral, or ambiguous")
    clean_stance_detail = stance_detail.strip() if stance_detail else None
    clean_confidence = confidence.strip() if confidence else None
    if clean_confidence not in {None, "high", "medium", "low"}:
        raise ValueError("Confidence must be high, medium, or low")
    clean_media_meaning = media_meaning.strip() if media_meaning else None
    clean_evidence = sorted(
        {
            str(item).strip()
            for item in (evidence or [])
            if str(item).strip()
        }
    )
    evidence_json = json.dumps(clean_evidence, ensure_ascii=False)
    if disposition == "published" and not clean_reply_url:
        raise ValueError("Published resolution requires reply_url")
    if disposition in {"skip", "blocked"} and clean_reply_url:
        raise ValueError(
            "Skip and blocked resolutions must not include reply_url"
        )
    _enforce_mandatory_response_resolution(
        config,
        connection,
        event_id=event_id,
        disposition=disposition,
        blocker_code=clean_blocker_code,
    )
    history_turn = connection.execute(
        "SELECT 1 FROM conversation_turns WHERE status_id = ?",
        (event_id,),
    ).fetchone()
    if history_turn is None:
        raise ValueError(
            "Import the exact inspected event turn into conversation history "
            f"before resolving {event_id}"
        )
    if disposition == "published":
        _require_matching_published_alex_turn(
            connection,
            event=event,
            event_id=event_id,
            reply_url=clean_reply_url,
        )
    existing = connection.execute(
        "SELECT * FROM event_resolutions WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    expected = {
        "disposition": disposition,
        "reason": clean_reason,
        "reply_url": clean_reply_url,
        "blocker_code": clean_blocker_code,
        "stance": clean_stance,
        "stance_detail": clean_stance_detail,
        "confidence": clean_confidence,
        "media_meaning": clean_media_meaning,
        "evidence_json": evidence_json,
    }
    if existing is not None:
        _assert_existing_values(
            existing,
            expected,
            resource=f"event resolution {event_id}",
        )
    with connection:
        if existing is None:
            connection.execute(
                """
                INSERT INTO event_resolutions(
                    event_id, disposition, reason, reply_url, blocker_code, stance,
                    stance_detail, confidence, media_meaning, evidence_json,
                    resolved_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    disposition,
                    clean_reason,
                    clean_reply_url,
                    clean_blocker_code,
                    clean_stance,
                    clean_stance_detail,
                    clean_confidence,
                    clean_media_meaning,
                    evidence_json,
                    isoformat(),
                ),
            )
        elif existing["stance_detail"] is None and clean_stance_detail is not None:
            connection.execute(
                """
                UPDATE event_resolutions
                SET stance_detail = ?
                WHERE event_id = ?
                """,
                (clean_stance_detail, event_id),
            )
        connection.execute(
            "UPDATE events SET delivery_state = 'acknowledged' "
            "WHERE event_id = ?",
            (event_id,),
        )
    wake = refresh_wake_file(config, connection)
    health = write_health(config, connection, last_new_count=0)
    return {
        "event_id": event_id,
        "disposition": disposition,
        "reason": clean_reason,
        "reply_url": clean_reply_url,
        "blocker_code": clean_blocker_code,
        "stance": clean_stance,
        "stance_detail": clean_stance_detail,
        "confidence": clean_confidence,
        "media_meaning": clean_media_meaning,
        "evidence": clean_evidence,
        "pending_count": wake["pending_count"],
        "health": health["status"],
    }


def revise_event_resolution(
    config: Config,
    connection: sqlite3.Connection,
    event_id: str,
    *,
    disposition: str,
    reason: str,
    reply_url: str | None,
    revision_reason: str,
    blocker_code: str | None = None,
    stance: str | None = None,
    stance_detail: str | None = None,
    confidence: str | None = None,
    media_meaning: str | None = None,
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    event = connection.execute(
        "SELECT * FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if event is None:
        raise KeyError(f"Unknown event {event_id}")
    existing = connection.execute(
        "SELECT * FROM event_resolutions WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if existing is None:
        raise ValueError(
            f"Event {event_id} has no existing resolution to revise"
        )
    clean_revision_reason = revision_reason.strip()
    if not clean_revision_reason:
        raise ValueError("Resolution revision reason must not be empty")
    if disposition not in {"published", "skip", "blocked"}:
        raise ValueError("Disposition must be published, skip, or blocked")
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError("Resolution reason must not be empty")
    clean_reply_url = reply_url.strip() if reply_url else None
    clean_blocker_code = blocker_code.strip() if blocker_code else None
    clean_stance = stance.strip() if stance else None
    if clean_stance not in {
        None,
        "supportive",
        "opposing",
        "neutral",
        "ambiguous",
    }:
        raise ValueError(
            "Stance must be supportive, opposing, neutral, or ambiguous"
        )
    clean_stance_detail = stance_detail.strip() if stance_detail else None
    clean_confidence = confidence.strip() if confidence else None
    if clean_confidence not in {None, "high", "medium", "low"}:
        raise ValueError("Confidence must be high, medium, or low")
    clean_media_meaning = media_meaning.strip() if media_meaning else None
    clean_evidence = sorted(
        {
            str(item).strip()
            for item in (evidence or [])
            if str(item).strip()
        }
    )
    evidence_json = json.dumps(clean_evidence, ensure_ascii=False)
    if disposition == "published" and not clean_reply_url:
        raise ValueError("Published resolution requires reply_url")
    if disposition in {"skip", "blocked"} and clean_reply_url:
        raise ValueError(
            "Skip and blocked resolutions must not include reply_url"
        )
    _enforce_mandatory_response_resolution(
        config,
        connection,
        event_id=event_id,
        disposition=disposition,
        blocker_code=clean_blocker_code,
    )
    expected = {
        "disposition": disposition,
        "reason": clean_reason,
        "reply_url": clean_reply_url,
        "blocker_code": clean_blocker_code,
        "stance": clean_stance,
        "stance_detail": clean_stance_detail,
        "confidence": clean_confidence,
        "media_meaning": clean_media_meaning,
        "evidence_json": evidence_json,
    }
    if all(existing[name] == value for name, value in expected.items()):
        wake = refresh_wake_file(config, connection)
        health = write_health(config, connection, last_new_count=0)
        return {
            "event_id": event_id,
            "revised": False,
            "disposition": disposition,
            "reply_url": clean_reply_url,
            "pending_count": wake["pending_count"],
            "health": health["status"],
        }
    allowed_transitions = {
        ("skip", "skip"),
        ("skip", "published"),
        ("skip", "blocked"),
        ("blocked", "blocked"),
        ("blocked", "published"),
        ("blocked", "skip"),
    }
    if (
        existing["disposition"] == "skip"
        and disposition == "skip"
        and not config.mandatory_response_mode
    ):
        raise ValueError(
            "Skip to skip revision requires mandatory response mode"
        )
    if (existing["disposition"], disposition) not in allowed_transitions:
        raise ValueError(
            "Unsupported resolution revision transition "
            f"{existing['disposition']} to {disposition}"
        )
    history_turn = connection.execute(
        "SELECT 1 FROM conversation_turns WHERE status_id = ?",
        (event_id,),
    ).fetchone()
    if history_turn is None:
        raise ValueError(
            "Import the exact inspected event turn before revising "
            f"{event_id}"
        )
    if disposition == "published":
        _require_matching_published_alex_turn(
            connection,
            event=event,
            event_id=event_id,
            reply_url=clean_reply_url,
        )
    revised_at = isoformat()
    replacement = {
        **{name: value for name, value in expected.items() if name != "evidence_json"},
        "evidence": clean_evidence,
        "resolved_at": revised_at,
    }
    with connection:
        connection.execute(
            """
            INSERT INTO event_resolution_revisions(
                event_id, previous_json, replacement_json,
                revision_reason, revised_at
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (
                event_id,
                json.dumps(
                    _resolution_row_payload(existing),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                json.dumps(
                    replacement,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                clean_revision_reason,
                revised_at,
            ),
        )
        connection.execute(
            """
            UPDATE event_resolutions
            SET disposition = ?, reason = ?, reply_url = ?, blocker_code = ?,
                stance = ?,
                stance_detail = ?, confidence = ?, media_meaning = ?,
                evidence_json = ?, resolved_at = ?
            WHERE event_id = ?
            """,
            (
                disposition,
                clean_reason,
                clean_reply_url,
                clean_blocker_code,
                clean_stance,
                clean_stance_detail,
                clean_confidence,
                clean_media_meaning,
                evidence_json,
                revised_at,
                event_id,
            ),
        )
        connection.execute(
            "UPDATE events SET delivery_state = 'acknowledged' "
            "WHERE event_id = ?",
            (event_id,),
        )
    wake = refresh_wake_file(config, connection)
    health = write_health(config, connection, last_new_count=0)
    return {
        "event_id": event_id,
        "revised": True,
        "disposition": disposition,
        "reply_url": clean_reply_url,
        "blocker_code": clean_blocker_code,
        "revision_reason": clean_revision_reason,
        "pending_count": wake["pending_count"],
        "health": health["status"],
    }


def export_audit_resolutions(
    connection: sqlite3.Connection,
    output: Path,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    rows = connection.execute(
        """
        SELECT
            e.event_id, e.created_at, e.conversation_id, e.username,
            r.disposition, r.reason, r.reply_url, r.blocker_code, r.stance,
            r.stance_detail, r.confidence, r.media_meaning, r.evidence_json,
            r.resolved_at
        FROM event_resolutions r
        JOIN events e ON e.event_id = r.event_id
        ORDER BY CAST(e.event_id AS INTEGER)
        """
    ).fetchall()
    revisions = connection.execute(
        """
        SELECT
            id, event_id, previous_json, replacement_json,
            revision_reason, revised_at
        FROM event_resolution_revisions
        ORDER BY id
        """
    ).fetchall()
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "record_type": "initial_audit_meta",
                    "schema_version": SCHEMA_VERSION,
                    **initial_audit_status(connection),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        for row in rows:
            handle.write(
                json.dumps(
                    {
                        "record_type": "event_resolution",
                        "event_id": row["event_id"],
                        "created_at": row["created_at"],
                        "conversation_id": row["conversation_id"],
                        "username": row["username"],
                        "disposition": row["disposition"],
                        "reason": row["reason"],
                        "reply_url": row["reply_url"],
                        "blocker_code": row["blocker_code"],
                        "stance": row["stance"],
                        "stance_detail": row["stance_detail"],
                        "confidence": row["confidence"],
                        "media_meaning": row["media_meaning"],
                        "evidence": json.loads(row["evidence_json"] or "[]"),
                        "resolved_at": row["resolved_at"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
        for revision in revisions:
            handle.write(
                json.dumps(
                    {
                        "record_type": "event_resolution_revision",
                        "revision_id": revision["id"],
                        "event_id": revision["event_id"],
                        "previous": json.loads(revision["previous_json"]),
                        "replacement": json.loads(
                            revision["replacement_json"]
                        ),
                        "revision_reason": revision["revision_reason"],
                        "revised_at": revision["revised_at"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    temporary.replace(output)
    return {
        "output": str(output),
        "resolutions": len(rows),
        "resolution_revisions": len(revisions),
    }


def _history_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Invalid JSONL at line {line_number}"
                ) from error
            if not isinstance(record, dict):
                raise ValueError(
                    f"History record at line {line_number} must be an object"
                )
            records.append(record)
        return records
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return payload
    raise ValueError("History input must be an object, object array, or JSONL")


def _required_text(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise ValueError("Missing required history field: " + " or ".join(names))


def _required_exact_text(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        if name in payload and payload[name] is not None:
            value = payload[name]
            return str(value)
    raise ValueError("Missing required history field: " + " or ".join(names))


def _optional_text(payload: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _validate_status_id(value: str, field_name: str) -> str:
    if not value.isdigit() or len(value) > 19:
        raise ValueError(f"{field_name} must be a numeric X status ID")
    return value


def _assert_existing_values(
    existing: sqlite3.Row,
    expected: dict[str, Any],
    *,
    resource: str,
) -> None:
    mismatches = [
        name
        for name, value in expected.items()
        if existing[name] is not None and value is not None and existing[name] != value
    ]
    if mismatches:
        raise ValueError(
            f"Append-only history conflict for {resource}: "
            + ", ".join(sorted(mismatches))
        )


def import_history_snapshot(
    connection: sqlite3.Connection,
    record: dict[str, Any],
    *,
    within_transaction: bool = False,
) -> dict[str, Any]:
    chain_id = _validate_status_id(
        _required_text(
            record,
            "chain_id",
            "conversation_id",
            "root_id",
            "conversation_root_id",
        ),
        "chain_id",
    )
    root_status_id = _validate_status_id(
        _required_text(
            record,
            "root_status_id",
            "root_id",
            "conversation_id",
            "conversation_root_id",
        ),
        "root_status_id",
    )
    turns = record.get("turns")
    if not isinstance(turns, list) or not turns:
        raise ValueError("History record must contain a non-empty turns array")
    provenance = _optional_text(record, "provenance", "reply_type")
    if provenance is None:
        turn_provenance = {
            str(turn.get("provenance")).strip()
            for turn in turns
            if isinstance(turn, dict) and turn.get("provenance")
        }
        provenance = "pro" if "pro" in turn_provenance else "short"
    if provenance not in {"short", "pro", "mixed"}:
        raise ValueError("History provenance must be short, pro, or mixed")
    chatgpt_url = _optional_text(
        record,
        "chatgpt_conversation_url",
        "conversation_url",
    )
    ledger_reference = _optional_text(record, "ledger_reference", "ledger_ref")
    observed_at = isoformat()
    inserted_turns = 0
    inserted_sources = 0

    transaction = contextlib.nullcontext() if within_transaction else connection
    with transaction:
        existing_chain = connection.execute(
            "SELECT * FROM conversation_chains WHERE chain_id = ?",
            (chain_id,),
        ).fetchone()
        if existing_chain is None:
            connection.execute(
                """
                INSERT INTO conversation_chains(
                    chain_id, root_status_id, provenance,
                    chatgpt_conversation_url, ledger_reference,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chain_id,
                    root_status_id,
                    provenance,
                    chatgpt_url,
                    ledger_reference,
                    observed_at,
                    observed_at,
                ),
            )
        else:
            _assert_existing_values(
                existing_chain,
                {
                    "root_status_id": root_status_id,
                    "provenance": provenance,
                    "chatgpt_conversation_url": chatgpt_url,
                    "ledger_reference": ledger_reference,
                },
                resource=f"chain {chain_id}",
            )
            connection.execute(
                """
                UPDATE conversation_chains
                SET chatgpt_conversation_url =
                        COALESCE(chatgpt_conversation_url, ?),
                    ledger_reference = COALESCE(ledger_reference, ?),
                    updated_at = ?
                WHERE chain_id = ?
                """,
                (chatgpt_url, ledger_reference, observed_at, chain_id),
            )

        for turn in turns:
            if not isinstance(turn, dict):
                raise ValueError("Each history turn must be an object")
            status_id = _validate_status_id(
                _required_text(turn, "status_id", "event_id"),
                "status_id",
            )
            parent_status_id = _optional_text(
                turn,
                "parent_status_id",
                "in_reply_to_status_id",
            )
            if parent_status_id is not None:
                parent_status_id = _validate_status_id(
                    parent_status_id,
                    "parent_status_id",
                )
            actor = _required_text(turn, "actor")
            actor = {
                "axrbarsic": "alex",
                "root": "other",
            }.get(actor, actor)
            if actor not in {"target", "alex", "user", "other"}:
                raise ValueError(
                    "History actor must be target, alex, user, or other"
                )
            author = _optional_text(turn, "author", "username")
            url = _required_text(turn, "url", "status_url", "event_url")
            exact_text = _required_exact_text(turn, "exact_text", "text")
            media = turn.get("media", [])
            if media is None:
                media = []
            if not isinstance(media, list):
                raise ValueError("History turn media must be an array")
            media_json = json.dumps(
                media,
                ensure_ascii=False,
                sort_keys=True,
            )
            posted_at = _optional_text(
                turn,
                "posted_at",
                "created_at",
                "timestamp",
                "timestamp_visible",
            )
            turn_provenance = _optional_text(turn, "provenance", "reply_type")
            existing_turn = connection.execute(
                "SELECT * FROM conversation_turns WHERE status_id = ?",
                (status_id,),
            ).fetchone()
            expected_turn = {
                "chain_id": chain_id,
                "parent_status_id": parent_status_id,
                "actor": actor,
                "author": author,
                "url": url,
                "exact_text": exact_text,
                "posted_at": posted_at,
                "provenance": turn_provenance,
            }
            if existing_turn is None:
                connection.execute(
                    """
                    INSERT INTO conversation_turns(
                        status_id, chain_id, parent_status_id, actor, author,
                        url, exact_text, media_json, posted_at, observed_at,
                        provenance
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        status_id,
                        chain_id,
                        parent_status_id,
                        actor,
                        author,
                        url,
                        exact_text,
                        media_json,
                        posted_at,
                        observed_at,
                        turn_provenance,
                    ),
                )
                inserted_turns += 1
            else:
                _assert_existing_values(
                    existing_turn,
                    expected_turn,
                    resource=f"turn {status_id}",
                )
                existing_media_json = existing_turn["media_json"]
                if (
                    existing_media_json not in {None, "[]"}
                    and existing_media_json != media_json
                ):
                    raise ValueError(
                        f"Append-only history conflict for turn {status_id}: "
                        "media_json"
                    )
                connection.execute(
                    """
                    UPDATE conversation_turns
                    SET parent_status_id = COALESCE(parent_status_id, ?),
                        author = COALESCE(author, ?),
                        posted_at = COALESCE(posted_at, ?),
                        provenance = COALESCE(provenance, ?),
                        media_json = CASE
                            WHEN media_json = '[]' THEN ?
                            ELSE media_json
                        END
                    WHERE status_id = ?
                    """,
                    (
                        parent_status_id,
                        author,
                        posted_at,
                        turn_provenance,
                        media_json,
                        status_id,
                    ),
                )

            sources = turn.get("source_urls", turn.get("sources", []))
            if sources is None:
                sources = []
            if not isinstance(sources, list):
                raise ValueError("History turn sources must be an array")
            for source_url in sources:
                source = str(source_url).strip()
                if not source:
                    continue
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO conversation_sources(
                        status_id, source_url
                    ) VALUES(?, ?)
                    """,
                    (status_id, source),
                )
                inserted_sources += int(cursor.rowcount)

    return {
        "chain_id": chain_id,
        "inserted_turns": inserted_turns,
        "inserted_sources": inserted_sources,
        "total_turns": len(turns),
    }


def import_history_file(
    connection: sqlite3.Connection,
    path: Path,
) -> dict[str, Any]:
    records = _history_records(path)
    provenance_corrections: dict[str, str] = {}
    chain_provenance_corrections: dict[str, str] = {}
    for record in records:
        snapshot_type = record.get("snapshot_type")
        if snapshot_type == "provenance_correction":
            corrected_status_id = _optional_text(
                record,
                "corrected_status_id",
            )
            corrected_provenance = _optional_text(
                record,
                "corrected_provenance",
            )
            if corrected_status_id and corrected_provenance in {"short", "pro"}:
                provenance_corrections[corrected_status_id] = (
                    corrected_provenance
                )
        elif snapshot_type == "chain_provenance_correction":
            chain_id = _validate_status_id(
                _required_text(
                    record,
                    "chain_id",
                    "conversation_root_id",
                ),
                "chain_id",
            )
            corrected_provenance = _required_text(
                record,
                "corrected_provenance",
            )
            if corrected_provenance not in {"short", "pro", "mixed"}:
                raise ValueError(
                    "Corrected chain provenance must be short, pro, or mixed"
                )
            _required_text(record, "correction_reason")
            previous = chain_provenance_corrections.get(chain_id)
            if previous is not None and previous != corrected_provenance:
                raise ValueError(
                    f"Conflicting chain provenance corrections for {chain_id}"
                )
            chain_provenance_corrections[chain_id] = corrected_provenance

    chain_records: list[dict[str, Any]] = []
    for source_record in records:
        if isinstance(source_record.get("turns"), list):
            record = json.loads(json.dumps(source_record))
        elif source_record.get("record_type") in {
            "initial_audit_event_turn",
            "initial_audit_alex_turn",
        }:
            media = source_record.get("media_json", [])
            if isinstance(media, str):
                try:
                    media = json.loads(media)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        "Flat history turn media_json must be valid JSON"
                    ) from error
            if not isinstance(media, list):
                raise ValueError(
                    "Flat history turn media_json must be an array"
                )
            chain_id = _required_text(
                source_record,
                "chain_id",
                "conversation_root_id",
            )
            existing_chain = connection.execute(
                """
                SELECT provenance
                FROM conversation_chains
                WHERE chain_id = ?
                """,
                (chain_id,),
            ).fetchone()
            record = {
                "chain_id": chain_id,
                "root_status_id": _required_text(
                    source_record,
                    "conversation_root_id",
                    "chain_id",
                ),
                "provenance": _optional_text(
                    source_record,
                    "chain_provenance",
                )
                or (
                    str(existing_chain["provenance"])
                    if existing_chain is not None
                    else None
                )
                or "short",
                "turns": [
                    {
                        "status_id": _required_text(
                            source_record,
                            "status_id",
                        ),
                        "parent_status_id": source_record.get(
                            "parent_status_id"
                        ),
                        "actor": _required_text(source_record, "actor"),
                        "author": source_record.get("author"),
                        "url": _required_text(source_record, "url"),
                        "exact_text": _required_exact_text(
                            source_record,
                            "exact_text",
                        ),
                        "posted_at": source_record.get("posted_at"),
                        "provenance": source_record.get("provenance"),
                        "media": media,
                        "source_urls": source_record.get(
                            "sources",
                            source_record.get("source_urls", []),
                        ),
                    }
                ],
            }
        else:
            continue
        for turn in record["turns"]:
            if not isinstance(turn, dict):
                continue
            status_id = str(turn.get("status_id") or "")
            if status_id in provenance_corrections:
                turn["provenance"] = provenance_corrections[status_id]
        chain_id = str(record.get("chain_id") or "")
        if chain_id in chain_provenance_corrections:
            record["provenance"] = chain_provenance_corrections[chain_id]
        chain_records.append(record)

    with connection:
        results = [
            import_history_snapshot(
                connection,
                record,
                within_transaction=True,
            )
            for record in chain_records
        ]
    return {
        "records": len(results),
        "skipped_metadata_records": len(records) - len(chain_records),
        "inserted_turns": sum(item["inserted_turns"] for item in results),
        "inserted_sources": sum(item["inserted_sources"] for item in results),
        "chains": [item["chain_id"] for item in results],
    }


def sync_browser_handoffs(
    config: Config,
    connection: sqlite3.Connection,
    *,
    history_path: Path,
    ledger_path: Path,
) -> dict[str, Any]:
    history_parent = history_path.expanduser().resolve().parent
    ledger_parent = ledger_path.expanduser().resolve().parent
    canonical_evidence_root = (
        config.source_path.parent / "var/evidence/browser-owner"
    ).resolve()
    history_is_canonical = history_parent.parent == canonical_evidence_root
    ledger_is_canonical = ledger_parent.parent == canonical_evidence_root
    if (history_is_canonical or ledger_is_canonical) and not (
        history_is_canonical
        and ledger_is_canonical
        and history_parent == ledger_parent
    ):
        raise ValueError(
            "Canonical Browser handoff files must share one evidence directory"
        )

    history_result = import_history_file(connection, history_path)
    records = _history_records(ledger_path)
    candidate_groups: dict[str, list[dict[str, Any]]] = {}
    already_resolved: list[str] = []

    def handoff_signature(record: dict[str, Any]) -> str:
        evidence = record.get("evidence", [])
        if isinstance(evidence, list):
            normalized_evidence: Any = sorted(
                str(item).strip()
                for item in evidence
                if str(item).strip()
            )
        elif isinstance(evidence, str):
            normalized_evidence = [evidence.strip()] if evidence.strip() else []
        else:
            normalized_evidence = evidence
        return json.dumps(
            {
                "conversation_id": record.get("conversation_id"),
                "direct_reply_to_axrbarsic": record.get(
                    "direct_reply_to_axrbarsic"
                ),
                "tracked_conversation_reply": record.get(
                    "tracked_conversation_reply"
                ),
                "mention_reply_to_axrbarsic": record.get(
                    "mention_reply_to_axrbarsic"
                ),
                "history_status": record.get("history_status"),
                "watcher_disposition": record.get("watcher_disposition"),
                "disposition": record.get("disposition"),
                "reason": record.get("reason"),
                "reply_url": record.get("reply_url"),
                "existing_alex_reply_url": record.get(
                    "existing_alex_reply_url"
                ),
                "blocker_code": record.get("blocker_code"),
                "stance": record.get("stance"),
                "stance_detail": record.get("stance_detail"),
                "confidence": record.get("confidence"),
                "media_meaning": record.get("media_meaning"),
                "evidence": normalized_evidence,
                "supersedes_existing_resolution": record.get(
                    "supersedes_existing_resolution"
                ),
                "resolution_revision_reason": record.get(
                    "resolution_revision_reason"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    for record in records:
        if record.get("event") != "initial_audit_disposition":
            continue
        watcher_disposition = str(record.get("watcher_disposition") or "")
        if not watcher_disposition.endswith("_pending_root_resolve"):
            continue
        event_id = _validate_status_id(
            _required_text(record, "event_id"),
            "event_id",
        )
        existing = connection.execute(
            "SELECT 1 FROM event_resolutions WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if (
            existing is not None
            and record.get("supersedes_existing_resolution") is not True
        ):
            already_resolved.append(event_id)
            continue
        candidate_groups.setdefault(event_id, []).append(record)

    candidate_records: dict[str, dict[str, Any]] = {}
    for event_id, group in candidate_groups.items():
        signatures = {handoff_signature(record) for record in group}
        latest = group[-1]
        if (
            len(signatures) > 1
            and latest.get("supersedes_invalid_handoff") is not True
        ):
            raise ValueError(
                f"Conflicting Browser handoffs for unresolved event {event_id}"
            )
        candidate_records[event_id] = latest

    prepared: list[dict[str, Any]] = []
    for event_id, record in candidate_records.items():
        watcher_disposition = str(record.get("watcher_disposition") or "")
        is_revision = record.get("supersedes_existing_resolution") is True
        revision_reason = (
            _required_text(record, "resolution_revision_reason")
            if is_revision
            else None
        )
        confirmed_direct = record.get("direct_reply_to_axrbarsic") is True
        confirmed_tracked = (
            record.get("tracked_conversation_reply") is True
        )
        confirmed_mention = (
            record.get("mention_reply_to_axrbarsic") is True
        )
        if sum(
            (
                confirmed_direct,
                confirmed_tracked,
                confirmed_mention,
            )
        ) != 1:
            raise ValueError(
                f"Browser handoff {event_id} must confirm exactly one route"
            )
        if record.get("history_status") != "exact_user_turn_appended":
            raise ValueError(
                f"Browser handoff {event_id} lacks exact history confirmation"
            )
        event = connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if event is None:
            raise KeyError(f"Unknown event {event_id}")
        event_is_self_authored = event["author_id"] == config.user_id
        event_is_direct = (
            not event_is_self_authored
            and int(event["is_reply"]) == 1
            and event["in_reply_to_user_id"] == config.user_id
        )
        event_is_tracked = is_eligible_reply(
            connection,
            config,
            author_id=event["author_id"],
            is_reply=bool(event["is_reply"]),
            in_reply_to_user_id=None,
            conversation_id=event["conversation_id"],
        )
        event_is_mention = (
            not event_is_self_authored
            and event_mentions_configured_account(config, event)
        )
        if (
            (confirmed_direct and not event_is_direct)
            or (confirmed_tracked and not event_is_tracked)
            or (confirmed_mention and not event_is_mention)
        ):
            raise ValueError(
                f"Browser handoff {event_id} route does not match stored event"
            )
        conversation_id = _validate_status_id(
            _required_text(record, "conversation_id"),
            "conversation_id",
        )
        if conversation_id != event["conversation_id"]:
            raise ValueError(
                f"Browser handoff {event_id} has mismatched conversation_id"
            )
        if connection.execute(
            "SELECT 1 FROM conversation_turns WHERE status_id = ?",
            (event_id,),
        ).fetchone() is None:
            raise ValueError(
                f"Browser handoff {event_id} has no imported exact history turn"
            )
        evidence = record.get("evidence", [])
        if isinstance(evidence, str):
            evidence = [evidence] if evidence.strip() else []
        elif not isinstance(evidence, list):
            raise ValueError(
                f"Browser handoff {event_id} evidence must be text or an array"
            )
        disposition = _required_text(record, "disposition")
        if disposition not in {"published", "skip", "blocked"}:
            raise ValueError(
                f"Browser handoff {event_id} has invalid disposition"
            )
        if disposition == "skip" and (
            watcher_disposition != "durable_skip_pending_root_resolve"
        ):
            raise ValueError(
                f"Browser handoff {event_id} has an invalid skip marker"
            )
        if disposition == "blocked" and (
            watcher_disposition
            != "durable_blocked_pending_root_resolve"
        ):
            raise ValueError(
                f"Browser handoff {event_id} has an invalid blocked marker"
            )
        if disposition == "published" and (
            watcher_disposition
            != "verified_publication_pending_root_resolve"
        ):
            raise ValueError(
                f"Browser handoff {event_id} has an invalid publication marker"
            )
        reply_url = _optional_text(record, "reply_url")
        existing_alex_reply_url = _optional_text(
            record,
            "existing_alex_reply_url",
        )
        blocker_code = _optional_text(record, "blocker_code")
        reply_match = (
            re.fullmatch(
                r"https://x\.com/axrbarsic/status/([0-9]{1,19})",
                reply_url,
            )
            if reply_url is not None
            else None
        )
        if disposition == "published":
            if reply_match is None:
                raise ValueError(
                    f"Browser handoff {event_id} lacks a verified reply URL"
                )
            if record.get("alex_history_status") != "exact_alex_turn_appended":
                raise ValueError(
                    f"Browser handoff {event_id} lacks exact Alex history "
                    "confirmation"
                )
            try:
                _require_matching_published_alex_turn(
                    connection,
                    event=event,
                    event_id=event_id,
                    reply_url=reply_url,
                )
            except ValueError as error:
                raise ValueError(
                    f"Browser handoff {event_id} has no matching exact Alex turn"
                ) from error
        elif reply_url is not None:
            raise ValueError(
                f"Browser handoff {event_id} has reply_url without publication"
            )
        if disposition == "skip" and config.mandatory_response_mode:
            if record.get("alex_history_status") != "exact_alex_turn_appended":
                raise ValueError(
                    f"Browser handoff {event_id} lacks exact existing Alex "
                    "history confirmation"
                )
            matching_turns = _matching_alex_reply_turns(connection, event_id)
            matching_urls = {str(turn["url"]) for turn in matching_turns}
            if existing_alex_reply_url not in matching_urls:
                raise ValueError(
                    f"Browser handoff {event_id} lacks a matching existing "
                    "Alex child reply URL"
                )
        elif existing_alex_reply_url is not None:
            raise ValueError(
                f"Browser handoff {event_id} has an unexpected existing "
                "Alex reply URL"
            )
        _enforce_mandatory_response_resolution(
            config,
            connection,
            event_id=event_id,
            disposition=disposition,
            blocker_code=blocker_code,
        )
        stance = _optional_text(record, "stance")
        if stance not in {None, "supportive", "opposing", "neutral", "ambiguous"}:
            raise ValueError(
                f"Browser handoff {event_id} has invalid broad stance"
            )
        confidence = _optional_text(record, "confidence")
        if confidence not in {None, "high", "medium", "low"}:
            raise ValueError(
                f"Browser handoff {event_id} has invalid confidence"
            )
        item = {
            "event_id": event_id,
            "disposition": disposition,
            "reason": _required_text(record, "reason"),
            "reply_url": reply_url,
            "blocker_code": blocker_code,
            "stance": stance,
            "stance_detail": _optional_text(record, "stance_detail"),
            "confidence": confidence,
            "media_meaning": _optional_text(record, "media_meaning"),
            "evidence": sorted(
                {
                    str(value).strip()
                    for value in evidence
                    if str(value).strip()
                }
            ),
            "is_revision": is_revision,
            "revision_reason": revision_reason,
        }
        existing = connection.execute(
            "SELECT * FROM event_resolutions WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if is_revision:
            if existing is None:
                raise ValueError(
                    f"Browser handoff {event_id} has no resolution to revise"
                )
            expected = {
                "disposition": item["disposition"],
                "reason": item["reason"],
                "reply_url": item["reply_url"],
                "blocker_code": item["blocker_code"],
                "stance": item["stance"],
                "stance_detail": item["stance_detail"],
                "confidence": item["confidence"],
                "media_meaning": item["media_meaning"],
                "evidence_json": json.dumps(
                    item["evidence"],
                    ensure_ascii=False,
                ),
            }
            if all(existing[name] == value for name, value in expected.items()):
                already_resolved.append(event_id)
                continue
        prepared.append(item)

    resolved: list[dict[str, Any]] = []
    revised_event_ids: list[str] = []
    for item in prepared:
        if item["is_revision"]:
            result = revise_event_resolution(
                config,
                connection,
                item["event_id"],
                disposition=item["disposition"],
                reason=item["reason"],
                reply_url=item["reply_url"],
                revision_reason=item["revision_reason"],
                blocker_code=item["blocker_code"],
                stance=item["stance"],
                stance_detail=item["stance_detail"],
                confidence=item["confidence"],
                media_meaning=item["media_meaning"],
                evidence=item["evidence"],
            )
            if result["revised"]:
                revised_event_ids.append(item["event_id"])
                resolved.append(result)
            else:
                already_resolved.append(item["event_id"])
            continue
        resolved.append(
            resolve_event(
                config,
                connection,
                item["event_id"],
                disposition=item["disposition"],
                reason=item["reason"],
                reply_url=item["reply_url"],
                blocker_code=item["blocker_code"],
                stance=item["stance"],
                stance_detail=item["stance_detail"],
                confidence=item["confidence"],
                media_meaning=item["media_meaning"],
                evidence=item["evidence"],
            )
        )
    result = {
        "history": history_result,
        "eligible_handoffs": len(prepared),
        "resolved_event_ids": [item["event_id"] for item in resolved],
        "revised_event_ids": revised_event_ids,
        "already_resolved_event_ids": sorted(set(already_resolved)),
        "audit": initial_audit_status(connection),
    }
    if (
        history_parent == ledger_parent
        and history_parent.parent == canonical_evidence_root
    ):
        from evidence_import import finalize_runtime_evidence

        result["evidence_manifest"] = finalize_runtime_evidence(
            destination=history_parent,
            connection=connection,
            output_root=canonical_evidence_root,
        )
    return result


def history_chain_for_status(
    connection: sqlite3.Connection,
    status_id: str,
) -> dict[str, Any]:
    status = _validate_status_id(status_id, "status_id")
    turn_match = connection.execute(
        "SELECT chain_id FROM conversation_turns WHERE status_id = ?",
        (status,),
    ).fetchone()
    if turn_match is not None:
        chain = connection.execute(
            "SELECT * FROM conversation_chains WHERE chain_id = ?",
            (turn_match["chain_id"],),
        ).fetchone()
    else:
        chain = connection.execute(
            "SELECT * FROM conversation_chains WHERE chain_id = ?",
            (status,),
        ).fetchone()
    if chain is None:
        root_matches = connection.execute(
            """
            SELECT *
            FROM conversation_chains
            WHERE root_status_id = ?
            ORDER BY chain_id
            """,
            (status,),
        ).fetchall()
        if len(root_matches) > 1:
            raise ValueError(
                f"Ambiguous conversation history for root status {status}"
            )
        chain = root_matches[0] if root_matches else None
    if chain is None:
        raise KeyError(f"No conversation history for status {status}")
    turns = connection.execute(
        """
        SELECT *
        FROM conversation_turns
        WHERE chain_id = ?
        ORDER BY COALESCE(posted_at, observed_at), CAST(status_id AS INTEGER)
        """,
        (chain["chain_id"],),
    ).fetchall()
    result_turns: list[dict[str, Any]] = []
    for turn in turns:
        sources = connection.execute(
            """
            SELECT source_url
            FROM conversation_sources
            WHERE status_id = ?
            ORDER BY source_url
            """,
            (turn["status_id"],),
        ).fetchall()
        result_turns.append(
            {
                "status_id": turn["status_id"],
                "parent_status_id": turn["parent_status_id"],
                "actor": turn["actor"],
                "author": turn["author"],
                "url": turn["url"],
                "exact_text": turn["exact_text"],
                "media": json.loads(turn["media_json"] or "[]"),
                "posted_at": turn["posted_at"],
                "observed_at": turn["observed_at"],
                "provenance": turn["provenance"],
                "source_urls": [row["source_url"] for row in sources],
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "chain_id": chain["chain_id"],
        "root_status_id": chain["root_status_id"],
        "provenance": chain["provenance"],
        "chatgpt_conversation_url": chain["chatgpt_conversation_url"],
        "ledger_reference": chain["ledger_reference"],
        "turns": result_turns,
    }


def export_history(
    connection: sqlite3.Connection,
    output: Path,
) -> dict[str, Any]:
    chain_ids = [
        str(row["chain_id"])
        for row in connection.execute(
            "SELECT chain_id FROM conversation_chains ORDER BY chain_id"
        ).fetchall()
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for chain_id in chain_ids:
            handle.write(
                json.dumps(
                    history_chain_for_status(connection, chain_id),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    temporary.replace(output)
    return {
        "output": str(output),
        "chains": len(chain_ids),
    }


def history_status(connection: sqlite3.Connection) -> dict[str, Any]:
    chains = int(
        connection.execute(
            "SELECT COUNT(*) AS count FROM conversation_chains"
        ).fetchone()["count"]
    )
    turns = int(
        connection.execute(
            "SELECT COUNT(*) AS count FROM conversation_turns"
        ).fetchone()["count"]
    )
    sources = int(
        connection.execute(
            "SELECT COUNT(*) AS count FROM conversation_sources"
        ).fetchone()["count"]
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "chains": chains,
        "turns": turns,
        "sources": sources,
    }


def memory_audit(
    config: Config,
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    integrity_rows = [
        str(row[0])
        for row in connection.execute("PRAGMA integrity_check").fetchall()
    ]
    foreign_key_violations = len(
        connection.execute("PRAGMA foreign_key_check").fetchall()
    )
    configured_user_id = get_meta(connection, "configured_user_id")
    initial_audit = initial_audit_status(connection)
    history = history_status(connection)
    event_row = connection.execute(
        """
        SELECT COUNT(*) AS event_count,
               COUNT(DISTINCT author_id) AS stable_author_count,
               COUNT(DISTINCT lower(username)) AS username_count,
               SUM(
                   CASE
                       WHEN COALESCE(author_id, '') = ''
                         OR COALESCE(username, '') = ''
                       THEN 1 ELSE 0
                   END
               ) AS missing_identity_count
        FROM events
        """
    ).fetchone()
    turn_row = connection.execute(
        """
        SELECT SUM(
                   CASE
                       WHEN actor IN ('user', 'target')
                        AND COALESCE(author, '') = ''
                       THEN 1 ELSE 0
                   END
               ) AS missing_user_author_count
        FROM conversation_turns
        """
    ).fetchone()
    archive_row = connection.execute(
        """
        SELECT COUNT(*) AS import_count,
               COALESCE(SUM(post_count), 0) AS declared_post_count,
               COALESCE(SUM(inserted_post_count), 0) AS inserted_post_count,
               SUM(
                   CASE
                       WHEN account_user_id <> ?
                       THEN 1 ELSE 0
                   END
               ) AS owner_mismatch_count,
               SUM(
                   CASE
                       WHEN post_count <= 0
                         OR inserted_post_count < 0
                         OR inserted_post_count > post_count
                       THEN 1 ELSE 0
                   END
               ) AS invalid_count_count
        FROM archive_imports
        """,
        (config.user_id,),
    ).fetchone()
    archive_post_row = connection.execute(
        """
        SELECT COUNT(*) AS post_count,
               SUM(
                   CASE
                       WHEN author_id <> ?
                       THEN 1 ELSE 0
                   END
               ) AS owner_mismatch_count,
               SUM(
                   CASE
                       WHEN canonical_url <>
                            'https://x.com/i/web/status/' || status_id
                         OR length(payload_sha256) <> 64
                       THEN 1 ELSE 0
                   END
               ) AS invalid_record_count
        FROM archive_posts
        """,
        (config.user_id,),
    ).fetchone()
    archive_alias_mismatch = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM archive_imports i
            LEFT JOIN archive_account_aliases a
              ON a.account_user_id = i.account_user_id
             AND lower(a.username) = lower(i.username)
            WHERE a.account_user_id IS NULL
            """
        ).fetchone()[0]
    )
    candidate_row = connection.execute(
        """
        SELECT COUNT(*) AS import_count,
               COALESCE(SUM(inserted_record_count), 0) AS inserted_count,
               SUM(
                   CASE
                       WHEN record_count <= 0
                         OR inserted_record_count < 0
                         OR inserted_record_count > record_count
                       THEN 1 ELSE 0
                   END
               ) AS invalid_count_count
        FROM candidate_corpus_imports
        """
    ).fetchone()
    candidate_post_row = connection.execute(
        """
        SELECT COUNT(*) AS post_count,
               SUM(
                   CASE
                       WHEN post.subject_user_id <> import.subject_user_id
                         OR post.trust_state <> 'unverified_candidate'
                         OR length(post.payload_sha256) <> 64
                       THEN 1 ELSE 0
                   END
               ) AS invalid_record_count
        FROM candidate_public_posts post
        JOIN candidate_corpus_imports import
          ON import.id = post.first_import_id
        """
    ).fetchone()
    candidate_verification_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM candidate_post_verifications"
        ).fetchone()[0]
    )

    current_errors: list[str] = []
    if integrity_rows != ["ok"]:
        current_errors.append("sqlite_integrity_check_failed")
    if foreign_key_violations:
        current_errors.append("foreign_key_violations")
    if configured_user_id != config.user_id:
        current_errors.append("configured_user_id_mismatch")
    if int(event_row["missing_identity_count"] or 0):
        current_errors.append("events_missing_stable_identity")
    if int(turn_row["missing_user_author_count"] or 0):
        current_errors.append("history_turns_missing_author")
    if not initial_audit["complete"]:
        current_errors.append("initial_audit_incomplete")
    if not initial_audit["history_complete"]:
        current_errors.append("resolution_history_incomplete")
    if not initial_audit["invariant_ok"]:
        current_errors.append("resolution_invariant_failed")
    if int(candidate_row["invalid_count_count"] or 0):
        current_errors.append("candidate_import_counts_invalid")
    if int(candidate_post_row["invalid_record_count"] or 0):
        current_errors.append("candidate_records_invalid")
    if (
        int(candidate_row["inserted_count"] or 0)
        != int(candidate_post_row["post_count"] or 0)
    ):
        current_errors.append("candidate_inserted_count_mismatch")

    archive_errors: list[str] = []
    archive_import_count = int(archive_row["import_count"] or 0)
    archive_post_count = int(archive_post_row["post_count"] or 0)
    if int(archive_row["owner_mismatch_count"] or 0):
        archive_errors.append("archive_import_owner_mismatch")
    if int(archive_post_row["owner_mismatch_count"] or 0):
        archive_errors.append("archive_post_owner_mismatch")
    if int(archive_row["invalid_count_count"] or 0):
        archive_errors.append("archive_import_counts_invalid")
    if int(archive_post_row["invalid_record_count"] or 0):
        archive_errors.append("archive_records_invalid")
    if archive_alias_mismatch:
        archive_errors.append("archive_account_alias_missing")
    if (
        int(archive_row["inserted_post_count"] or 0)
        != archive_post_count
    ):
        archive_errors.append("archive_inserted_count_mismatch")

    current_memory_ok = not current_errors and not archive_errors
    archive_ready = (
        archive_import_count > 0
        and archive_post_count > 0
        and not archive_errors
    )
    final_complete = current_memory_ok and archive_ready
    status = (
        "complete"
        if final_complete
        else "archive_pending"
        if current_memory_ok
        else "invalid"
    )
    return {
        "status": status,
        "schema_version": SCHEMA_VERSION,
        "current_memory_ok": current_memory_ok,
        "archive_ready": archive_ready,
        "final_complete": final_complete,
        "errors": current_errors,
        "archive_errors": archive_errors,
        "database": {
            "integrity_check": integrity_rows,
            "foreign_key_violations": foreign_key_violations,
        },
        "identity": {
            "configured_user_id": configured_user_id,
            "expected_user_id": config.user_id,
            "events": int(event_row["event_count"]),
            "stable_authors": int(event_row["stable_author_count"]),
            "usernames": int(event_row["username_count"]),
            "missing_event_identities": int(
                event_row["missing_identity_count"] or 0
            ),
            "missing_history_authors": int(
                turn_row["missing_user_author_count"] or 0
            ),
        },
        "history": {
            **history,
            "initial_audit": initial_audit,
        },
        "archive": {
            "import_count": archive_import_count,
            "declared_post_count": int(
                archive_row["declared_post_count"] or 0
            ),
            "inserted_post_count": int(
                archive_row["inserted_post_count"] or 0
            ),
            "post_count": archive_post_count,
            "account_alias_mismatch": archive_alias_mismatch,
            "direct_messages_imported": 0,
        },
        "candidate_corpus": {
            "import_count": int(candidate_row["import_count"] or 0),
            "inserted_count": int(candidate_row["inserted_count"] or 0),
            "post_count": int(candidate_post_row["post_count"] or 0),
            "verified_post_count": candidate_verification_count,
            "unverified_posts_usable_as_evidence": 0,
        },
    }


def record_failure(
    config: Config,
    connection: sqlite3.Connection,
    error: Exception,
    *,
    source: str,
    started_at: str,
) -> dict[str, Any]:
    completed_at = isoformat()
    previous_failures = int(get_meta(connection, "consecutive_failures") or "0")
    failures = previous_failures + 1
    safe_message = safe_error_message(error)
    with connection:
        set_meta(connection, "consecutive_failures", str(failures))
        set_meta(connection, "last_error_class", type(error).__name__)
        set_meta(connection, "last_error_message", safe_message)
        connection.execute(
            """
            INSERT INTO poll_runs(
                started_at, completed_at, source, status, new_count,
                error_class, error_message
            ) VALUES(?, ?, ?, 'failure', 0, ?, ?)
            """,
            (started_at, completed_at, source, type(error).__name__, safe_message),
        )
    return write_health(config, connection, last_new_count=0)


def safe_error_message(error: Exception) -> str:
    message = str(error)
    message = re.sub(r"(?i)bearer\s+[^\s\"']+", "Bearer [REDACTED]", message)
    for name in TOKEN_ENV_NAMES:
        token = os.environ.get(name)
        if token:
            message = message.replace(token, "[REDACTED]")
    return message[:500]


def write_health(
    config: Config,
    connection: sqlite3.Connection,
    *,
    last_new_count: int,
) -> dict[str, Any]:
    failures = int(get_meta(connection, "consecutive_failures") or "0")
    status = "healthy" if failures == 0 else "degraded"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "updated_at": isoformat(),
        "first_success_at": get_meta(connection, "first_success_at"),
        "last_success_at": get_meta(connection, "last_success_at"),
        "last_new_event_at": get_meta(connection, "last_new_event_at"),
        "last_new_count": last_new_count,
        "last_seen_id": get_meta(connection, "since_id"),
        "consecutive_failures": failures,
        "last_error_class": get_meta(connection, "last_error_class"),
        "last_error_message": get_meta(connection, "last_error_message"),
        "queued_events": len(queued_events(connection)),
        "poll_interval_seconds": config.poll_interval_seconds,
    }
    atomic_write_json(config.health_file, payload)
    return payload


def bearer_token_with_source(config: Config) -> tuple[str, str]:
    for name in TOKEN_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            return value, f"environment:{name}"
    if sys.platform == "darwin" and config.keychain_service and config.keychain_account:
        try:
            if config.keychain_helper.is_file() and os.access(
                config.keychain_helper, os.X_OK
            ):
                command = [
                    str(config.keychain_helper),
                    "get",
                    config.keychain_service,
                    config.keychain_account,
                ]
                source = "keychain_helper"
            else:
                command = [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    config.keychain_service,
                    "-a",
                    config.keychain_account,
                    "-w",
                ]
                source = "keychain"
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("Unable to read X API bearer token from Keychain") from error
        value = result.stdout.strip()
        if value:
            return value, source
    raise RuntimeError(
        "Missing X API bearer token. Set an environment variable or configured Keychain item."
    )


def bearer_token(config: Config) -> str:
    return bearer_token_with_source(config)[0]


def preflight(config: Config) -> dict[str, Any]:
    user_id_valid = bool(config.user_id and config.user_id.isdigit())
    try:
        _, source = bearer_token_with_source(config)
        token_available = True
    except RuntimeError:
        source = None
        token_available = False
    return {
        "config": str(config.source_path),
        "user_id_configured": user_id_valid,
        "token_available": token_available,
        "token_source": source,
        "database_parent_writable": os.access(config.database.parent, os.W_OK)
        if config.database.parent.exists()
        else os.access(config.database.parent.parent, os.W_OK),
        "ready_for_live_poll": user_id_valid and token_available,
    }


def notify_macos(config: Config, title: str, body: str) -> None:
    if not config.notifications_enabled or sys.platform != "darwin":
        return
    try:
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                "on run argv",
                "-e",
                "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e",
                "end run",
                title,
                body,
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def request_json(url: str, token: str, timeout_seconds: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "axrbarsic-x-mention-watcher/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read(1000).decode("utf-8", errors="replace")
        detail = ""
        try:
            payload = json.loads(body)
            detail = str(payload.get("title") or payload.get("detail") or "")
        except (TypeError, ValueError):
            detail = ""
        suffix = f" ({detail[:200]})" if detail else ""
        raise RuntimeError(f"X API HTTP {error.code}{suffix}") from error


def active_conversation_ids(
    config: Config,
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> list[str]:
    current = now or utc_now()
    cutoff = isoformat(
        current - timedelta(hours=config.conversation_tail_watch_hours)
    )
    rows = connection.execute(
        """
        SELECT chain_id, MAX(COALESCE(posted_at, observed_at)) AS latest_alex_at
        FROM conversation_turns
        WHERE actor = 'alex'
          AND COALESCE(posted_at, observed_at) >= ?
        GROUP BY chain_id
        ORDER BY latest_alex_at DESC, chain_id DESC
        LIMIT ?
        """,
        (cutoff, config.conversation_tail_max_conversations),
    ).fetchall()
    return [str(row["chain_id"]) for row in rows]


def conversation_tail_query_chunks(
    conversation_ids: Iterable[str],
    *,
    account_handle: str,
    max_query_length: int = 3800,
) -> list[str]:
    suffix = " is:reply"
    handle = account_handle.strip().lstrip("@")
    if handle:
        suffix += f" -from:{handle}"
    chunks: list[str] = []
    current: list[str] = []
    for conversation_id in conversation_ids:
        if not str(conversation_id).isdigit():
            raise ValueError("Conversation tail contains a non-numeric ID")
        clause = f"conversation_id:{conversation_id}"
        candidate = f"({' OR '.join([*current, clause])}){suffix}"
        if current and len(candidate) > max_query_length:
            chunks.append(f"({' OR '.join(current)}){suffix}")
            current = [clause]
        else:
            current.append(clause)
    if current:
        chunks.append(f"({' OR '.join(current)}){suffix}")
    return chunks


def poll_conversation_tails(
    config: Config,
    connection: sqlite3.Connection,
    *,
    token: str,
    fetch: Callable[[str, str, int], dict[str, Any]] = request_json,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or utc_now()
    last_success = parse_time(
        get_meta(connection, "conversation_tail_last_success_at")
    )
    if last_success is not None:
        age = (current - last_success).total_seconds()
        if age < config.conversation_tail_poll_interval_seconds:
            return {
                "source": "x_api_conversation_tail",
                "status": "not_due",
                "new_count": 0,
                "new_event_ids": [],
            }

    conversation_ids = active_conversation_ids(
        config,
        connection,
        now=current,
    )
    if not conversation_ids:
        with connection:
            set_meta(
                connection,
                "conversation_tail_last_success_at",
                isoformat(current),
            )
        return {
            "source": "x_api_conversation_tail",
            "status": "no_active_conversations",
            "tracked_conversation_count": 0,
            "new_count": 0,
            "new_event_ids": [],
        }

    if last_success is None:
        start_time = current - timedelta(
            hours=config.conversation_tail_initial_lookback_hours
        )
    else:
        start_time = last_success - timedelta(
            seconds=config.conversation_tail_overlap_seconds
        )
    watch_cutoff = current - timedelta(hours=config.conversation_tail_watch_hours)
    start_time = max(start_time, watch_cutoff)
    queries = conversation_tail_query_chunks(
        conversation_ids,
        account_handle=config.keychain_account,
    )

    all_data: list[dict[str, Any]] = []
    all_users: dict[str, dict[str, Any]] = {}
    all_media: dict[str, dict[str, Any]] = {}
    newest_ids: list[str | None] = []
    page_count = 0
    for query in queries:
        pagination_token: str | None = None
        seen_pagination_tokens: set[str] = set()
        while True:
            page_count += 1
            if page_count > config.max_pages_per_poll:
                raise RuntimeError(
                    "X conversation tail pagination exceeded configured page limit"
                )
            params: dict[str, str] = {
                "query": query,
                "start_time": isoformat(start_time),
                "max_results": "100",
                "sort_order": "recency",
                "tweet.fields": (
                    "author_id,created_at,conversation_id,in_reply_to_user_id,"
                    "referenced_tweets,attachments"
                ),
                "expansions": "author_id,attachments.media_keys",
                "user.fields": "username",
                "media.fields": (
                    "media_key,type,url,preview_image_url,alt_text,"
                    "width,height,duration_ms"
                ),
            }
            if pagination_token:
                params["pagination_token"] = pagination_token
            url = (
                f"{config.api_base}/tweets/search/recent?"
                f"{urllib.parse.urlencode(params)}"
            )
            page = fetch(url, token, config.request_timeout_seconds)
            if not isinstance(page, dict):
                raise RuntimeError(
                    "X conversation tail returned a non-object JSON response"
                )
            if page.get("errors"):
                raise RuntimeError(
                    "X conversation tail returned errors: "
                    + json.dumps(page["errors"], ensure_ascii=False)[:1000]
                )
            all_data.extend(page.get("data", []) or [])
            for user in page.get("includes", {}).get("users", []) or []:
                if user.get("id"):
                    all_users[str(user["id"])] = user
            for item in page.get("includes", {}).get("media", []) or []:
                if item.get("media_key"):
                    all_media[str(item["media_key"])] = item
            meta = page.get("meta", {}) or {}
            newest_ids.append(meta.get("newest_id"))
            pagination_token = meta.get("next_token")
            if not pagination_token:
                break
            if pagination_token in seen_pagination_tokens:
                raise RuntimeError(
                    "X conversation tail returned a repeated pagination token"
                )
            seen_pagination_tokens.add(pagination_token)

    merged = {
        "data": all_data,
        "includes": {
            "users": list(all_users.values()),
            "media": list(all_media.values()),
        },
        "meta": {"newest_id": numeric_max(newest_ids)},
    }
    result = ingest_response(
        config,
        connection,
        merged,
        source="x_api_conversation_tail",
        started_at=isoformat(current),
    )
    return {
        **result,
        "status": "success",
        "tracked_conversation_count": len(conversation_ids),
        "query_count": len(queries),
        "start_time": isoformat(start_time),
    }


def poll_live(
    config: Config,
    connection: sqlite3.Connection,
    *,
    fetch: Callable[[str, str, int], dict[str, Any]] = request_json,
) -> dict[str, Any]:
    started_at = isoformat()
    failure_source = "x_api"
    try:
        if (
            not config.user_id
            or config.user_id == "REPLACE_WITH_X_USER_ID"
            or not config.user_id.isdigit()
        ):
            raise RuntimeError("config user_id is not configured as a numeric X user ID")
        token = bearer_token(config)
        since_id = get_meta(connection, "since_id")
        all_data: list[dict[str, Any]] = []
        all_users: dict[str, dict[str, Any]] = {}
        all_media: dict[str, dict[str, Any]] = {}
        newest_ids: list[str | None] = [since_id]
        pagination_token: str | None = None
        seen_pagination_tokens: set[str] = set()
        page_count = 0

        while True:
            page_count += 1
            if page_count > config.max_pages_per_poll:
                raise RuntimeError("X API pagination exceeded configured page limit")
            params: dict[str, str] = {
                "max_results": "100",
                "tweet.fields": (
                    "author_id,created_at,conversation_id,in_reply_to_user_id,"
                    "referenced_tweets,attachments"
                ),
                "expansions": "author_id,attachments.media_keys",
                "user.fields": "username",
                "media.fields": (
                    "media_key,type,url,preview_image_url,alt_text,"
                    "width,height,duration_ms"
                ),
            }
            if since_id:
                params["since_id"] = since_id
            if pagination_token:
                params["pagination_token"] = pagination_token
            url = (
                f"{config.api_base}/users/{urllib.parse.quote(config.user_id)}/mentions?"
                f"{urllib.parse.urlencode(params)}"
            )
            page = fetch(url, token, config.request_timeout_seconds)
            if not isinstance(page, dict):
                raise RuntimeError("X API returned a non-object JSON response")
            if page.get("errors"):
                raise RuntimeError(
                    "X API returned errors: "
                    + json.dumps(page["errors"], ensure_ascii=False)[:1000]
                )
            all_data.extend(page.get("data", []) or [])
            for user in page.get("includes", {}).get("users", []) or []:
                if user.get("id"):
                    all_users[str(user["id"])] = user
            for item in page.get("includes", {}).get("media", []) or []:
                if item.get("media_key"):
                    all_media[str(item["media_key"])] = item
            meta = page.get("meta", {}) or {}
            newest_ids.append(meta.get("newest_id"))
            pagination_token = meta.get("next_token")
            if not pagination_token:
                break
            if pagination_token in seen_pagination_tokens:
                raise RuntimeError("X API returned a repeated pagination token")
            seen_pagination_tokens.add(pagination_token)

        merged = {
            "data": all_data,
            "includes": {
                "users": list(all_users.values()),
                "media": list(all_media.values()),
            },
            "meta": {"newest_id": numeric_max(newest_ids)},
        }
        mention_result = ingest_response(
            config,
            connection,
            merged,
            source="x_api",
            started_at=started_at,
        )
        if not config.conversation_tail_enabled:
            return mention_result

        failure_source = "x_api_conversation_tail"
        tail_result = poll_conversation_tails(
            config,
            connection,
            token=token,
            fetch=fetch,
        )
        combined_new_ids = sorted(
            set(mention_result["new_event_ids"])
            | set(tail_result["new_event_ids"]),
            key=int,
        )
        combined_self_authored_ids = sorted(
            set(mention_result["self_authored_event_ids"])
            | set(tail_result.get("self_authored_event_ids", [])),
            key=int,
        )
        return {
            **mention_result,
            "observed_count": (
                mention_result["observed_count"]
                + int(tail_result.get("observed_count", 0))
            ),
            "new_count": len(combined_new_ids),
            "new_event_ids": combined_new_ids,
            "self_authored_event_ids": combined_self_authored_ids,
            "pending_count": int(
                tail_result.get(
                    "pending_count",
                    mention_result["pending_count"],
                )
            ),
            "health": str(
                tail_result.get("health", mention_result["health"])
            ),
            "conversation_tail": tail_result,
        }
    except Exception as error:
        record_failure(
            config,
            connection,
            error,
            source=failure_source,
            started_at=started_at,
        )
        raise


def acknowledge_events(
    config: Config,
    connection: sqlite3.Connection,
    event_ids: list[str],
) -> dict[str, Any]:
    requested = list(dict.fromkeys(event_ids))
    placeholders = ",".join("?" for _ in requested)
    protected_rows = connection.execute(
        f"""
        SELECT event_id, author_id, is_reply, in_reply_to_user_id,
               conversation_id
        FROM events
        WHERE delivery_state = 'queued'
          AND event_id IN ({placeholders})
        """,
        requested,
    ).fetchall()
    protected_ids = sorted(
        (
            str(row["event_id"])
            for row in protected_rows
            if is_eligible_reply(
                connection,
                config,
                author_id=row["author_id"],
                is_reply=bool(row["is_reply"]),
                in_reply_to_user_id=row["in_reply_to_user_id"],
                conversation_id=row["conversation_id"],
            )
        ),
        key=int,
    )
    if protected_ids:
        raise ValueError(
            "Eligible reply events require a durable resolve disposition: "
            + ", ".join(protected_ids)
        )
    rows = connection.execute(
        f"""
        SELECT event_id
        FROM events
        WHERE delivery_state = 'queued' AND event_id IN ({placeholders})
        """,
        requested,
    ).fetchall()
    acknowledged = sorted((str(row["event_id"]) for row in rows), key=int)
    rejected = sorted(set(requested) - set(acknowledged), key=int)
    with connection:
        connection.executemany(
            "UPDATE events SET delivery_state = 'acknowledged' WHERE event_id = ?",
            [(event_id,) for event_id in acknowledged],
        )
    wake = refresh_wake_file(config, connection)
    health = write_health(config, connection, last_new_count=0)
    return {
        "acknowledged": acknowledged,
        "not_queued": rejected,
        "pending_count": wake["pending_count"],
        "health": health["status"],
    }


def evaluate_health(config: Config, now: datetime | None = None) -> dict[str, Any]:
    current = now or utc_now()
    if not config.health_file.exists():
        return {
            "status": "never_started",
            "healthy": False,
            "reason": "health_file_missing",
        }
    health = read_json(config.health_file)
    first_success = parse_time(health.get("first_success_at"))
    last_success = parse_time(health.get("last_success_at"))
    last_new = parse_time(health.get("last_new_event_at"))
    failures = int(health.get("consecutive_failures") or 0)
    last_error_class = health.get("last_error_class")
    last_error_message = health.get("last_error_message")

    if str(last_error_message or "").startswith("X API HTTP 402"):
        status = "billing_blocked"
        reason = "x_api_credits_depleted"
        healthy = False
    elif last_success is None:
        status = "never_succeeded"
        reason = "last_success_missing"
        healthy = False
    else:
        success_age = (current - last_success).total_seconds()
        if success_age > config.stale_after_seconds:
            status = "stale"
            reason = "last_success_too_old"
            healthy = False
        elif failures >= config.failure_threshold:
            status = "failing"
            reason = "failure_threshold_reached"
            healthy = False
        elif (
            (last_new or first_success) is not None
            and (current - (last_new or first_success)).total_seconds()
            > config.silence_review_seconds
        ):
            status = "healthy_silence_review"
            reason = "no_new_events_review_due"
            healthy = True
        else:
            status = "healthy"
            reason = "polling_current"
            healthy = True

    return {
        "status": status,
        "healthy": healthy,
        "reason": reason,
        "checked_at": isoformat(current),
        "first_success_at": health.get("first_success_at"),
        "last_success_at": health.get("last_success_at"),
        "last_new_event_at": health.get("last_new_event_at"),
        "consecutive_failures": failures,
        "queued_events": int(health.get("queued_events") or 0),
        "last_error_class": last_error_class,
        "last_error_message": last_error_message,
    }


def run_watchdog(config: Config, *, loop: bool) -> int:
    previous_status: str | None = None
    if config.alert_file.exists():
        try:
            previous_status = str(read_json(config.alert_file).get("status") or "") or None
        except (OSError, ValueError):
            previous_status = None
    while True:
        result = evaluate_health(config)
        if result["status"] != previous_status:
            atomic_write_json(config.alert_file, result)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
            if result["status"] == "healthy":
                notify_macos(config, "X watcher восстановлен", "Проверка X снова работает.")
            else:
                notify_macos(
                    config,
                    "Требуется проверка X watcher",
                    f"Состояние: {result['status']}. Причина: {result['reason']}.",
                )
            previous_status = str(result["status"])
        if not loop:
            return 0 if result["healthy"] else 2
        time.sleep(config.watchdog_interval_seconds)


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="Path to watcher JSON config.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    poll = commands.add_parser("poll", help="Poll X API once or ingest a fixture.")
    poll.add_argument("--fixture", type=Path)
    poll.add_argument("--quiet", action="store_true")

    commands.add_parser("watch", help="Poll continuously at the configured interval.")
    commands.add_parser("preflight", help="Check config and token availability safely.")
    status = commands.add_parser(
        "status",
        help="Print compact health and queue status.",
    )
    status.add_argument(
        "--full",
        action="store_true",
        help="Include every queued event in the command output.",
    )
    commands.add_parser(
        "baseline",
        help="Legacy recovery command. Do not use for a normal first audit.",
    )
    commands.add_parser(
        "self-authored-reconcile",
        help=(
            "Remove unresolved posts authored by the configured account from "
            "the inbound reply queue without creating an event resolution."
        ),
    )
    commands.add_parser(
        "initial-audit-start",
        help="Queue every unresolved historical direct reply for first review.",
    )
    commands.add_parser(
        "initial-audit-status",
        help="Show first-review progress and remaining queued events.",
    )
    audit_next = commands.add_parser(
        "initial-audit-next",
        help="Show the next unresolved conversation groups newest-first.",
    )
    audit_next.add_argument(
        "--conversations",
        type=int,
        default=1,
        help="Number of conversation groups to return.",
    )
    audit_expire = commands.add_parser(
        "initial-audit-expire",
        help=(
            "Durably skip unresolved replies older than Alex's one-time "
            "lookback window without Browser review."
        ),
    )
    audit_expire.add_argument(
        "--hours",
        type=float,
        required=True,
        help=(
            "One-time lookback window selected by Alex for this run. "
            "The UTC cutoff is fixed when the command starts."
        ),
    )
    audit_expire.add_argument(
        "--as-of",
        required=True,
        help=(
            "Timezone-aware ISO-8601 cycle start. Reuse the exact same value "
            "for dry-run and apply."
        ),
    )
    audit_expire.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report candidates without changing durable state.",
    )
    response_requeue = commands.add_parser(
        "mandatory-response-requeue",
        help=(
            "Requeue recent content-based skips and previously ignored mention "
            "replies that have no exact Alex child reply."
        ),
    )
    response_requeue.add_argument(
        "--hours",
        type=float,
        required=True,
        help="Recent lookback window to reconcile under mandatory mode.",
    )
    response_requeue.add_argument(
        "--as-of",
        required=True,
        help="Timezone-aware ISO-8601 reconciliation time.",
    )
    response_requeue.add_argument(
        "--dry-run",
        action="store_true",
        help="Report candidates without changing queue state.",
    )
    commands.add_parser(
        "initial-audit-complete",
        help="Complete first review only when the queue is empty.",
    )
    audit_export = commands.add_parser(
        "initial-audit-export",
        help="Export canonical initial-audit resolutions as JSONL.",
    )
    audit_export.add_argument("--output", type=Path, required=True)
    history_import = commands.add_parser(
        "history-import",
        help="Import append-only conversation chain snapshots.",
    )
    history_import.add_argument("--file", type=Path, required=True)
    browser_sync = commands.add_parser(
        "browser-handoff-sync",
        help=(
            "Import exact Browser history and durably resolve confirmed "
            "initial-audit dispositions."
        ),
    )
    browser_sync.add_argument("--history-file", type=Path, required=True)
    browser_sync.add_argument("--ledger-file", type=Path, required=True)

    history_show = commands.add_parser(
        "history-show",
        help="Show a complete stored conversation chain for one status ID.",
    )
    history_show.add_argument("status_id")

    history_export = commands.add_parser(
        "history-export",
        help="Export canonical conversation history as JSONL.",
    )
    history_export.add_argument("--output", type=Path, required=True)

    commands.add_parser(
        "history-status",
        help="Show conversation history database counts.",
    )
    memory_check = commands.add_parser(
        "memory-audit",
        help=(
            "Fail closed on database, identity, history, archive, and "
            "candidate-memory inconsistencies."
        ),
    )
    memory_check.add_argument(
        "--require-archive",
        action="store_true",
        help=(
            "Return failure until a valid official X archive import is "
            "present."
        ),
    )
    commenter_history = commands.add_parser(
        "commenter-history",
        help=(
            "Show source-linked public interaction history for the author of "
            "one stored event."
        ),
    )
    commenter_history.add_argument("event_id")
    commenter_history.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum prior interactions to return, newest-first.",
    )

    acknowledge = commands.add_parser("ack", help="Acknowledge queued event IDs.")
    acknowledge.add_argument("event_ids", nargs="+")

    resolve = commands.add_parser(
        "resolve",
        help=(
            "Durably resolve one queued event as published, skipped, or "
            "contract-blocked."
        ),
    )
    resolve.add_argument("event_id")
    resolve.add_argument(
        "--disposition",
        choices=("published", "skip", "blocked"),
        required=True,
    )
    resolve.add_argument("--reason", required=True)
    resolve.add_argument("--reply-url")
    resolve.add_argument(
        "--blocker-code",
        choices=tuple(sorted(TERMINAL_BLOCKER_CODES)),
    )
    resolve.add_argument(
        "--stance",
        choices=("supportive", "opposing", "neutral", "ambiguous"),
    )
    resolve.add_argument("--stance-detail")
    resolve.add_argument(
        "--confidence",
        choices=("high", "medium", "low"),
    )
    resolve.add_argument("--media-meaning")
    resolve.add_argument("--evidence", action="append", default=[])
    revise = commands.add_parser(
        "revise-resolution",
        help=(
            "Auditably revise an existing skip after an explicit scope "
            "change and verified publication."
        ),
    )
    revise.add_argument("event_id")
    revise.add_argument(
        "--disposition",
        choices=("published", "skip", "blocked"),
        required=True,
    )
    revise.add_argument("--reason", required=True)
    revise.add_argument("--reply-url")
    revise.add_argument(
        "--blocker-code",
        choices=tuple(sorted(TERMINAL_BLOCKER_CODES)),
    )
    revise.add_argument("--revision-reason", required=True)
    revise.add_argument(
        "--stance",
        choices=("supportive", "opposing", "neutral", "ambiguous"),
    )
    revise.add_argument("--stance-detail")
    revise.add_argument(
        "--confidence",
        choices=("high", "medium", "low"),
    )
    revise.add_argument("--media-meaning")
    revise.add_argument("--evidence", action="append", default=[])

    watchdog = commands.add_parser("watchdog", help="Check watcher health.")
    watchdog.add_argument("--loop", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)

    if args.command == "preflight":
        result = preflight(config)
        print_json(result)
        return 0 if result["ready_for_live_poll"] else 2

    connection = connect_database(config.database)
    try:
        if args.command == "poll":
            try:
                with exclusive_process_lock(config.lock_file):
                    if args.fixture:
                        payload = read_json(args.fixture)
                        result = ingest_response(
                            config,
                            connection,
                            payload,
                            source=f"fixture:{args.fixture.name}",
                        )
                    else:
                        result = poll_live(config, connection)
            except AlreadyRunningError:
                result = {"status": "already_running"}
            except Exception as error:
                print_json(
                    {
                        "status": "poll_failed",
                        "error_class": type(error).__name__,
                    }
                )
                return 1
            if not args.quiet:
                print_json(result)
            return 0

        if args.command == "watch":
            try:
                with exclusive_process_lock(config.lock_file):
                    while True:
                        try:
                            print_json(poll_live(config, connection))
                        except Exception as error:
                            print(
                                json.dumps(
                                    {
                                        "status": "poll_failed",
                                        "error_class": type(error).__name__,
                                    },
                                    sort_keys=True,
                                ),
                                file=sys.stderr,
                                flush=True,
                            )
                        time.sleep(config.poll_interval_seconds)
            except AlreadyRunningError:
                print_json({"status": "already_running"})
                return 0

        if args.command == "status":
            queue = refresh_wake_file(config, connection)
            queue_output = queue if args.full else {
                "schema_version": queue["schema_version"],
                "updated_at": queue["updated_at"],
                "pending_count": queue["pending_count"],
                "newest_event": queue["events"][0] if queue["events"] else None,
            }
            print_json(
                {
                    "health": evaluate_health(config),
                    "queue": queue_output,
                }
            )
            return 0

        if args.command == "baseline":
            print_json(
                {
                    "status": "blocked",
                    "message": (
                        "Legacy baseline is disabled. Use initial-audit-start "
                        "and resolve every queued direct reply."
                    ),
                }
            )
            return 2

        if args.command == "self-authored-reconcile":
            print_json(reconcile_self_authored_events(config, connection))
            return 0

        if args.command == "initial-audit-start":
            print_json(start_initial_audit(config, connection))
            return 0

        if args.command == "initial-audit-status":
            print_json(initial_audit_status(connection))
            return 0

        if args.command == "initial-audit-next":
            try:
                print_json(
                    initial_audit_next(
                        connection,
                        conversation_limit=args.conversations,
                    )
                )
            except ValueError as error:
                print_json({"status": "blocked", "message": str(error)})
                return 2
            return 0

        if args.command == "initial-audit-expire":
            try:
                expiry_as_of = parse_time(args.as_of)
                if (
                    expiry_as_of is None
                    or expiry_as_of.tzinfo is None
                    or expiry_as_of.utcoffset() is None
                ):
                    raise ValueError(
                        "Expiry --as-of must be a timezone-aware ISO-8601 value"
                    )
                result = expire_initial_audit_events(
                    config,
                    connection,
                    response_window_hours=args.hours,
                    now=expiry_as_of,
                    dry_run=args.dry_run,
                )
                print_json(result)
            except ValueError as error:
                print_json({"status": "blocked", "message": str(error)})
                return 2
            return 2 if result["blocked_candidates"] else 0

        if args.command == "initial-audit-complete":
            try:
                print_json(complete_initial_audit(config, connection))
            except ValueError as error:
                print_json({"status": "blocked", "message": str(error)})
                return 2
            return 0

        if args.command == "mandatory-response-requeue":
            try:
                requeue_as_of = parse_time(args.as_of)
                if (
                    requeue_as_of is None
                    or requeue_as_of.tzinfo is None
                    or requeue_as_of.utcoffset() is None
                ):
                    raise ValueError(
                        "Policy requeue --as-of must be timezone-aware"
                    )
                print_json(
                    requeue_unanswered_skips(
                        config,
                        connection,
                        response_window_hours=args.hours,
                        now=requeue_as_of,
                        dry_run=args.dry_run,
                    )
                )
            except ValueError as error:
                print_json({"status": "blocked", "message": str(error)})
                return 2
            return 0

        if args.command == "initial-audit-export":
            print_json(export_audit_resolutions(connection, args.output))
            return 0

        if args.command == "history-import":
            print_json(import_history_file(connection, args.file))
            return 0

        if args.command == "browser-handoff-sync":
            try:
                print_json(
                    sync_browser_handoffs(
                        config,
                        connection,
                        history_path=args.history_file,
                        ledger_path=args.ledger_file,
                    )
                )
            except (KeyError, ValueError) as error:
                print_json({"status": "blocked", "message": str(error)})
                return 2
            return 0

        if args.command == "history-show":
            try:
                print_json(history_chain_for_status(connection, args.status_id))
            except KeyError as error:
                print_json({"status": "not_found", "message": str(error)})
                return 2
            return 0

        if args.command == "history-export":
            print_json(export_history(connection, args.output))
            return 0

        if args.command == "history-status":
            print_json(history_status(connection))
            return 0

        if args.command == "memory-audit":
            result = memory_audit(config, connection)
            print_json(result)
            if not result["current_memory_ok"]:
                return 2
            if args.require_archive and not result["final_complete"]:
                return 2
            return 0

        if args.command == "commenter-history":
            try:
                print_json(
                    commenter_history_for_event(
                        connection,
                        args.event_id,
                        limit=args.limit,
                    )
                )
            except (KeyError, ValueError) as error:
                print_json({"status": "not_found", "message": str(error)})
                return 2
            return 0

        if args.command == "ack":
            try:
                print_json(acknowledge_events(config, connection, args.event_ids))
            except ValueError as error:
                print_json({"status": "blocked", "message": str(error)})
                return 2
            return 0

        if args.command == "resolve":
            try:
                print_json(
                    resolve_event(
                        config,
                        connection,
                        args.event_id,
                        disposition=args.disposition,
                        reason=args.reason,
                        reply_url=args.reply_url,
                        blocker_code=args.blocker_code,
                        stance=args.stance,
                        stance_detail=args.stance_detail,
                        confidence=args.confidence,
                        media_meaning=args.media_meaning,
                        evidence=args.evidence,
                    )
                )
            except (KeyError, ValueError) as error:
                print_json({"status": "blocked", "message": str(error)})
                return 2
            return 0

        if args.command == "revise-resolution":
            try:
                print_json(
                    revise_event_resolution(
                        config,
                        connection,
                        args.event_id,
                        disposition=args.disposition,
                        reason=args.reason,
                        reply_url=args.reply_url,
                        revision_reason=args.revision_reason,
                        blocker_code=args.blocker_code,
                        stance=args.stance,
                        stance_detail=args.stance_detail,
                        confidence=args.confidence,
                        media_meaning=args.media_meaning,
                        evidence=args.evidence,
                    )
                )
            except (KeyError, ValueError) as error:
                print_json({"status": "blocked", "message": str(error)})
                return 2
            return 0

        if args.command == "watchdog":
            return run_watchdog(config, loop=args.loop)

        raise AssertionError(f"Unhandled command: {args.command}")
    except KeyboardInterrupt:
        return 130
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
