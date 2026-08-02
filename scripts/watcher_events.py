#!/usr/bin/env python3
"""Durable event queue and ingestion domain for the X watcher."""

from __future__ import annotations

import fcntl
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from scripts import watcher_constants, watcher_io, watcher_time


SCHEMA_VERSION = watcher_constants.SCHEMA_VERSION
CONVERSATION_TAIL_SOURCE = watcher_constants.CONVERSATION_TAIL_SOURCE
atomic_write_json = watcher_io.atomic_write_json
isoformat = watcher_time.isoformat


@dataclass(frozen=True)
class Dependencies:
    write_health: Callable[..., dict[str, Any]]
    notify_macos: Callable[..., None]


@dataclass(frozen=True)
class IngestedEvents:
    new_ids: list[str]
    observed_ids: list[str]
    seen_ids: list[str]
    self_authored_ids: list[str]

def is_eligible_reply(
    connection: sqlite3.Connection,
    config: Any,
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
    config: Any,
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
    config: Any,
    connection: sqlite3.Connection,
    *,
    dependencies: Dependencies,
) -> dict[str, Any]:
    with connection:
        set_meta(connection, "configured_user_id", config.user_id)
        event_ids = _reclassify_self_authored_events(
            connection,
            config.user_id,
        )
    wake = refresh_wake_file(config, connection)
    health = dependencies.write_health(config, connection, last_new_count=0)
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

def refresh_wake_file(config: Any, connection: sqlite3.Connection) -> dict[str, Any]:
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


