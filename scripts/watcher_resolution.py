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
    deleted_publication_replacement,
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


@dataclass(frozen=True)
class ResolutionInput:
    disposition: str
    reason: str
    reply_url: str | None
    blocker_code: str | None
    stance: str | None
    stance_detail: str | None
    confidence: str | None
    media_meaning: str | None
    evidence: tuple[str, ...]

    @property
    def evidence_json(self) -> str:
        return json.dumps(list(self.evidence), ensure_ascii=False)

    def expected_row(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition,
            "reason": self.reason,
            "reply_url": self.reply_url,
            "blocker_code": self.blocker_code,
            "stance": self.stance,
            "stance_detail": self.stance_detail,
            "confidence": self.confidence,
            "media_meaning": self.media_meaning,
            "evidence_json": self.evidence_json,
        }


def _clean_optional(value: str | None) -> str | None:
    return value.strip() if value else None


def _normalize_resolution(
    *,
    disposition: str,
    reason: str,
    reply_url: str | None,
    blocker_code: str | None,
    stance: str | None,
    stance_detail: str | None,
    confidence: str | None,
    media_meaning: str | None,
    evidence: list[str] | None,
) -> ResolutionInput:
    if disposition not in {"published", "skip", "blocked"}:
        raise ValueError("Disposition must be published, skip, or blocked")
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError("Resolution reason must not be empty")
    clean_stance = _clean_optional(stance)
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
    clean_confidence = _clean_optional(confidence)
    if clean_confidence not in {None, "high", "medium", "low"}:
        raise ValueError("Confidence must be high, medium, or low")
    clean_reply_url = _clean_optional(reply_url)
    if disposition == "published" and not clean_reply_url:
        raise ValueError("Published resolution requires reply_url")
    if disposition in {"skip", "blocked"} and clean_reply_url:
        raise ValueError(
            "Skip and blocked resolutions must not include reply_url"
        )
    return ResolutionInput(
        disposition=disposition,
        reason=clean_reason,
        reply_url=clean_reply_url,
        blocker_code=_clean_optional(blocker_code),
        stance=clean_stance,
        stance_detail=_clean_optional(stance_detail),
        confidence=clean_confidence,
        media_meaning=_clean_optional(media_meaning),
        evidence=tuple(
            sorted(
                {
                    str(item).strip()
                    for item in (evidence or [])
                    if str(item).strip()
                }
            )
        ),
    )


def _require_event(
    connection: sqlite3.Connection,
    event_id: str,
) -> sqlite3.Row:
    event = connection.execute(
        "SELECT * FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if event is None:
        raise KeyError(f"Unknown event {event_id}")
    return event


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


def _require_event_history(
    connection: sqlite3.Connection,
    event_id: str,
    *,
    action: str,
) -> None:
    history_turn = connection.execute(
        "SELECT 1 FROM conversation_turns WHERE status_id = ?",
        (event_id,),
    ).fetchone()
    if history_turn is None:
        if action == "resolving":
            raise ValueError(
                "Import the exact inspected event turn into conversation "
                f"history before resolving {event_id}"
            )
        raise ValueError(
            f"Import the exact inspected event turn before revising {event_id}"
        )


def _validate_resolution_proof(
    config: Any,
    connection: sqlite3.Connection,
    *,
    event: sqlite3.Row,
    event_id: str,
    resolution: ResolutionInput,
    action: str,
) -> None:
    _enforce_mandatory_response_resolution(
        config,
        connection,
        event_id=event_id,
        disposition=resolution.disposition,
        blocker_code=resolution.blocker_code,
    )
    _require_event_history(connection, event_id, action=action)
    if resolution.disposition == "published":
        _require_matching_published_alex_turn(
            connection,
            event=event,
            event_id=event_id,
            reply_url=resolution.reply_url,
        )


def _acknowledge_event(
    connection: sqlite3.Connection,
    event_id: str,
) -> None:
    connection.execute(
        "UPDATE events SET delivery_state = 'acknowledged' WHERE event_id = ?",
        (event_id,),
    )


def _refresh_resolution_runtime(
    config: Any,
    connection: sqlite3.Connection,
    dependencies: Dependencies,
) -> tuple[dict[str, Any], dict[str, Any]]:
    wake = dependencies.refresh_wake_file(config, connection)
    health = dependencies.write_health(config, connection, last_new_count=0)
    return wake, health


def _insert_event_resolution(
    connection: sqlite3.Connection,
    event_id: str,
    resolution: ResolutionInput,
    resolved_at: str,
) -> None:
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
            resolution.disposition,
            resolution.reason,
            resolution.reply_url,
            resolution.blocker_code,
            resolution.stance,
            resolution.stance_detail,
            resolution.confidence,
            resolution.media_meaning,
            resolution.evidence_json,
            resolved_at,
        ),
    )


