#!/usr/bin/env python3
"""Validate and apply durable Browser-owner handoffs.

The watcher keeps its public compatibility facade, while this module owns the
ordered transaction that imports exact history and resolves matching events.
All watcher-specific operations are supplied explicitly so this module cannot
silently reach into mutable global state.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import evidence_import


@dataclass(frozen=True)
class Dependencies:
    import_history_file: Callable[..., dict[str, Any]]
    history_records: Callable[[Path], list[dict[str, Any]]]
    validate_status_id: Callable[[str, str], str]
    required_text: Callable[..., str]
    optional_text: Callable[..., str | None]
    is_eligible_reply: Callable[..., bool]
    event_mentions_configured_account: Callable[..., bool]
    require_matching_published_alex_turn: Callable[..., Any]
    matching_alex_reply_turns: Callable[..., list[sqlite3.Row]]
    enforce_mandatory_response_resolution: Callable[..., None]
    revise_event_resolution: Callable[..., dict[str, Any]]
    resolve_event: Callable[..., dict[str, Any]]
    initial_audit_status: Callable[..., dict[str, Any]]


def _evidence_context(
    config: Any,
    history_path: Path,
    ledger_path: Path,
) -> tuple[Path, Path, Path]:
    history_parent = history_path.expanduser().resolve().parent
    ledger_parent = ledger_path.expanduser().resolve().parent
    canonical_root = (
        config.source_path.parent / "var/evidence/browser-owner"
    ).resolve()
    history_is_canonical = history_parent.parent == canonical_root
    ledger_is_canonical = ledger_parent.parent == canonical_root
    if (history_is_canonical or ledger_is_canonical) and not (
        history_is_canonical
        and ledger_is_canonical
        and history_parent == ledger_parent
    ):
        raise ValueError(
            "Canonical Browser handoff files must share one evidence directory"
        )
    return history_parent, ledger_parent, canonical_root


def _handoff_signature(record: dict[str, Any]) -> str:
    evidence = record.get("evidence", [])
    if isinstance(evidence, list):
        normalized_evidence: Any = sorted(
            str(item).strip() for item in evidence if str(item).strip()
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


def _select_candidates(
    connection: sqlite3.Connection,
    records: list[dict[str, Any]],
    dependencies: Dependencies,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    already_resolved: list[str] = []
    for record in records:
        if record.get("event") != "initial_audit_disposition":
            continue
        watcher_disposition = str(record.get("watcher_disposition") or "")
        if not watcher_disposition.endswith("_pending_root_resolve"):
            continue
        event_id = dependencies.validate_status_id(
            dependencies.required_text(record, "event_id"),
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
        groups.setdefault(event_id, []).append(record)

    candidates: dict[str, dict[str, Any]] = {}
    for event_id, group in groups.items():
        signatures = {_handoff_signature(record) for record in group}
        latest = group[-1]
        if (
            len(signatures) > 1
            and latest.get("supersedes_invalid_handoff") is not True
        ):
            raise ValueError(
                f"Conflicting Browser handoffs for unresolved event {event_id}"
            )
        candidates[event_id] = latest
    return candidates, already_resolved


def _validate_route(
    config: Any,
    connection: sqlite3.Connection,
    *,
    event_id: str,
    event: sqlite3.Row,
    record: dict[str, Any],
    dependencies: Dependencies,
) -> None:
    confirmed_direct = record.get("direct_reply_to_axrbarsic") is True
    confirmed_tracked = record.get("tracked_conversation_reply") is True
    confirmed_mention = record.get("mention_reply_to_axrbarsic") is True
    if sum((confirmed_direct, confirmed_tracked, confirmed_mention)) != 1:
        raise ValueError(
            f"Browser handoff {event_id} must confirm exactly one route"
        )
    event_is_self_authored = event["author_id"] == config.user_id
    event_is_direct = (
        not event_is_self_authored
        and int(event["is_reply"]) == 1
        and event["in_reply_to_user_id"] == config.user_id
    )
    event_is_tracked = dependencies.is_eligible_reply(
        connection,
        config,
        author_id=event["author_id"],
        is_reply=bool(event["is_reply"]),
        in_reply_to_user_id=None,
        conversation_id=event["conversation_id"],
    )
    event_is_mention = (
        not event_is_self_authored
        and dependencies.event_mentions_configured_account(config, event)
    )
    if (
        (confirmed_direct and not event_is_direct)
        or (confirmed_tracked and not event_is_tracked)
        or (confirmed_mention and not event_is_mention)
    ):
        raise ValueError(
            f"Browser handoff {event_id} route does not match stored event"
        )


def _validated_urls(
    config: Any,
    connection: sqlite3.Connection,
    *,
    event_id: str,
    event: sqlite3.Row,
    disposition: str,
    record: dict[str, Any],
    dependencies: Dependencies,
) -> tuple[str | None, str | None]:
    reply_url = dependencies.optional_text(record, "reply_url")
    existing_alex_reply_url = dependencies.optional_text(
        record,
        "existing_alex_reply_url",
    )
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
            dependencies.require_matching_published_alex_turn(
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
        matching_turns = dependencies.matching_alex_reply_turns(
            connection,
            event_id,
        )
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
    return reply_url, existing_alex_reply_url


def _candidate_revision_context(
    record: dict[str, Any],
    dependencies: Dependencies,
) -> tuple[str, bool, str | None]:
    watcher_disposition = str(record.get("watcher_disposition") or "")
    is_revision = record.get("supersedes_existing_resolution") is True
    revision_reason = (
        dependencies.required_text(record, "resolution_revision_reason")
        if is_revision
        else None
    )
    return watcher_disposition, is_revision, revision_reason


def _candidate_event(
    config: Any,
    connection: sqlite3.Connection,
    *,
    event_id: str,
    record: dict[str, Any],
    dependencies: Dependencies,
) -> sqlite3.Row:
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
    _validate_route(
        config,
        connection,
        event_id=event_id,
        event=event,
        record=record,
        dependencies=dependencies,
    )
    conversation_id = dependencies.validate_status_id(
        dependencies.required_text(record, "conversation_id"),
        "conversation_id",
    )
    if conversation_id != event["conversation_id"]:
        raise ValueError(
            f"Browser handoff {event_id} has mismatched conversation_id"
        )
    history_turn = connection.execute(
        "SELECT 1 FROM conversation_turns WHERE status_id = ?",
        (event_id,),
    ).fetchone()
    if history_turn is None:
        raise ValueError(
            f"Browser handoff {event_id} has no imported exact history turn"
        )
    return event


def _candidate_evidence(
    event_id: str,
    record: dict[str, Any],
) -> list[str]:
    evidence = record.get("evidence", [])
    if isinstance(evidence, str):
        evidence = [evidence] if evidence.strip() else []
    elif not isinstance(evidence, list):
        raise ValueError(
            f"Browser handoff {event_id} evidence must be text or an array"
        )
    return sorted(
        {str(value).strip() for value in evidence if str(value).strip()}
    )


def _candidate_disposition(
    event_id: str,
    watcher_disposition: str,
    record: dict[str, Any],
    dependencies: Dependencies,
) -> str:
    disposition = dependencies.required_text(record, "disposition")
    expected_markers = {
        "published": "verified_publication_pending_root_resolve",
        "skip": "durable_skip_pending_root_resolve",
        "blocked": "durable_blocked_pending_root_resolve",
    }
    if disposition not in expected_markers:
        raise ValueError(f"Browser handoff {event_id} has invalid disposition")
    if watcher_disposition != expected_markers[disposition]:
        raise ValueError(
            f"Browser handoff {event_id} has an invalid {disposition} marker"
        )
    return disposition


def _candidate_classification(
    event_id: str,
    record: dict[str, Any],
    dependencies: Dependencies,
) -> tuple[str | None, str | None]:
    stance = dependencies.optional_text(record, "stance")
    if stance not in {None, "supportive", "opposing", "neutral", "ambiguous"}:
        raise ValueError(
            f"Browser handoff {event_id} has invalid broad stance"
        )
    confidence = dependencies.optional_text(record, "confidence")
    if confidence not in {None, "high", "medium", "low"}:
        raise ValueError(f"Browser handoff {event_id} has invalid confidence")
    return stance, confidence


def _candidate_item(
    config: Any,
    connection: sqlite3.Connection,
    *,
    event_id: str,
    event: sqlite3.Row,
    record: dict[str, Any],
    watcher_disposition: str,
    is_revision: bool,
    revision_reason: str | None,
    evidence: list[str],
    dependencies: Dependencies,
) -> dict[str, Any]:
    disposition = _candidate_disposition(
        event_id,
        watcher_disposition,
        record,
        dependencies,
    )
    reply_url, _ = _validated_urls(
        config,
        connection,
        event_id=event_id,
        event=event,
        disposition=disposition,
        record=record,
        dependencies=dependencies,
    )
    blocker_code = dependencies.optional_text(record, "blocker_code")
    dependencies.enforce_mandatory_response_resolution(
        config,
        connection,
        event_id=event_id,
        disposition=disposition,
        blocker_code=blocker_code,
    )
    stance, confidence = _candidate_classification(
        event_id,
        record,
        dependencies,
    )
    return {
        "event_id": event_id,
        "disposition": disposition,
        "reason": dependencies.required_text(record, "reason"),
        "reply_url": reply_url,
        "blocker_code": blocker_code,
        "stance": stance,
        "stance_detail": dependencies.optional_text(record, "stance_detail"),
        "confidence": confidence,
        "media_meaning": dependencies.optional_text(record, "media_meaning"),
        "evidence": evidence,
        "is_revision": is_revision,
        "revision_reason": revision_reason,
    }


def _revision_already_applied(
    connection: sqlite3.Connection,
    event_id: str,
    item: dict[str, Any],
) -> bool:
    existing = connection.execute(
        "SELECT * FROM event_resolutions WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if not item["is_revision"]:
        return False
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
        "evidence_json": json.dumps(item["evidence"], ensure_ascii=False),
    }
    return all(existing[name] == value for name, value in expected.items())


def _prepare_candidate(
    config: Any,
    connection: sqlite3.Connection,
    *,
    event_id: str,
    record: dict[str, Any],
    dependencies: Dependencies,
) -> tuple[dict[str, Any] | None, bool]:
    watcher_disposition, is_revision, revision_reason = (
        _candidate_revision_context(record, dependencies)
    )
    event = _candidate_event(
        config,
        connection,
        event_id=event_id,
        record=record,
        dependencies=dependencies,
    )
    item = _candidate_item(
        config,
        connection,
        event_id=event_id,
        event=event,
        record=record,
        watcher_disposition=watcher_disposition,
        is_revision=is_revision,
        revision_reason=revision_reason,
        evidence=_candidate_evidence(event_id, record),
        dependencies=dependencies,
    )
    if _revision_already_applied(connection, event_id, item):
        return None, True
    return item, False


def _apply_prepared(
    config: Any,
    connection: sqlite3.Connection,
    prepared: list[dict[str, Any]],
    dependencies: Dependencies,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    resolved: list[dict[str, Any]] = []
    revised_event_ids: list[str] = []
    already_resolved: list[str] = []
    for item in prepared:
        common = {
            "disposition": item["disposition"],
            "reason": item["reason"],
            "reply_url": item["reply_url"],
            "blocker_code": item["blocker_code"],
            "stance": item["stance"],
            "stance_detail": item["stance_detail"],
            "confidence": item["confidence"],
            "media_meaning": item["media_meaning"],
            "evidence": item["evidence"],
        }
        if item["is_revision"]:
            result = dependencies.revise_event_resolution(
                config,
                connection,
                item["event_id"],
                revision_reason=item["revision_reason"],
                **common,
            )
            if result["revised"]:
                revised_event_ids.append(item["event_id"])
                resolved.append(result)
            else:
                already_resolved.append(item["event_id"])
            continue
        resolved.append(
            dependencies.resolve_event(
                config,
                connection,
                item["event_id"],
                **common,
            )
        )
    return resolved, revised_event_ids, already_resolved


def sync_browser_handoffs(
    config: Any,
    connection: sqlite3.Connection,
    *,
    history_path: Path,
    ledger_path: Path,
    dependencies: Dependencies,
) -> dict[str, Any]:
    history_parent, ledger_parent, canonical_root = _evidence_context(
        config,
        history_path,
        ledger_path,
    )
    history_result = dependencies.import_history_file(connection, history_path)
    records = dependencies.history_records(ledger_path)
    candidates, already_resolved = _select_candidates(
        connection,
        records,
        dependencies,
    )
    prepared: list[dict[str, Any]] = []
    for event_id, record in candidates.items():
        item, was_already_resolved = _prepare_candidate(
            config,
            connection,
            event_id=event_id,
            record=record,
            dependencies=dependencies,
        )
        if was_already_resolved:
            already_resolved.append(event_id)
        elif item is not None:
            prepared.append(item)

    resolved, revised, apply_already_resolved = _apply_prepared(
        config,
        connection,
        prepared,
        dependencies,
    )
    already_resolved.extend(apply_already_resolved)
    result = {
        "history": history_result,
        "eligible_handoffs": len(prepared),
        "resolved_event_ids": [item["event_id"] for item in resolved],
        "revised_event_ids": revised,
        "already_resolved_event_ids": sorted(set(already_resolved)),
        "audit": dependencies.initial_audit_status(connection),
    }
    if history_parent == ledger_parent and history_parent.parent == canonical_root:
        result["evidence_manifest"] = evidence_import.finalize_runtime_evidence(
            destination=history_parent,
            connection=connection,
            output_root=canonical_root,
        )
    return result