def _included_entities(
    response: dict[str, Any],
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    includes = response.get("includes", {})
    users = {
        str(user.get("id")): str(user.get("username"))
        for user in includes.get("users", [])
        if user.get("id") and user.get("username")
    }
    media = {
        str(item.get("media_key")): item
        for item in includes.get("media", [])
        if item.get("media_key")
    }
    return users, media


def _stored_event_payload(
    event: dict[str, Any],
    media: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    stored = dict(event)
    media_keys = (event.get("attachments") or {}).get("media_keys") or []
    included_media = [
        media[str(media_key)]
        for media_key in media_keys
        if str(media_key) in media
    ]
    if included_media:
        stored["included_media"] = included_media
    return stored


def _event_delivery_state(
    config: Any,
    connection: sqlite3.Connection,
    *,
    source: str,
    author_id: str | None,
    is_reply: bool,
    in_reply_to_user_id: str | None,
    conversation_id: str | None,
) -> str:
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
        eligible_reply = True
    if author_id == config.user_id:
        return "self_authored"
    if config.queue_direct_replies_only and not eligible_reply:
        return "ignored"
    return "queued"


def _insert_response_events(
    config: Any,
    connection: sqlite3.Connection,
    response: dict[str, Any],
    *,
    source: str,
    observed_at: str,
) -> IngestedEvents:
    users, media = _included_entities(response)
    new_ids: list[str] = []
    observed_ids: list[str] = []
    seen_ids: list[str] = []
    self_authored_ids: list[str] = []
    for event in response.get("data", []) or []:
        event_id = str(event.get("id", "")).strip()
        if not event_id:
            continue
        seen_ids.append(event_id)
        referenced = event.get("referenced_tweets") or []
        is_reply = any(item.get("type") == "replied_to" for item in referenced)
        author_id = str(event.get("author_id", "")).strip() or None
        in_reply_to_user_id = (
            str(event.get("in_reply_to_user_id", "")).strip() or None
        )
        conversation_id = str(event.get("conversation_id", "")).strip() or None
        delivery_state = _event_delivery_state(
            config,
            connection,
            source=source,
            author_id=author_id,
            is_reply=is_reply,
            in_reply_to_user_id=in_reply_to_user_id,
            conversation_id=conversation_id,
        )
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
                json.dumps(
                    _stored_event_payload(event, media),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                observed_at,
                delivery_state,
            ),
        )
        if cursor.rowcount != 1:
            continue
        observed_ids.append(event_id)
        if delivery_state == "queued":
            new_ids.append(event_id)
        elif delivery_state == "self_authored":
            self_authored_ids.append(event_id)
    return IngestedEvents(
        new_ids=new_ids,
        observed_ids=observed_ids,
        seen_ids=seen_ids,
        self_authored_ids=self_authored_ids,
    )


def _update_poll_metadata(
    config: Any,
    connection: sqlite3.Connection,
    response: dict[str, Any],
    ingested: IngestedEvents,
    *,
    source: str,
    started_at: str,
    observed_at: str,
    returned_count: int,
    request_count: int,
    poll_status: str,
    live_bootstrap: bool,
    advance_conversation_tail_cursor: bool,
    conversation_tail_cursor_at: str | None,
) -> None:
    if source != CONVERSATION_TAIL_SOURCE:
        newest_id = numeric_max(
            [
                get_meta(connection, "since_id"),
                response.get("meta", {}).get("newest_id"),
                *ingested.seen_ids,
            ]
        )
        if newest_id:
            set_meta(connection, "since_id", newest_id)
    if source == CONVERSATION_TAIL_SOURCE and advance_conversation_tail_cursor:
        set_meta(
            connection,
            "conversation_tail_last_success_at",
            conversation_tail_cursor_at or observed_at,
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
    if ingested.new_ids:
        set_meta(connection, "last_new_event_at", observed_at)
    connection.execute(
        """
        INSERT INTO poll_runs(
            started_at, completed_at, source, status, new_count,
            returned_count, request_count
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            started_at,
            observed_at,
            source,
            poll_status,
            len(ingested.new_ids),
            returned_count,
            request_count,
        ),
    )


def ingest_response(
    config: Any,
    connection: sqlite3.Connection,
    response: dict[str, Any],
    *,
    source: str,
    started_at: str | None = None,
    returned_count: int | None = None,
    request_count: int = 1,
    poll_status: str = "success",
    advance_conversation_tail_cursor: bool = True,
    conversation_tail_cursor_at: str | None = None,
    dependencies: Dependencies,
) -> dict[str, Any]:
    started = started_at or isoformat()
    observed_at = isoformat()
    measured_returned_count = (
        len(response.get("data", []) or [])
        if returned_count is None
        else returned_count
    )
    if measured_returned_count < 0 or request_count < 0:
        raise ValueError("Poll resource counters must not be negative")
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
        ingested = _insert_response_events(
            config,
            connection,
            response,
            source=source,
            observed_at=observed_at,
        )
        _update_poll_metadata(
            config,
            connection,
            response,
            ingested,
            source=source,
            started_at=started,
            observed_at=observed_at,
            returned_count=measured_returned_count,
            request_count=request_count,
            poll_status=poll_status,
            live_bootstrap=live_bootstrap,
            advance_conversation_tail_cursor=advance_conversation_tail_cursor,
            conversation_tail_cursor_at=conversation_tail_cursor_at,
        )

    health = dependencies.write_health(
        config,
        connection,
        last_new_count=len(ingested.new_ids),
    )
    wake = refresh_wake_file(config, connection)
    if ingested.new_ids:
        dependencies.notify_macos(
            config,
            "Новые ответы в X",
            f"В очереди {wake['pending_count']}, новых {len(ingested.new_ids)}.",
        )
    return {
        "source": source,
        "bootstrap": live_bootstrap,
        "observed_count": len(ingested.observed_ids),
        "new_count": len(ingested.new_ids),
        "returned_count": measured_returned_count,
        "request_count": request_count,
        "new_event_ids": sorted(ingested.new_ids, key=int),
        "self_authored_event_ids": sorted(
            set(reclassified_self_authored_ids + ingested.self_authored_ids),
            key=int,
        ),
        "since_id": get_meta(connection, "since_id"),
        "pending_count": wake["pending_count"],
        "health": health["status"],
    }

def baseline_existing_queue(
    config: Any,
    connection: sqlite3.Connection,
    *,
    dependencies: Dependencies,
) -> dict[str, Any]:
    with connection:
        cursor = connection.execute(
            "UPDATE events SET delivery_state = 'baseline' "
            "WHERE delivery_state = 'queued'"
        )
    wake = refresh_wake_file(config, connection)
    health = dependencies.write_health(config, connection, last_new_count=0)
    return {
        "baselined_count": int(cursor.rowcount),
        "pending_count": wake["pending_count"],
        "since_id": get_meta(connection, "since_id"),
        "health": health["status"],
    }
