#!/usr/bin/env python3
"""Read-only initial-audit state and selection queries."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from scripts import watcher_constants, watcher_validation


TERMINAL_BLOCKER_CODES = watcher_constants.TERMINAL_BLOCKER_CODES
INITIAL_AUDIT_EXPIRY_PROVENANCE = (
    watcher_constants.INITIAL_AUDIT_EXPIRY_PROVENANCE
)
_validate_status_id = watcher_validation.validate_status_id


@dataclass(frozen=True)
class Dependencies:
    get_meta: Callable[[sqlite3.Connection, str], str | None]

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


def initial_audit_status(
    connection: sqlite3.Connection,
    *,
    dependencies: Dependencies,
) -> dict[str, Any]:
    configured_user_id = dependencies.get_meta(connection, "configured_user_id")
    if configured_user_id is None:
        return {
            "started_at": dependencies.get_meta(connection, "initial_audit_started_at"),
            "cycle_started_at": dependencies.get_meta(
                connection,
                "initial_audit_cycle_started_at",
            ),
            "cycle_expiry_as_of": dependencies.get_meta(
                connection,
                "initial_audit_cycle_expiry_as_of",
            ),
            "cycle_expiry_hours": dependencies.get_meta(
                connection,
                "initial_audit_cycle_expiry_hours",
            ),
            "completed_at": dependencies.get_meta(connection, "initial_audit_completed_at"),
            "complete": dependencies.get_meta(connection, "initial_audit_completed_at") is not None,
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
    completed_at = dependencies.get_meta(connection, "initial_audit_completed_at")
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
        "started_at": dependencies.get_meta(connection, "initial_audit_started_at"),
        "cycle_started_at": dependencies.get_meta(
            connection,
            "initial_audit_cycle_started_at",
        ),
        "cycle_expiry_as_of": dependencies.get_meta(
            connection,
            "initial_audit_cycle_expiry_as_of",
        ),
        "cycle_expiry_hours": dependencies.get_meta(
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
    dependencies: Dependencies,
) -> dict[str, Any]:
    if conversation_limit <= 0:
        raise ValueError("conversation_limit must be positive")
    configured_user_id = dependencies.get_meta(connection, "configured_user_id")
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
    status = initial_audit_status(
        connection,
        dependencies=dependencies,
    )
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