def _persist_initial_resolution(
    connection: sqlite3.Connection,
    event_id: str,
    resolution: ResolutionInput,
    existing: sqlite3.Row | None,
) -> None:
    with connection:
        if existing is None:
            _insert_event_resolution(
                connection,
                event_id,
                resolution,
                isoformat(),
            )
        elif (
            existing["stance_detail"] is None
            and resolution.stance_detail is not None
        ):
            connection.execute(
                """
                UPDATE event_resolutions
                SET stance_detail = ?
                WHERE event_id = ?
                """,
                (resolution.stance_detail, event_id),
            )
        _acknowledge_event(connection, event_id)


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
    event = _require_event(connection, event_id)
    resolution = _normalize_resolution(
        disposition=disposition,
        reason=reason,
        reply_url=reply_url,
        blocker_code=blocker_code,
        stance=stance,
        stance_detail=stance_detail,
        confidence=confidence,
        media_meaning=media_meaning,
        evidence=evidence,
    )
    _validate_resolution_proof(
        config,
        connection,
        event=event,
        event_id=event_id,
        resolution=resolution,
        action="resolving",
    )
    existing = connection.execute(
        "SELECT * FROM event_resolutions WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if existing is not None:
        _assert_existing_values(
            existing,
            resolution.expected_row(),
            resource=f"event resolution {event_id}",
        )
    _persist_initial_resolution(connection, event_id, resolution, existing)
    wake, health = _refresh_resolution_runtime(
        config,
        connection,
        dependencies,
    )
    return {
        "event_id": event_id,
        "disposition": resolution.disposition,
        "reason": resolution.reason,
        "reply_url": resolution.reply_url,
        "blocker_code": resolution.blocker_code,
        "stance": resolution.stance,
        "stance_detail": resolution.stance_detail,
        "confidence": resolution.confidence,
        "media_meaning": resolution.media_meaning,
        "evidence": list(resolution.evidence),
        "pending_count": wake["pending_count"],
        "health": health["status"],
    }


def _resolution_matches(
    existing: sqlite3.Row,
    resolution: ResolutionInput,
) -> bool:
    return all(
        existing[name] == value
        for name, value in resolution.expected_row().items()
    )


def _validate_revision_transition(
    config: Any,
    connection: sqlite3.Connection,
    event_id: str,
    existing: sqlite3.Row,
    resolution: ResolutionInput,
) -> None:
    allowed_transitions = {
        ("skip", "skip"),
        ("skip", "published"),
        ("skip", "blocked"),
        ("blocked", "blocked"),
        ("blocked", "published"),
        ("blocked", "skip"),
    }
    transition = (existing["disposition"], resolution.disposition)
    if transition == ("published", "published"):
        deleted_publication_replacement.require_active_revision_authorization(
            connection,
            event_id=event_id,
            existing=existing,
            replacement_reply_url=resolution.reply_url,
        )
        return
    if transition == ("skip", "skip") and not config.mandatory_response_mode:
        raise ValueError(
            "Skip to skip revision requires mandatory response mode"
        )
    if transition not in allowed_transitions:
        raise ValueError(
            "Unsupported resolution revision transition "
            f"{existing['disposition']} to {resolution.disposition}"
        )


def _revision_replacement(
    resolution: ResolutionInput,
    revised_at: str,
) -> dict[str, Any]:
    return {
        "disposition": resolution.disposition,
        "reason": resolution.reason,
        "reply_url": resolution.reply_url,
        "blocker_code": resolution.blocker_code,
        "stance": resolution.stance,
        "stance_detail": resolution.stance_detail,
        "confidence": resolution.confidence,
        "media_meaning": resolution.media_meaning,
        "evidence": list(resolution.evidence),
        "resolved_at": revised_at,
    }


def _insert_resolution_revision(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    existing: sqlite3.Row,
    replacement: dict[str, Any],
    revision_reason: str,
    revised_at: str,
) -> None:
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
            json.dumps(replacement, ensure_ascii=False, sort_keys=True),
            revision_reason,
            revised_at,
        ),
    )


