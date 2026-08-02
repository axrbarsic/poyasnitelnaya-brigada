#!/usr/bin/env python3
"""Deterministic commit boundary for Browser-owner event outcomes."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import evidence_import
import xmention_watcher as watcher

from scripts import autopilot_dispatch, build_outbound_history, json_contract


EVIDENCE_NAME = "evidence.json"
HISTORY_NAME = "conversation-history.jsonl"
LEDGER_NAME = "run-ledger.jsonl"
PUBLISHED_STATE = "published_verified"
SKIP_STATE = "already_answered_verified"
BLOCKED_STATE = "terminal_blocker_verified"
AGGREGATE_NAMES = (HISTORY_NAME, LEDGER_NAME)


def _canonical_record(record: dict[str, Any]) -> str:
    return json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def _evidence_root(config: watcher.Config) -> Path:
    return (
        config.source_path.parent / "var/evidence/browser-owner"
    ).resolve()


def _read_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Required evidence file is not regular: {path}")
    return json_contract.read_object(path)


def _required_object(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"Evidence must contain an object: {name}")
    return value


def _required_text(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Evidence must contain non-empty text: {name}")
    return value.strip()


def _optional_text(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Evidence text field is invalid: {name}")
    return value.strip()


def _evidence_file(event_dir: Path, value: str) -> Path:
    candidate = (event_dir / value).resolve()
    if candidate.parent != event_dir:
        raise ValueError("Evidence files must stay inside one event directory")
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"Evidence file is not regular: {candidate}")
    return candidate


def _validated_event_dir(
    config: watcher.Config,
    requested: Path,
    *,
    claim_token: str,
) -> tuple[Path, Path, str]:
    if evidence_import.LABEL_PATTERN.fullmatch(claim_token) is None:
        raise ValueError("claim token is not a valid evidence label")
    if requested.is_symlink():
        raise ValueError("Event evidence directory must not be a symlink")
    event_dir = requested.expanduser().resolve()
    session_dir = event_dir.parent
    evidence_root = _evidence_root(config)
    if session_dir.parent != evidence_root or session_dir.name != claim_token:
        raise ValueError(
            "Event evidence must be inside the active claim directory"
        )
    event_id = event_dir.name
    if not event_id.isdigit() or len(event_id) > 19:
        raise ValueError("Event evidence directory must be a numeric status ID")
    if not event_dir.is_dir():
        raise ValueError(f"Event evidence directory is missing: {event_dir}")
    return event_dir, session_dir, event_id


def active_claim_event_ids(
    state_file: Path,
    *,
    claim_token: str,
) -> list[str]:
    state = autopilot_dispatch.load_state(state_file)
    owner = state.get("owner")
    if not isinstance(owner, dict) or owner.get("claim_token") != claim_token:
        raise ValueError("claim token is not the active Browser-owner claim")
    event_ids = [str(value) for value in owner.get("event_ids", [])]
    if not event_ids or any(
        not event_id.isdigit() or len(event_id) > 19
        for event_id in event_ids
    ):
        raise ValueError("Active Browser-owner claim has invalid event IDs")
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("Active Browser-owner claim has duplicate event IDs")
    return event_ids


def _event_row(
    connection: sqlite3.Connection,
    *,
    event_id: str,
) -> sqlite3.Row:
    event = connection.execute(
        "SELECT * FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if event is None:
        raise ValueError(f"Unknown claimed event: {event_id}")
    return event


def _event_payload(event: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json_contract.loads(
            str(event["payload_json"] or ""),
            source=f"event {event['event_id']} payload_json",
        )
    except ValueError as error:
        raise ValueError("Stored event payload is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("Stored event payload must be an object")
    return payload


def _stored_event_text(payload: dict[str, Any]) -> str:
    note_tweet = payload.get("note_tweet")
    if isinstance(note_tweet, dict):
        note_text = note_tweet.get("text")
        if isinstance(note_text, str):
            return note_text
    text = payload.get("text")
    if not isinstance(text, str):
        raise ValueError("Stored event payload lacks exact text")
    return text


def _stored_parent_status_id(payload: dict[str, Any]) -> str | None:
    references = payload.get("referenced_tweets", [])
    if not isinstance(references, list):
        raise ValueError("Stored event references must be an array")
    parent_ids = [
        str(reference.get("id") or "")
        for reference in references
        if isinstance(reference, dict)
        and reference.get("type") == "replied_to"
    ]
    if len(parent_ids) > 1:
        raise ValueError("Stored event has multiple replied_to parents")
    if not parent_ids:
        return None
    parent_id = parent_ids[0]
    if not parent_id.isdigit() or len(parent_id) > 19:
        raise ValueError("Stored event parent status ID is invalid")
    return parent_id


def _validate_target(
    evidence_path: Path,
    target: dict[str, Any],
    event: sqlite3.Row,
) -> None:
    payload = _event_payload(event)
    exact_text = build_outbound_history.target_exact_text(
        evidence_path,
        target,
    )
    if exact_text != _stored_event_text(payload):
        raise ValueError("Evidence target text does not match stored event")
    target_author_id = _required_text(target, "author_id")
    if target_author_id != str(event["author_id"] or ""):
        raise ValueError("Evidence target author does not match stored event")
    target_parent = _optional_text(target, "parent_status_id")
    if target_parent != _stored_parent_status_id(payload):
        raise ValueError("Evidence target parent does not match stored event")


def _route_fields(
    config: watcher.Config,
    connection: sqlite3.Connection,
    event: sqlite3.Row,
) -> dict[str, bool]:
    self_authored = str(event["author_id"] or "") == config.user_id
    direct = (
        not self_authored
        and bool(event["is_reply"])
        and str(event["in_reply_to_user_id"] or "") == config.user_id
    )
    tracked = (
        not direct
        and watcher.is_eligible_reply(
            connection,
            config,
            author_id=str(event["author_id"] or ""),
            is_reply=bool(event["is_reply"]),
            in_reply_to_user_id=None,
            conversation_id=str(event["conversation_id"] or ""),
        )
    )
    mention = (
        not direct
        and not tracked
        and not self_authored
        and watcher.event_mentions_configured_account(config, event)
    )
    if not any((direct, tracked, mention)):
        raise ValueError("Claimed event has no authorized Browser handoff route")
    return {
        "direct_reply_to_axrbarsic": direct,
        "tracked_conversation_reply": tracked,
        "mention_reply_to_axrbarsic": mention,
    }


def _disposition(evidence: dict[str, Any]) -> str:
    state = _required_text(evidence, "state")
    states = {
        PUBLISHED_STATE: "published",
        SKIP_STATE: "skip",
        BLOCKED_STATE: "blocked",
    }
    try:
        return states[state]
    except KeyError as error:
        raise ValueError(f"Unsupported terminal evidence state: {state}") from error


def _history_record(
    evidence_path: Path,
    evidence: dict[str, Any],
    *,
    disposition: str,
    maximum_length: int,
) -> dict[str, Any]:
    payload = dict(evidence)
    if disposition == "skip":
        payload["reply"] = _required_object(evidence, "existing_reply")
    elif disposition == "blocked":
        payload.pop("reply", None)
    return build_outbound_history.build_record_from_payload(
        evidence_path,
        payload,
        maximum_length=maximum_length,
        enforce_reply_constraints=disposition != "skip",
    )


def _validate_report(
    event_dir: Path,
    report_name: str,
    *,
    status_id: str,
    parent_status_id: str,
    exact_text: str,
) -> Path:
    report_path = _evidence_file(event_dir, report_name)
    report = _read_object(report_path)
    required_true = ("valid", "exact_file_match", "parent_matches")
    if any(report.get(name) is not True for name in required_true):
        raise ValueError("Official X verification report is not valid")
    if str(report.get("status_id") or "") != status_id:
        raise ValueError("Official X verification status ID does not match")
    parent_ids = [str(value) for value in report.get("parent_status_ids", [])]
    if parent_status_id not in parent_ids:
        raise ValueError("Official X verification parent ID does not match")
    if int(report.get("code_points") or -1) != len(exact_text):
        raise ValueError("Official X verification length does not match")
    exact_sha = hashlib.sha256(exact_text.encode("utf-8")).hexdigest()
    if str(report.get("sha256") or "") != exact_sha:
        raise ValueError("Official X verification SHA-256 does not match")
    return report_path


def _validate_generation(evidence: dict[str, Any]) -> None:
    generation = _required_object(evidence, "generation")
    profile = _required_text(generation, "generation_profile")
    skill = generation.get("generation_skill")
    web_used = generation.get("chatgpt_web_used")
    allowed_profiles = {
        "local_sol_max",
        "sol_short",
        "short_sol_max",
        "satirical_377",
        "lozhkin_web",
    }
    if profile not in allowed_profiles:
        raise ValueError("Published outcome has unknown generation profile")
    if _required_text(generation, "generation_model") != "gpt-5.6-sol":
        raise ValueError("Published outcome must use gpt-5.6-sol")
    if _required_text(generation, "reasoning_effort") != "max":
        raise ValueError("Published outcome must use reasoning_effort=max")
    if profile == "local_sol_max":
        if skill != "poyasnitelnaya-brigada-v2":
            raise ValueError(
                "local_sol_max must use poyasnitelnaya-brigada-v2"
            )
        if web_used is not False:
            raise ValueError("local_sol_max must not use ChatGPT web")
    elif profile in {"sol_short", "short_sol_max"}:
        if skill is not None:
            raise ValueError(
                "Sol short outcome must not name a generation skill"
            )
        if web_used is not False:
            raise ValueError("Sol short outcome must not use ChatGPT web")
    elif profile == "satirical_377":
        if skill != "377" or web_used is not False:
            raise ValueError("satirical_377 must use local skill 377")
    elif skill != "lozhkin" or web_used is not True:
        raise ValueError("lozhkin_web requires the explicit web visual bot")


def _validated_published_reply(
    event_dir: Path,
    evidence: dict[str, Any],
    *,
    event_id: str,
) -> tuple[dict[str, Any], Path, str]:
    reply = _required_object(evidence, "reply")
    if _required_text(reply, "parent_status_id") != event_id:
        raise ValueError("Published reply parent does not match event ID")
    reply_file = _evidence_file(event_dir, _required_text(reply, "file"))
    exact_text = build_outbound_history.read_exact_file(reply_file)
    exact_sha = hashlib.sha256(exact_text.encode("utf-8")).hexdigest()
    if int(reply.get("code_points") or -1) != len(exact_text):
        raise ValueError("Evidence reply length does not match reply file")
    if str(reply.get("sha256_exact_text") or "") != exact_sha:
        raise ValueError("Evidence reply SHA-256 does not match reply file")
    return reply, reply_file, exact_text


def _require_exact_published_history(
    history: dict[str, Any],
    exact_text: str,
) -> None:
    alex_turns = [
        turn
        for turn in history.get("turns", [])
        if isinstance(turn, dict) and turn.get("actor") == "alex"
    ]
    if len(alex_turns) != 1 or alex_turns[0].get("exact_text") != exact_text:
        raise ValueError("Generated history lacks the exact published Alex turn")


def _published_fields(
    event_dir: Path,
    evidence: dict[str, Any],
    history: dict[str, Any],
    *,
    event_id: str,
) -> tuple[dict[str, Any], list[Path]]:
    _validate_generation(evidence)
    verification = _required_object(evidence, "verification")
    reply, reply_file, exact_text = _validated_published_reply(
        event_dir,
        evidence,
        event_id=event_id,
    )
    report_name = (
        _optional_text(verification, "official_api_note_tweet_report_file")
        or _optional_text(verification, "official_api_verification_file")
        or "api-verification.json"
    )
    report_path = _validate_report(
        event_dir,
        report_name,
        status_id=_required_text(reply, "status_id"),
        parent_status_id=event_id,
        exact_text=exact_text,
    )
    _require_exact_published_history(history, exact_text)
    return (
        {
            "reply_url": _required_text(reply, "url"),
            "source_path": str(reply_file),
            "source_sha256": evidence_import.sha256(reply_file),
            "source_code_points": len(exact_text),
            "alex_history_status": "exact_alex_turn_appended",
        },
        [report_path],
    )


def _skip_fields(
    event_dir: Path,
    evidence: dict[str, Any],
    *,
    event_id: str,
) -> tuple[dict[str, Any], list[Path]]:
    existing = _required_object(evidence, "existing_reply")
    verification = _required_object(evidence, "duplicate_verification")
    status_id = _required_text(existing, "status_id")
    parent_status_id = _required_text(existing, "parent_status_id")
    if parent_status_id != event_id:
        raise ValueError("Existing Alex reply parent does not match event ID")
    exact_file = _evidence_file(event_dir, _required_text(existing, "file"))
    exact_text = build_outbound_history.read_exact_file(exact_file)
    report_name = _required_text(verification, "verification_file")
    report_path = _validate_report(
        event_dir,
        report_name,
        status_id=status_id,
        parent_status_id=event_id,
        exact_text=exact_text,
    )
    return (
        {
            "reply_url": None,
            "existing_alex_reply_url": _required_text(existing, "url"),
            "alex_history_status": "exact_alex_turn_appended",
        },
        [report_path],
    )


def _validated_outcome(
    connection: sqlite3.Connection,
    *,
    evidence_path: Path,
    claim_token: str,
    event_id: str,
    maximum_length: int,
) -> tuple[
    dict[str, Any],
    sqlite3.Row,
    str,
    str,
    dict[str, Any],
    dict[str, Any],
    str,
]:
    evidence = _read_object(evidence_path)
    if evidence.get("schema_version") != 1:
        raise ValueError("Unsupported Browser outcome schema_version")
    if _required_text(evidence, "session_id") != claim_token:
        raise ValueError("Evidence session_id does not match claim token")
    target = _required_object(evidence, "target")
    if _required_text(target, "status_id") != event_id:
        raise ValueError("Evidence target status ID does not match directory")
    event = _event_row(connection, event_id=event_id)
    _validate_target(evidence_path, target, event)
    conversation_id = str(event["conversation_id"] or "")
    if _required_text(target, "chain_id") != conversation_id:
        raise ValueError("Evidence chain_id does not match stored conversation")
    disposition = _disposition(evidence)
    history = _history_record(
        evidence_path,
        evidence,
        disposition=disposition,
        maximum_length=maximum_length,
    )
    resolution = evidence.get("resolution")
    if resolution is None:
        resolution = evidence.get("blocker")
    if not isinstance(resolution, dict):
        raise ValueError("Evidence must contain resolution or blocker details")
    verification = _required_object(evidence, "verification")
    checked_at = _required_text(verification, "verified_at")
    checked_time = watcher.parse_time(checked_at)
    if (
        checked_time is None
        or checked_time.tzinfo is None
        or checked_time.utcoffset() is None
    ):
        raise ValueError("verification.verified_at must be timezone-aware")
    return (
        evidence,
        event,
        conversation_id,
        disposition,
        history,
        resolution,
        checked_at,
    )


def _base_ledger(
    config: watcher.Config,
    connection: sqlite3.Connection,
    event: sqlite3.Row,
    evidence: dict[str, Any],
    *,
    event_id: str,
    conversation_id: str,
    disposition: str,
    reason: str,
    checked_at: str,
) -> dict[str, Any]:
    classification = evidence.get("classification")
    if not isinstance(classification, dict):
        classification = {}
    return {
        "event": "initial_audit_disposition",
        "event_id": event_id,
        "conversation_id": conversation_id,
        **_route_fields(config, connection, event),
        "history_status": "exact_user_turn_appended",
        "watcher_disposition": {
            "published": "verified_publication_pending_root_resolve",
            "skip": "durable_skip_pending_root_resolve",
            "blocked": "durable_blocked_pending_root_resolve",
        }[disposition],
        "disposition": disposition,
        "reason": reason,
        "reply_url": None,
        "blocker_code": None,
        "stance": _optional_text(classification, "stance"),
        "stance_detail": _optional_text(classification, "stance_detail"),
        "confidence": _optional_text(classification, "confidence"),
        "media_meaning": _optional_text(classification, "media_meaning"),
        "checked_at": checked_at,
    }


def _apply_disposition_fields(
    event_dir: Path,
    evidence: dict[str, Any],
    history: dict[str, Any],
    ledger: dict[str, Any],
    *,
    event_id: str,
    disposition: str,
) -> list[Path]:
    if disposition == "published":
        fields, extra = _published_fields(
            event_dir,
            evidence,
            history,
            event_id=event_id,
        )
        ledger.update(fields)
        return extra
    if disposition == "skip":
        fields, extra = _skip_fields(
            event_dir,
            evidence,
            event_id=event_id,
        )
        ledger.update(fields)
        return extra
    blocker = _required_object(evidence, "blocker")
    blocker_code = _required_text(blocker, "code")
    if blocker_code not in watcher.TERMINAL_BLOCKER_CODES:
        raise ValueError("Evidence blocker code is not terminal")
    ledger["blocker_code"] = blocker_code
    return []


def _apply_ledger_evidence(
    config: watcher.Config,
    evidence_path: Path,
    extra_evidence: list[Path],
    resolution: dict[str, Any],
    ledger: dict[str, Any],
) -> None:
    supplied = resolution.get("evidence", [])
    if not isinstance(supplied, list):
        raise ValueError("resolution.evidence must be an array")
    ledger["evidence"] = sorted(
        {
            str(evidence_path.relative_to(config.source_path.parent)),
            *(str(value).strip() for value in supplied if str(value).strip()),
            *(
                str(path.relative_to(config.source_path.parent))
                for path in extra_evidence
            ),
        }
    )


def _apply_resolution_recovery(
    evidence: dict[str, Any],
    ledger: dict[str, Any],
) -> None:
    recovery = evidence.get("resolution_recovery")
    if recovery is None:
        return
    if not isinstance(recovery, dict):
        raise ValueError("resolution_recovery must be an object")
    if recovery.get("supersedes_existing_resolution") is not True:
        raise ValueError("resolution_recovery must supersede a resolution")
    ledger["supersedes_existing_resolution"] = True
    ledger["resolution_revision_reason"] = _required_text(
        recovery,
        "resolution_revision_reason",
    )


def build_event_records(
    config: watcher.Config,
    connection: sqlite3.Connection,
    *,
    event_dir: Path,
    claim_token: str,
    event_id: str,
    maximum_length: int = 4000,
) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence_path = event_dir / EVIDENCE_NAME
    (
        evidence,
        event,
        conversation_id,
        disposition,
        history,
        resolution,
        checked_at,
    ) = _validated_outcome(
        connection,
        evidence_path=evidence_path,
        claim_token=claim_token,
        event_id=event_id,
        maximum_length=maximum_length,
    )
    ledger = _base_ledger(
        config,
        connection,
        event,
        evidence,
        event_id=event_id,
        conversation_id=conversation_id,
        disposition=disposition,
        reason=_required_text(resolution, "reason"),
        checked_at=checked_at,
    )
    extra_evidence = _apply_disposition_fields(
        event_dir,
        evidence,
        history,
        ledger,
        event_id=event_id,
        disposition=disposition,
    )
    _apply_ledger_evidence(
        config,
        evidence_path,
        extra_evidence,
        resolution,
        ledger,
    )
    _apply_resolution_recovery(evidence, ledger)
    return history, ledger


def _write_or_confirm(path: Path, record: dict[str, Any]) -> str:
    serialized = _canonical_record(record)
    if path.exists():
        if path.is_symlink() or path.read_text(encoding="utf-8") != serialized:
            raise ValueError(f"Generated evidence conflicts with {path.name}")
        return "unchanged"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return "created"


def _confirm_existing_record(path: Path, record: dict[str, Any]) -> None:
    if not path.exists():
        return
    if path.is_symlink() or path.read_text(encoding="utf-8") != _canonical_record(
        record
    ):
        raise ValueError(f"Generated evidence conflicts with {path.name}")


def commit_event(
    *,
    config_path: Path,
    claim_token: str,
    requested_event_dir: Path,
    maximum_length: int = 4000,
) -> dict[str, Any]:
    if maximum_length <= 0:
        raise ValueError("maximum_length must be positive")
    config = watcher.load_config(config_path)
    event_dir, _, event_id = _validated_event_dir(
        config,
        requested_event_dir,
        claim_token=claim_token,
    )
    _, state_file = autopilot_dispatch.load_paths(config_path)
    connection = watcher.connect_database(config.database)
    try:
        with autopilot_dispatch.locked_state(state_file):
            claimed = active_claim_event_ids(
                state_file,
                claim_token=claim_token,
            )
            if event_id not in claimed:
                raise ValueError("Event is not part of the active claim")
            history, ledger = build_event_records(
                config,
                connection,
                event_dir=event_dir,
                claim_token=claim_token,
                event_id=event_id,
                maximum_length=maximum_length,
            )
            history_path = event_dir / HISTORY_NAME
            ledger_path = event_dir / LEDGER_NAME
            _confirm_existing_record(history_path, history)
            _confirm_existing_record(ledger_path, ledger)
            with tempfile.TemporaryDirectory(
                prefix=".event-commit.",
                dir=event_dir,
            ) as temporary_name:
                staging = Path(temporary_name)
                staging_history = staging / HISTORY_NAME
                staging_ledger = staging / LEDGER_NAME
                _atomic_write_records(staging_history, [history])
                _atomic_write_records(staging_ledger, [ledger])
                sync = watcher.sync_browser_handoffs(
                    config,
                    connection,
                    history_path=staging_history,
                    ledger_path=staging_ledger,
                )
            history_state = _write_or_confirm(history_path, history)
            ledger_state = _write_or_confirm(ledger_path, ledger)
        resolution = connection.execute(
            "SELECT disposition, reply_url, blocker_code, resolved_at "
            "FROM event_resolutions WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if resolution is None:
            raise ValueError("Event did not receive a durable resolution")
        return {
            "status": "committed",
            "claim_token": claim_token,
            "event_id": event_id,
            "disposition": resolution["disposition"],
            "reply_url": resolution["reply_url"],
            "blocker_code": resolution["blocker_code"],
            "resolved_at": resolution["resolved_at"],
            "history": history_state,
            "ledger": ledger_state,
            "sync": sync,
        }
    finally:
        connection.close()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Evidence JSONL is not a regular file: {path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            record = json_contract.loads(
                line,
                source=f"{path}:{line_number}",
            )
        except (json.JSONDecodeError, json_contract.DuplicateKeyError) as error:
            raise ValueError(
                f"Invalid JSONL in {path} at line {line_number}"
            ) from error
        if not isinstance(record, dict):
            raise ValueError(
                f"JSONL record must be an object in {path} "
                f"at line {line_number}"
            )
        records.append(record)
    if not records:
        raise ValueError(f"Evidence JSONL contains no records: {path}")
    return records


def _atomic_write_records(
    path: Path,
    records: Sequence[dict[str, Any]],
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(_canonical_record(record))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _status_ids_from_history(record: dict[str, Any]) -> set[str]:
    status_ids: set[str] = set()
    status_id = str(record.get("status_id") or "").strip()
    if status_id:
        status_ids.add(status_id)
    turns = record.get("turns")
    if isinstance(turns, list):
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            turn_status_id = str(turn.get("status_id") or "").strip()
            if turn_status_id:
                status_ids.add(turn_status_id)
    return status_ids


def _event_id_from_ledger(
    records: Sequence[dict[str, Any]],
    *,
    source: Path,
) -> str:
    dispositions = [
        record
        for record in records
        if record.get("event") == "initial_audit_disposition"
    ]
    if len(dispositions) != 1:
        raise ValueError(
            f"Expected exactly one final disposition in {source}, "
            f"found {len(dispositions)}"
        )
    event_id = str(dispositions[0].get("event_id") or "").strip()
    if not event_id.isdigit() or len(event_id) > 19:
        raise ValueError(f"Invalid disposition event_id in {source}")
    return event_id


def _event_evidence_dirs(session_dir: Path) -> list[Path]:
    directories: list[Path] = []
    for child in sorted(session_dir.iterdir(), key=lambda path: path.name):
        if child.name in AGGREGATE_NAMES or child.name == "manifest.json":
            continue
        if child.is_symlink():
            raise ValueError(f"Symlink is not allowed in evidence: {child}")
        if child.is_dir():
            directories.append(child)
    return directories


def _event_directory_records(
    child: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str] | None:
    history_path = child / HISTORY_NAME
    ledger_path = child / LEDGER_NAME
    if not history_path.exists() and not ledger_path.exists():
        return None
    if not history_path.is_file() or not ledger_path.is_file():
        raise ValueError(
            f"Event evidence must contain both JSONL files: {child}"
        )
    history_records = _read_jsonl(history_path)
    ledger_records = _read_jsonl(ledger_path)
    event_id = _event_id_from_ledger(ledger_records, source=ledger_path)
    if child.name != event_id:
        raise ValueError(
            f"Event directory {child.name} does not match ledger "
            f"event_id {event_id}"
        )
    history_status_ids = {
        status_id
        for record in history_records
        for status_id in _status_ids_from_history(record)
    }
    if event_id not in history_status_ids:
        raise ValueError(
            f"History in {child} does not contain its exact event turn"
        )
    return history_records, ledger_records, event_id


def _register_unique_records(
    seen: dict[tuple[str, str], Path],
    *,
    kind: str,
    source: Path,
    records: list[dict[str, Any]],
) -> None:
    for record in records:
        key = (kind, _canonical_record(record))
        previous = seen.get(key)
        if previous is not None:
            raise ValueError(
                f"Duplicate {kind} record in {previous} and {source}"
            )
        seen[key] = source


def _collect_event_records(
    session_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    histories: list[dict[str, Any]] = []
    ledgers: list[dict[str, Any]] = []
    event_ids: list[str] = []
    seen_records: dict[tuple[str, str], Path] = {}
    for child in _event_evidence_dirs(session_dir):
        collected = _event_directory_records(child)
        if collected is None:
            continue
        history_records, ledger_records, event_id = collected
        if event_id in event_ids:
            raise ValueError(f"Duplicate event evidence: {event_id}")
        event_ids.append(event_id)
        for kind, source, records in (
            ("history", child / HISTORY_NAME, history_records),
            ("ledger", child / LEDGER_NAME, ledger_records),
        ):
            _register_unique_records(
                seen_records,
                kind=kind,
                source=source,
                records=records,
            )
        histories.extend(history_records)
        ledgers.extend(ledger_records)
    if not event_ids:
        raise ValueError("Browser-owner session contains no event evidence")
    return histories, ledgers, event_ids


def _write_or_confirm_aggregate(
    path: Path,
    records: Sequence[dict[str, Any]],
) -> bool:
    if path.exists():
        existing = _read_jsonl(path)
        if list(existing) != list(records):
            raise ValueError(
                f"Existing aggregate differs from event evidence: {path}"
            )
        return False
    _atomic_write_records(path, records)
    return True


def _confirm_existing_aggregate(
    path: Path,
    records: Sequence[dict[str, Any]],
) -> None:
    if path.exists() and _read_jsonl(path) != list(records):
        raise ValueError(
            f"Existing aggregate differs from event evidence: {path}"
        )


def _validated_session_dir(
    config: watcher.Config,
    requested: Path,
) -> tuple[Path, Path]:
    if requested.is_symlink():
        raise ValueError(
            f"Evidence session directory must not be a symlink: {requested}"
        )
    session_dir = requested.expanduser().resolve()
    evidence_root = _evidence_root(config)
    if session_dir.parent != evidence_root:
        raise ValueError(
            "Evidence session must be a direct child of the canonical "
            f"Browser-owner root: {evidence_root}"
        )
    if not session_dir.is_dir():
        raise ValueError(f"Evidence session is not a directory: {session_dir}")
    if evidence_import.LABEL_PATTERN.fullmatch(session_dir.name) is None:
        raise ValueError("Evidence session has an invalid directory name")
    return session_dir, evidence_root


def _existing_manifest_result(
    session_dir: Path,
    evidence_root: Path,
    connection: sqlite3.Connection,
) -> dict[str, Any] | None:
    if not (session_dir / "manifest.json").is_file():
        return None
    result = evidence_import.finalize_runtime_evidence(
        destination=session_dir,
        connection=connection,
        output_root=evidence_root,
    )
    return {
        "status": result["status"],
        "session_dir": str(session_dir),
        "manifest": result,
    }


def _require_expected_event_set(
    expected_event_ids: list[str],
    evidence_event_ids: list[str],
) -> None:
    if set(evidence_event_ids) == set(expected_event_ids):
        return
    missing = sorted(
        set(expected_event_ids) - set(evidence_event_ids),
        key=int,
    )
    unexpected = sorted(
        set(evidence_event_ids) - set(expected_event_ids),
        key=int,
    )
    raise ValueError(
        "Evidence event set does not match active claim: "
        f"missing={missing}, unexpected={unexpected}"
    )


def _sync_staged_handoffs(
    config: watcher.Config,
    connection: sqlite3.Connection,
    session_dir: Path,
    histories: list[dict[str, Any]],
    ledgers: list[dict[str, Any]],
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(
        prefix=".handoff-sync.",
        dir=session_dir,
    ) as temporary_name:
        staging = Path(temporary_name)
        staging_history = staging / HISTORY_NAME
        staging_ledger = staging / LEDGER_NAME
        _atomic_write_records(staging_history, histories)
        _atomic_write_records(staging_ledger, ledgers)
        return watcher.sync_browser_handoffs(
            config,
            connection,
            history_path=staging_history,
            ledger_path=staging_ledger,
        )


def _require_unchanged_event_records(
    session_dir: Path,
    histories: list[dict[str, Any]],
    ledgers: list[dict[str, Any]],
    event_ids: list[str],
) -> None:
    current_histories, current_ledgers, current_event_ids = (
        _collect_event_records(session_dir)
    )
    if (
        current_histories != histories
        or current_ledgers != ledgers
        or current_event_ids != event_ids
    ):
        raise ValueError("Per-event evidence changed during finalization")


def _write_aggregates_and_manifest(
    connection: sqlite3.Connection,
    *,
    session_dir: Path,
    evidence_root: Path,
    histories: list[dict[str, Any]],
    ledgers: list[dict[str, Any]],
) -> dict[str, Any]:
    aggregate_paths = (session_dir / HISTORY_NAME, session_dir / LEDGER_NAME)
    for path, records in zip(
        aggregate_paths,
        (histories, ledgers),
        strict=True,
    ):
        _confirm_existing_aggregate(path, records)
    created_paths: list[Path] = []
    try:
        for path, records in zip(
            aggregate_paths,
            (histories, ledgers),
            strict=True,
        ):
            if _write_or_confirm_aggregate(path, records):
                created_paths.append(path)
        return evidence_import.finalize_runtime_evidence(
            destination=session_dir,
            connection=connection,
            output_root=evidence_root,
        )
    except Exception:
        if not (session_dir / "manifest.json").exists():
            for path in created_paths:
                path.unlink(missing_ok=True)
        raise


def finalize_session(
    *,
    config_path: Path,
    requested_session_dir: Path,
) -> dict[str, Any]:
    config = watcher.load_config(config_path)
    session_dir, evidence_root = _validated_session_dir(
        config,
        requested_session_dir,
    )
    connection = watcher.connect_database(config.database)
    try:
        existing = _existing_manifest_result(
            session_dir,
            evidence_root,
            connection,
        )
        if existing is not None:
            return existing
        _, state_file = autopilot_dispatch.load_paths(config_path)
        with autopilot_dispatch.locked_state(state_file):
            expected_event_ids = active_claim_event_ids(
                state_file,
                claim_token=session_dir.name,
            )
            histories, ledgers, evidence_event_ids = _collect_event_records(
                session_dir
            )
            _require_expected_event_set(
                expected_event_ids,
                evidence_event_ids,
            )
            for path, records in zip(
                (session_dir / HISTORY_NAME, session_dir / LEDGER_NAME),
                (histories, ledgers),
                strict=True,
            ):
                _confirm_existing_aggregate(path, records)
            sync = _sync_staged_handoffs(
                config,
                connection,
                session_dir,
                histories,
                ledgers,
            )
            _require_unchanged_event_records(
                session_dir,
                histories,
                ledgers,
                evidence_event_ids,
            )
            manifest = _write_aggregates_and_manifest(
                connection,
                session_dir=session_dir,
                evidence_root=evidence_root,
                histories=histories,
                ledgers=ledgers,
            )
        if not manifest.get("complete"):
            raise ValueError("Browser-owner evidence manifest is incomplete")
        sync["evidence_manifest"] = manifest
        return {
            "status": "finalized",
            "session_dir": str(session_dir),
            "event_ids": sorted(evidence_event_ids, key=int),
            "history_records": len(histories),
            "ledger_records": len(ledgers),
            "sync": sync,
        }
    finally:
        connection.close()
