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


@dataclass(frozen=True)
class InitialAuditCounts:
    direct_total: int
    unresolved: int
    queued: int
    resolved: int
    history_missing: int
    published: int
    blocked: int
    blocked_missing_code: int
    published_alex_history_missing: int


def _count_query(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[str, ...],
) -> int:
    return int(connection.execute(query, parameters).fetchone()["count"])


def _audit_meta(
    connection: sqlite3.Connection,
    dependencies: Dependencies,
) -> dict[str, str | None]:
    return {
        "started_at": dependencies.get_meta(
            connection,
            "initial_audit_started_at",
        ),
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
        "completed_at": dependencies.get_meta(
            connection,
            "initial_audit_completed_at",
        ),
    }


def _direct_event_count(
    connection: sqlite3.Connection,
    configured_user_id: str,
) -> int:
    return _count_query(
        connection,
        """
        SELECT COUNT(*) AS count
        FROM events e
        WHERE e.is_reply = 1
          AND e.in_reply_to_user_id = ?
          AND COALESCE(e.author_id, '') != ?
        """,
        (configured_user_id, configured_user_id),
    )


def _unresolved_event_count(
    connection: sqlite3.Connection,
    configured_user_id: str,
) -> int:
    return _count_query(
        connection,
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
    )


def _queued_event_count(
    connection: sqlite3.Connection,
    configured_user_id: str,
) -> int:
    return _count_query(
        connection,
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
    )


def _resolved_event_count(
    connection: sqlite3.Connection,
    configured_user_id: str,
) -> int:
    return _count_query(
        connection,
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
    )


def _history_missing_count(
    connection: sqlite3.Connection,
    configured_user_id: str,
) -> int:
    return _count_query(
        connection,
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
    )


def _published_resolution_count(
    connection: sqlite3.Connection,
    configured_user_id: str,
) -> int:
    return _count_query(
        connection,
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
    )


