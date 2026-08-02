#!/usr/bin/env python3
"""Validated, append-only event resolution for the X watcher."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from scripts import (
    watcher_chatgpt,
    watcher_constants,
    watcher_time,
    watcher_validation,
)


TERMINAL_BLOCKER_CODES = watcher_constants.TERMINAL_BLOCKER_CODES
SCHEMA_VERSION = watcher_constants.SCHEMA_VERSION
isoformat = watcher_time.isoformat
_validate_status_id = watcher_validation.validate_status_id
_assert_existing_values = watcher_validation.assert_existing_values
_is_exact_chatgpt_conversation_url = watcher_chatgpt.is_exact_conversation_url
_chatgpt_custom_gpt_scope = watcher_chatgpt.custom_gpt_scope


@dataclass(frozen=True)
class Dependencies:
    refresh_wake_file: Callable[..., dict[str, Any]]
    write_health: Callable[..., dict[str, Any]]

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


def _replied_to_status_id(event: sqlite3.Row) -> str:
    try:
        payload = json.loads(str(event["payload_json"]))
    except json.JSONDecodeError as error:
        raise ValueError("payload_json must be valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("payload_json must contain an object")
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


_is_exact_chatgpt_conversation_url = watcher_chatgpt.is_exact_conversation_url
_chatgpt_custom_gpt_scope = watcher_chatgpt.custom_gpt_scope


def _require_required_pro_model_unavailable_proof(
    connection: sqlite3.Connection,
    event_id: str,
) -> None:
    event = connection.execute(
        "SELECT * FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if event is None:
        raise KeyError(f"Unknown event {event_id}")
    conversation_id = str(event["conversation_id"] or event_id)
    chain = connection.execute(
        "SELECT * FROM conversation_chains WHERE chain_id = ?",
        (conversation_id,),
    ).fetchone()
    if (
        chain is None
        or chain["provenance"] not in {"pro", "mixed"}
        or not _is_exact_chatgpt_conversation_url(
            chain["chatgpt_conversation_url"]
        )
    ):
        raise ValueError(
            "required_pro_model_unavailable requires the exact ChatGPT "
            "conversation URL in the Pro chain"
        )
    parent_status_id = _replied_to_status_id(event)
    parent = connection.execute(
        "SELECT * FROM conversation_turns WHERE status_id = ?",
        (parent_status_id,),
    ).fetchone()
    expected_parent_url = (
        f"https://x.com/axrbarsic/status/{parent_status_id}"
    )
    if (
        parent is None
        or parent["chain_id"] != conversation_id
        or parent["actor"] != "alex"
        or parent["url"] != expected_parent_url
        or parent["provenance"] != "pro"
    ):
        raise ValueError(
            "required_pro_model_unavailable requires the exact imported "
            "Pro-generated Alex parent"
        )


def _enforce_mandatory_response_resolution(
    config: Any,
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
    if (
        disposition == "blocked"
        and blocker_code == "required_pro_model_unavailable"
    ):
        _require_required_pro_model_unavailable_proof(
            connection,
            event_id,
        )
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
    config: Any,
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
    dependencies: Dependencies,
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
    wake = dependencies.refresh_wake_file(config, connection)
    health = dependencies.write_health(config, connection, last_new_count=0)
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
    config: Any,
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
    dependencies: Dependencies,
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
        wake = dependencies.refresh_wake_file(config, connection)
        health = dependencies.write_health(config, connection, last_new_count=0)
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
    wake = dependencies.refresh_wake_file(config, connection)
    health = dependencies.write_health(config, connection, last_new_count=0)
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
    *,
    initial_audit_status: Callable[[sqlite3.Connection], dict[str, Any]],
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