def _update_event_resolution(
    connection: sqlite3.Connection,
    event_id: str,
    resolution: ResolutionInput,
    revised_at: str,
) -> None:
    connection.execute(
        """
        UPDATE event_resolutions
        SET disposition = ?, reason = ?, reply_url = ?, blocker_code = ?,
            stance = ?, stance_detail = ?, confidence = ?, media_meaning = ?,
            evidence_json = ?, resolved_at = ?
        WHERE event_id = ?
        """,
        (
            resolution.disposition,
            resolution.reason,
            resolution.reply_url,
            resolution.blocker_code,
            resolution.stance,
            resolution.stance_detail,
            resolution.confidence,
            resolution.media_meaning,
            resolution.evidence_json,
            revised_at,
            event_id,
        ),
    )


def _persist_resolution_revision(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    existing: sqlite3.Row,
    resolution: ResolutionInput,
    revision_reason: str,
) -> None:
    revised_at = isoformat()
    replacement = _revision_replacement(resolution, revised_at)
    with connection:
        _insert_resolution_revision(
            connection,
            event_id=event_id,
            existing=existing,
            replacement=replacement,
            revision_reason=revision_reason,
            revised_at=revised_at,
        )
        _update_event_resolution(
            connection,
            event_id,
            resolution,
            revised_at,
        )
        _acknowledge_event(connection, event_id)


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
    event = _require_event(connection, event_id)
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
    resolution = _normalize_resolution(
        disposition=disposition,
        reason=reason,
        reply_url=reply_url,
        blocker_code=blocker_code,
        stance=stance,
        stance_detail=stance_detail,
        confidence=confidence,
        media_meaning=media_meaning,
        evidence=evidence,
    )
    _enforce_mandatory_response_resolution(
        config,
        connection,
        event_id=event_id,
        disposition=resolution.disposition,
        blocker_code=resolution.blocker_code,
    )
    if _resolution_matches(existing, resolution):
        wake, health = _refresh_resolution_runtime(
            config,
            connection,
            dependencies,
        )
        return {
            "event_id": event_id,
            "revised": False,
            "disposition": resolution.disposition,
            "reply_url": resolution.reply_url,
            "pending_count": wake["pending_count"],
            "health": health["status"],
        }
    _validate_revision_transition(
        config,
        connection,
        event_id,
        existing,
        resolution,
    )
    _require_event_history(
        connection,
        event_id,
        action="revising",
    )
    if resolution.disposition == "published":
        _require_matching_published_alex_turn(
            connection,
            event=event,
            event_id=event_id,
            reply_url=resolution.reply_url,
        )
    _persist_resolution_revision(
        connection,
        event_id=event_id,
        existing=existing,
        resolution=resolution,
        revision_reason=clean_revision_reason,
    )
    wake, health = _refresh_resolution_runtime(
        config,
        connection,
        dependencies,
    )
    return {
        "event_id": event_id,
        "revised": True,
        "disposition": resolution.disposition,
        "reply_url": resolution.reply_url,
        "blocker_code": resolution.blocker_code,
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