def _published_alex_history_missing_count(
    connection: sqlite3.Connection,
    configured_user_id: str,
) -> int:
    return _count_query(
        connection,
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
              r.reply_url NOT LIKE 'https://x.com/axrbarsic/status/%'
              OR alex.status_id IS NULL
          )
        """,
        (configured_user_id, configured_user_id),
    )


def _initial_audit_counts(
    connection: sqlite3.Connection,
    configured_user_id: str,
) -> InitialAuditCounts:
    blocked, blocked_missing_code = _blocked_resolution_contract_status(
        connection,
        configured_user_id,
    )
    return InitialAuditCounts(
        direct_total=_direct_event_count(connection, configured_user_id),
        unresolved=_unresolved_event_count(connection, configured_user_id),
        queued=_queued_event_count(connection, configured_user_id),
        resolved=_resolved_event_count(connection, configured_user_id),
        history_missing=_history_missing_count(
            connection,
            configured_user_id,
        ),
        published=_published_resolution_count(
            connection,
            configured_user_id,
        ),
        blocked=blocked,
        blocked_missing_code=blocked_missing_code,
        published_alex_history_missing=(
            _published_alex_history_missing_count(
                connection,
                configured_user_id,
            )
        ),
    )


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
    meta = _audit_meta(connection, dependencies)
    configured_user_id = dependencies.get_meta(connection, "configured_user_id")
    if configured_user_id is None:
        return {
            **meta,
            "complete": meta["completed_at"] is not None,
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
    counts = _initial_audit_counts(connection, configured_user_id)
    history_complete = (
        counts.history_missing == 0
        and counts.published_alex_history_missing == 0
        and counts.blocked_missing_code == 0
    )
    invariant_ok = (
        counts.direct_total == counts.unresolved + counts.resolved
        and counts.blocked_missing_code == 0
    )
    return {
        **meta,
        "complete": (
            meta["completed_at"] is not None
            and counts.unresolved == 0
            and history_complete
            and invariant_ok
        ),
        "direct_events": counts.direct_total,
        "pending_events": counts.unresolved,
        "queued_events": counts.queued,
        "resolved_events": counts.resolved,
        "history_events": counts.resolved - counts.history_missing,
        "history_missing_events": counts.history_missing,
        "published_resolutions": counts.published,
        "blocked_resolutions": counts.blocked,
        "blocked_resolutions_missing_code": counts.blocked_missing_code,
        "blocked_contract_complete": counts.blocked_missing_code == 0,
        "published_alex_history_missing": (
            counts.published_alex_history_missing
        ),
        "published_alex_history_complete": (
            counts.published_alex_history_missing == 0
        ),
        "history_complete": history_complete,
        "invariant_ok": invariant_ok,
    }


def _queued_conversation_groups(
    connection: sqlite3.Connection,
    configured_user_id: str,
    conversation_limit: int,
) -> list[sqlite3.Row]:
    return connection.execute(
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


def _queued_conversation_events(
    connection: sqlite3.Connection,
    configured_user_id: str,
    conversation_key: str,
) -> list[sqlite3.Row]:
    return connection.execute(
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


def _audit_event_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(str(row["payload_json"]))
    event_id = str(row["event_id"])
    username = row["username"]
    references = payload.get("referenced_tweets") or []
    replied_to_status_id = next(
        (
            str(reference["id"])
            for reference in references
            if reference.get("type") == "replied_to" and reference.get("id")
        ),
        None,
    )
    exact_text = str(payload.get("text") or "")
    attachments = payload.get("attachments") or {}
    return {
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
        "text_urls": re.findall(r"https?://[^\s]+", exact_text),
        "media_keys": attachments.get("media_keys") or [],
        "included_media": payload.get("included_media") or [],
    }


def _audit_group_payload(
    connection: sqlite3.Connection,
    configured_user_id: str,
    group: sqlite3.Row,
) -> dict[str, Any]:
    conversation_key = str(group["conversation_key"])
    rows = _queued_conversation_events(
        connection,
        configured_user_id,
        conversation_key,
    )
    return {
        "conversation_id": conversation_key,
        "latest_created_at": group["latest_created_at"],
        "queued_count": int(group["queued_count"]),
        "events": [_audit_event_payload(row) for row in rows],
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
    groups = _queued_conversation_groups(
        connection,
        configured_user_id,
        conversation_limit,
    )
    result_groups = [
        _audit_group_payload(connection, configured_user_id, group)
        for group in groups
    ]
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


def _expiry_event_payload(
    event: sqlite3.Row,
    event_id: str,
) -> dict[str, Any]:
    try:
        payload = json.loads(str(event["payload_json"]))
    except json.JSONDecodeError as error:
        raise ValueError("payload_json must be valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("payload_json must contain an object")
    if str(payload.get("id") or "") != event_id:
        raise ValueError("payload_json id must match event_id")
    if not isinstance(payload.get("text"), str):
        raise ValueError("payload_json text must be a string")
    return payload


def _expiry_parent_status_id(payload: dict[str, Any]) -> str:
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
    return _validate_status_id(parent_ids[0], "parent_status_id")


def _expiry_media(payload: dict[str, Any]) -> list[dict[str, Any]]:
    attachments = payload.get("attachments") or {}
    if not isinstance(attachments, dict):
        raise ValueError("payload_json attachments must be an object")
    media_keys = attachments.get("media_keys") or []
    if not isinstance(media_keys, list):
        raise ValueError("payload_json media_keys must be an array")
    included_media = payload.get("included_media", [])
    if not isinstance(included_media, list):
        raise ValueError("payload_json included_media must be an array")
    requested = [str(media_key).strip() for media_key in media_keys]
    if any(not media_key for media_key in requested):
        raise ValueError("payload_json media_keys must not contain empty IDs")
    expanded: set[str] = set()
    for media_item in included_media:
        if not isinstance(media_item, dict):
            raise ValueError(
                "payload_json included_media items must be objects"
            )
        media_key = str(media_item.get("media_key") or "").strip()
        if not media_key:
            raise ValueError(
                "payload_json included_media items require media_key"
            )
        expanded.add(media_key)
    missing = sorted(set(requested) - expanded)
    if missing:
        raise ValueError(
            "media attachment metadata is incomplete for exact history: "
            + ", ".join(missing)
        )
    return included_media


def _expiry_chain_provenance(
    connection: sqlite3.Connection,
    conversation_id: str,
) -> str:
    row = connection.execute(
        "SELECT provenance FROM conversation_chains WHERE chain_id = ?",
        (conversation_id,),
    ).fetchone()
    return str(row["provenance"]) if row is not None else "short"


def _expiry_history_snapshot(
    connection: sqlite3.Connection,
    event: sqlite3.Row,
) -> dict[str, Any]:
    event_id = _validate_status_id(str(event["event_id"]), "event_id")
    conversation_id = _validate_status_id(
        str(event["conversation_id"] or ""),
        "conversation_id",
    )
    payload = _expiry_event_payload(event, event_id)
    parent_status_id = _expiry_parent_status_id(payload)
    included_media = _expiry_media(payload)
    username = str(event["username"] or "").strip()
    event_url = (
        f"https://x.com/{username}/status/{event_id}"
        if username
        else f"https://x.com/i/web/status/{event_id}"
    )
    return {
        "chain_id": conversation_id,
        "root_status_id": conversation_id,
        "provenance": _expiry_chain_provenance(connection, conversation_id),
        "turns": [
            {
                "status_id": event_id,
                "parent_status_id": parent_status_id,
                "actor": "user",
                "author": username or None,
                "url": event_url,
                "exact_text": payload["text"],
                "media": included_media,
                "posted_at": str(event["created_at"]),
                "provenance": INITIAL_AUDIT_EXPIRY_PROVENANCE,
            }
        ],
    }
