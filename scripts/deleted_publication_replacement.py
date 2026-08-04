#!/usr/bin/env python3
"""Auditably requeue an X reply after its exact publication was deleted."""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from scripts import watcher_time


REQUEUE_REASON = "operator_deleted_publication_replacement"
NOT_FOUND_TYPE = "https://api.x.com/2/problems/resource-not-found"
_STATUS_URL = re.compile(
    r"^https://(?:www\.)?x\.com/(?:i/web/status|[^/?#]+/status)/(\d+)(?:[?#].*)?$"
)


@dataclass(frozen=True)
class Dependencies:
    bearer_token: Callable[[Any], str]
    request_json: Callable[[str, str, int], dict[str, Any]]
    refresh_wake_file: Callable[..., dict[str, Any]]
    write_health: Callable[..., dict[str, Any]]
    isoformat: Callable[[], str] = watcher_time.isoformat


def status_id_from_url(url: str) -> str:
    match = _STATUS_URL.fullmatch(url.strip())
    if match is None:
        raise ValueError("Published reply URL is not a canonical X status URL")
    return match.group(1)


def _deletion_proof(response: dict[str, Any], status_id: str) -> dict[str, str]:
    if isinstance(response.get("data"), dict):
        raise ValueError("Published reply still exists according to the X API")
    errors = response.get("errors")
    if not isinstance(errors, list):
        raise ValueError("X API did not provide deletion proof")
    for error in errors:
        if not isinstance(error, dict):
            continue
        if (
            error.get("type") == NOT_FOUND_TYPE
            and error.get("resource_type") == "tweet"
            and str(error.get("parameter")) == "id"
            and str(error.get("value")) == status_id
        ):
            return {
                "title": str(error.get("title") or ""),
                "detail": str(error.get("detail") or ""),
                "type": str(error["type"]),
                "resource_type": "tweet",
                "parameter": "id",
                "value": status_id,
            }
    raise ValueError("X API response is not exact resource-not-found proof")


def _require_recorded_alex_turn(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    status_id: str,
    reply_url: str,
) -> None:
    turn = connection.execute(
        """
        SELECT 1
        FROM conversation_turns
        WHERE status_id = ?
          AND parent_status_id = ?
          AND actor = 'alex'
          AND url = ?
        """,
        (status_id, event_id, reply_url),
    ).fetchone()
    if turn is None:
        raise ValueError(
            "Published reply lacks an exact durable Alex conversation turn"
        )


def _resolution_payload(row: sqlite3.Row) -> dict[str, Any]:
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


def _active_authorization(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    resolved_at: str,
    status_id: str,
) -> bool:
    row = connection.execute(
        """
        SELECT previous_resolution_json, requeued_at
        FROM response_policy_requeues
        WHERE event_id = ? AND reason = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (event_id, REQUEUE_REASON),
    ).fetchone()
    if row is None or str(row["requeued_at"]) <= resolved_at:
        return False
    try:
        payload = json.loads(str(row["previous_resolution_json"]))
    except (TypeError, ValueError):
        return False
    authorization = payload.get("deleted_publication_replacement")
    return (
        isinstance(authorization, dict)
        and str(authorization.get("old_status_id")) == status_id
    )


def require_active_revision_authorization(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    existing: sqlite3.Row,
    replacement_reply_url: str | None,
) -> None:
    old_reply_url = str(existing["reply_url"] or "")
    if not old_reply_url or not replacement_reply_url:
        raise ValueError("Deleted publication replacement requires both URLs")
    if old_reply_url == replacement_reply_url:
        raise ValueError("Deleted publication replacement URL must be new")
    old_status_id = status_id_from_url(old_reply_url)
    if not _active_authorization(
        connection,
        event_id=event_id,
        resolved_at=str(existing["resolved_at"]),
        status_id=old_status_id,
    ):
        raise ValueError(
            "Published to published revision lacks active deletion authorization"
        )


def prepare_deleted_publication_replacement(
    config: Any,
    connection: sqlite3.Connection,
    event_id: str,
    *,
    reason: str,
    dependencies: Dependencies,
) -> dict[str, Any]:
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError("Replacement reason must not be empty")
    row = connection.execute(
        """
        SELECT e.delivery_state, r.*
        FROM events e
        JOIN event_resolutions r ON r.event_id = e.event_id
        WHERE e.event_id = ?
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Event {event_id} has no durable resolution")
    if row["disposition"] != "published" or not row["reply_url"]:
        raise ValueError("Only a published resolution can be replaced")
    reply_url = str(row["reply_url"])
    status_id = status_id_from_url(reply_url)
    _require_recorded_alex_turn(
        connection,
        event_id=event_id,
        status_id=status_id,
        reply_url=reply_url,
    )

    params = urllib.parse.urlencode(
        {"tweet.fields": "author_id,conversation_id,referenced_tweets"}
    )
    response = dependencies.request_json(
        f"{config.api_base}/tweets/{status_id}?{params}",
        dependencies.bearer_token(config),
        config.request_timeout_seconds,
    )
    proof = _deletion_proof(response, status_id)
    if _active_authorization(
        connection,
        event_id=event_id,
        resolved_at=str(row["resolved_at"]),
        status_id=status_id,
    ):
        wake = dependencies.refresh_wake_file(config, connection)
        return {
            "event_id": event_id,
            "prepared": False,
            "reason": "replacement_already_queued",
            "old_reply_url": reply_url,
            "old_status_id": status_id,
            "pending_count": wake["pending_count"],
        }

    checked_at = dependencies.isoformat()
    previous = _resolution_payload(row)
    previous["deleted_publication_replacement"] = {
        "operator_reason": clean_reason,
        "old_reply_url": reply_url,
        "old_status_id": status_id,
        "checked_at": checked_at,
        "api_proof": proof,
    }
    with connection:
        connection.execute(
            """
            INSERT INTO response_policy_requeues(
                event_id, previous_resolution_json, reason, requeued_at
            ) VALUES(?, ?, ?, ?)
            """,
            (
                event_id,
                json.dumps(previous, ensure_ascii=False, sort_keys=True),
                REQUEUE_REASON,
                checked_at,
            ),
        )
        connection.execute(
            "UPDATE events SET delivery_state = 'queued' WHERE event_id = ?",
            (event_id,),
        )
    wake = dependencies.refresh_wake_file(config, connection)
    health = dependencies.write_health(config, connection, last_new_count=0)
    return {
        "event_id": event_id,
        "prepared": True,
        "old_reply_url": reply_url,
        "old_status_id": status_id,
        "api_proof": proof,
        "requeued_at": checked_at,
        "pending_count": wake["pending_count"],
        "health": health["status"],
    }
