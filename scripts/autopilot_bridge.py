#!/usr/bin/env python3
"""Claim a token-free X queue for one Codex Desktop worker run."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from scripts import (
        autopilot_continuation,
        autopilot_contract,
        autopilot_dispatch,
        browser_owner_evidence,
        browser_owner_claim,
        event_dispatch,
        inbound_route_control,
        inbound_policy,
        resource_guard,
    )
except ModuleNotFoundError:
    import autopilot_continuation  # type: ignore[no-redef]
    import autopilot_contract  # type: ignore[no-redef]
    import autopilot_dispatch  # type: ignore[no-redef]
    import browser_owner_evidence  # type: ignore[no-redef]
    import browser_owner_claim  # type: ignore[no-redef]
    import event_dispatch  # type: ignore[no-redef]
    import inbound_route_control  # type: ignore[no-redef]
    import inbound_policy  # type: ignore[no-redef]
    import resource_guard  # type: ignore[no-redef]

try:
    import xmention_watcher as watcher
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import xmention_watcher as watcher  # type: ignore[no-redef]

manual_parent_continuation_profile = (
    autopilot_continuation.manual_parent_continuation_profile
)


def resolve_path(config_path: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return (config_path.resolve().parent / candidate).resolve()


def health_path(config_path: Path) -> Path:
    config = autopilot_dispatch.read_json(config_path)
    return resolve_path(
        config_path,
        str(config.get("autopilot_health_file", "var/autopilot-health.json")),
    )


def handoff_reservation_path(config_path: Path) -> Path:
    config = autopilot_dispatch.read_json(config_path)
    return resolve_path(
        config_path,
        str(
            config.get(
                "relay_handoff_reservation_file",
                "var/relay-handoff.json",
            )
        ),
    )


def route_control_path(config_path: Path, config: dict[str, Any]) -> Path:
    return inbound_route_control.state_path(config_path, config)


def route_selection(
    config_path: Path,
    config: dict[str, Any],
    pending_events: list[dict[str, Any]],
) -> tuple[dict[str, Any], frozenset[str], frozenset[str]]:
    path = route_control_path(config_path, config)
    pending_ids = [str(event["id"]) for event in pending_events]
    state = inbound_route_control.reconcile_wave(
        path,
        pending_event_ids=pending_ids,
        updated_at=autopilot_dispatch.isoformat(),
    )
    priority_ids, excluded_ids = inbound_route_control.selection_sets(
        state,
        pending_events,
    )
    return state, priority_ids, excluded_ids


def active_supervisor_owner(
    config_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a live doctor owner that must finish before another X claim."""

    config = autopilot_dispatch.read_json(config_path)
    path = resolve_path(
        config_path,
        str(
            config.get(
                "autopilot_supervisor_state_file",
                "var/autopilot-supervisor.json",
            )
        ),
    )
    try:
        state = autopilot_dispatch.read_json(path)
    except (OSError, ValueError, TypeError, AttributeError):
        return {"owner_busy": False}
    incident = state.get("incident")
    owner = incident.get("owner") if isinstance(incident, dict) else None
    lease_expires_at = (
        autopilot_dispatch.parse_time(owner.get("lease_expires_at"))
        if isinstance(owner, dict)
        else None
    )
    current = now or autopilot_dispatch.utc_now()
    owner_busy = (
        isinstance(incident, dict)
        and str(incident.get("status", ""))
        in {"claimed", "work_in_progress"}
        and isinstance(owner, dict)
        and bool(str(owner.get("claim_token", "")))
        and lease_expires_at is not None
        and lease_expires_at > current
    )
    if not owner_busy:
        return {"owner_busy": False}
    return {
        "owner_busy": True,
        "incident_id": str(incident.get("id", "")),
        "status": str(incident.get("status", "")),
        "lease_expires_at": autopilot_dispatch.isoformat(lease_expires_at),
    }


def reserve_handoff(
    config_path: Path,
    *,
    lease_seconds: int,
) -> dict[str, Any]:
    """Atomically reserve one relay handoff without claiming X events."""

    ready = gate(config_path, lease_seconds=lease_seconds)
    if not ready.get("dispatch"):
        return ready
    config = autopilot_dispatch.read_json(config_path)
    reservation_seconds = int(
        config.get("relay_handoff_reservation_seconds", 180)
    )
    if reservation_seconds <= 0:
        raise ValueError("relay handoff reservation seconds must be positive")
    path = handoff_reservation_path(config_path)
    now = autopilot_dispatch.utc_now()
    with autopilot_dispatch.locked_state(path):
        previous = (
            autopilot_dispatch.read_json(path) if path.exists() else {}
        )
        expires_at = autopilot_dispatch.parse_time(
            str(previous.get("expires_at", ""))
        )
        if (
            previous.get("status") == "reserved"
            and expires_at is not None
            and expires_at > now
        ):
            return {
                "status": "handoff_reserved",
                "dispatch": False,
                "event_ids": list(ready.get("event_ids", [])),
                "pending_count": int(ready.get("pending_count", 0)),
                "reservation_token": previous.get("reservation_token"),
                "expires_at": previous.get("expires_at"),
            }
        token = str(uuid.uuid4())
        expires_at = now + timedelta(seconds=reservation_seconds)
        payload = {
            "version": 1,
            "status": "reserved",
            "reservation_token": token,
            "event_ids": list(ready.get("event_ids", [])),
            "reserved_at": autopilot_dispatch.isoformat(now),
            "expires_at": autopilot_dispatch.isoformat(expires_at),
        }
        autopilot_dispatch.atomic_write_json(path, payload)
    return {
        **ready,
        "status": "handoff_reserved_ready",
        "reservation_token": token,
        "expires_at": autopilot_dispatch.isoformat(expires_at),
    }


def clear_handoff_reservation(
    config_path: Path,
    *,
    claim_token: str,
    event_ids: list[str],
) -> None:
    path = handoff_reservation_path(config_path)
    with autopilot_dispatch.locked_state(path):
        previous = (
            autopilot_dispatch.read_json(path) if path.exists() else {}
        )
        autopilot_dispatch.atomic_write_json(
            path,
            {
                "version": 1,
                "status": "claimed",
                "reservation_token": previous.get("reservation_token"),
                "claim_token": claim_token,
                "event_ids": event_ids,
                "cleared_at": autopilot_dispatch.isoformat(),
            },
        )


def release_handoff_reservation(
    config_path: Path,
    *,
    reservation_token: str,
    reason: str,
) -> dict[str, Any]:
    """Release only the relay reservation identified by the exact token."""

    token = reservation_token.strip()
    release_reason = reason.strip()
    if not token:
        raise ValueError("reservation token must not be empty")
    if not release_reason:
        raise ValueError("handoff release reason must not be empty")

    path = handoff_reservation_path(config_path)
    with autopilot_dispatch.locked_state(path):
        previous = (
            autopilot_dispatch.read_json(path) if path.exists() else {}
        )
        status = str(previous.get("status", "missing"))
        previous_token = str(previous.get("reservation_token", ""))
        if status != "reserved":
            return {
                "status": "handoff_not_released",
                "released": False,
                "reservation_status": status,
                "reservation_token": previous.get("reservation_token"),
                "event_ids": list(previous.get("event_ids", [])),
            }
        if previous_token != token:
            raise ValueError("reservation token does not match active handoff")

        payload = {
            "version": 1,
            "status": "released",
            "reservation_token": token,
            "event_ids": list(previous.get("event_ids", [])),
            "reserved_at": previous.get("reserved_at"),
            "expires_at": previous.get("expires_at"),
            "released_at": autopilot_dispatch.isoformat(),
            "release_reason": release_reason,
        }
        autopilot_dispatch.atomic_write_json(path, payload)

    return {
        "status": "handoff_released",
        "released": True,
        "reservation_token": token,
        "event_ids": payload["event_ids"],
        "reason": release_reason,
    }


def write_health(
    config_path: Path,
    *,
    status: str,
    event_ids: list[str],
    claim_token: str | None = None,
    error: str | None = None,
) -> None:
    path = health_path(config_path)
    config = autopilot_dispatch.read_json(config_path)
    rotation_after = int(
        config.get("autopilot_owner_rotation_after_runs", 20)
    )
    if rotation_after <= 0:
        raise ValueError("autopilot_owner_rotation_after_runs must be positive")
    with autopilot_dispatch.locked_state(path):
        previous: dict[str, Any] = {}
        if path.exists():
            previous = autopilot_dispatch.read_json(path)
        handoff_count = int(
            previous.get(
                "handoff_count",
                previous.get("completed_runs", 0),
            )
        )
        effective_status = status
        if (
            status == "leased_waiting"
            and previous.get("status") in {"handoff_pending", "work_in_progress"}
            and sorted(previous.get("event_ids", []), key=int)
            == sorted(event_ids, key=int)
        ):
            effective_status = str(previous["status"])
        if status in {"completed", "completed_with_warning"}:
            handoff_count += 1
        payload: dict[str, Any] = {
            "version": 2,
            "status": effective_status,
            "updated_at": autopilot_dispatch.isoformat(),
            "event_ids": event_ids,
            "handoff_count": handoff_count,
            "completed_runs": handoff_count,
            "rotation_after_runs": rotation_after,
            "rotation_recommended": handoff_count >= rotation_after,
        }
        effective_claim_token = claim_token
        if (
            effective_claim_token is None
            and effective_status in {"handoff_pending", "work_in_progress"}
        ):
            effective_claim_token = previous.get("claim_token")
        if effective_claim_token:
            payload["claim_token"] = effective_claim_token
        if error:
            payload["error"] = error
        autopilot_dispatch.atomic_write_json(path, payload)


def _deferred_claim_result(
    wake_file: Path,
    guard: dict[str, Any],
) -> dict[str, Any]:
    pending = autopilot_dispatch.load_wake_events(wake_file)
    return {
        "status": "memory_deferred",
        "dispatch": False,
        "pending_count": len(pending),
        "event_ids": [str(event["id"]) for event in pending],
        "resource_guard": guard,
    }


def _non_dispatch_claim_result(
    config_path: Path,
    wake_file: Path,
    result: dict[str, Any],
    guard: dict[str, Any],
) -> dict[str, Any]:
    pending = autopilot_dispatch.load_wake_events(wake_file)
    if result.get("owner_busy"):
        path = health_path(config_path)
        previous = autopilot_dispatch.read_json(path) if path.exists() else {}
        return {
            "status": str(previous.get("status", "leased_waiting")),
            **result,
            "resource_guard": guard,
        }
    status = str(result.get("status") or ("leased_waiting" if pending else "idle"))
    write_health(
        config_path,
        status=status,
        event_ids=[str(event["id"]) for event in pending],
    )
    return {"status": status, **result, "resource_guard": guard}


def _fallback_commenter_memory(event_id: str) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "identity_kind": "unavailable",
        "total_prior_interactions": 0,
        "returned_interactions": 0,
        "interactions": [],
    }


def _resolution_recovery(
    connection: sqlite3.Connection,
    event_id: str,
) -> dict[str, Any] | None:
    existing = connection.execute(
        """
        SELECT disposition, reason, blocker_code, resolved_at
        FROM event_resolutions
        WHERE event_id = ?
        """,
        (event_id,),
    ).fetchone()
    if existing is None:
        return None
    latest_requeue = connection.execute(
        """
        SELECT reason, requeued_at
        FROM response_policy_requeues
        WHERE event_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (event_id,),
    ).fetchone()
    requeue_reason = (
        str(latest_requeue["reason"])
        if latest_requeue is not None
        else "queued_event_with_existing_resolution"
    )
    revision_reason = (
        "The local poyasnitelnaya-brigada skill removed the legacy "
        "ChatGPT dependency"
        if requeue_reason == "local_poyasnitelnaya_brigada_skill_recovery"
        else "Auditable requeue after durable resolution: " + requeue_reason
    )
    return {
        "supersedes_existing_resolution": True,
        "existing_disposition": existing["disposition"],
        "existing_blocker_code": existing["blocker_code"],
        "existing_resolved_at": existing["resolved_at"],
        "requeue_reason": requeue_reason,
        "resolution_revision_reason": revision_reason,
    }


def _enriched_claim_event(
    connection: sqlite3.Connection,
    event: dict[str, Any],
    watcher_config: Any,
) -> dict[str, Any]:
    event_id = str(event["id"])
    try:
        memory = watcher.commenter_history_for_event(
            connection,
            event_id,
            limit=watcher_config.commenter_memory_limit,
        )
    except (KeyError, ValueError):
        memory = _fallback_commenter_memory(event_id)
    recent_status_ids = frozenset(
        str(record["status_id"])
        for record in memory.get("interactions", [])
    )
    try:
        dossier = watcher.author_dossier_for_event(
            connection,
            event_id,
            limit=watcher_config.commenter_memory_limit,
            query_text=None,
            include_recent=False,
            excluded_status_ids=recent_status_ids,
        )
    except (KeyError, ValueError):
        dossier = {
            "event_id": event_id,
            "identity_kind": "unavailable",
            "dossier_contract": watcher.AUTHOR_DOSSIER_CONTRACT,
            "conversation_summaries": [],
            "relevant_interactions": [],
        }
    enriched = {
        **event,
        "commenter_memory": memory,
        "author_dossier": dossier,
    }
    parent_profile = manual_parent_continuation_profile(
        connection,
        event_id,
        configured_user_id=watcher_config.user_id,
    )
    if parent_profile is not None:
        enriched["manual_parent_continuation"] = parent_profile
    recovery = _resolution_recovery(connection, event_id)
    if recovery is not None:
        enriched["resolution_recovery"] = recovery
    return enriched


def _enrich_claim_events(
    config_path: Path,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    watcher_config = watcher.load_config(config_path)
    connection = watcher.connect_database(watcher_config.database)
    try:
        return [
            _enriched_claim_event(connection, event, watcher_config)
            for event in events
        ]
    finally:
        connection.close()


def _finalize_claim(
    config_path: Path,
    browser_owner_cwd: Path,
    config: dict[str, Any],
    policy: inbound_policy.InboundPolicy,
    result: dict[str, Any],
    events: list[dict[str, Any]],
    guard: dict[str, Any],
) -> dict[str, Any]:
    event_ids = [str(event["id"]) for event in events]
    claim_token = str(result["claim_token"])
    clear_handoff_reservation(
        config_path,
        claim_token=claim_token,
        event_ids=event_ids,
    )
    prompt = autopilot_contract.build_prompt(
        events,
        config_path=config_path,
        browser_owner_cwd=browser_owner_cwd,
        config=config,
        policy=policy,
        resource_guard=guard,
    )
    write_health(
        config_path,
        status="handoff_pending",
        event_ids=event_ids,
        claim_token=claim_token,
    )
    return {
        "status": "handoff_pending",
        "dispatch": True,
        "claim_token": claim_token,
        "event_ids": event_ids,
        "prompt": prompt,
        "resource_guard": guard,
    }


def claim(config_path: Path, *, lease_seconds: int) -> dict[str, Any]:
    config = autopilot_dispatch.read_json(config_path)
    policy = inbound_policy.InboundPolicy.from_config(config)
    browser_owner_cwd = autopilot_contract.load_workspace(
        config_path,
        config=config,
    )
    wake_file, state_file = autopilot_dispatch.load_paths(config_path)
    pending_events = autopilot_dispatch.load_wake_events(wake_file)
    route_state, priority_event_ids, excluded_event_ids = route_selection(
        config_path,
        config,
        pending_events,
    )
    guard = resource_guard.check(config_path)
    if guard.get("defer"):
        return _deferred_claim_result(wake_file, guard)
    result = autopilot_dispatch.claim(
        wake_file,
        state_file,
        lease_seconds=lease_seconds,
        max_events=policy.claim_events,
        runtime_id=resource_guard.codex_runtime_id(),
        priority_author_ids=(
            autopilot_dispatch.configured_priority_author_ids(config)
        ),
        priority_event_ids=priority_event_ids,
        excluded_event_ids=excluded_event_ids,
    )
    if not result["dispatch"]:
        return _non_dispatch_claim_result(
            config_path,
            wake_file,
            result,
            guard,
        )
    events = _enrich_claim_events(config_path, list(result["events"]))
    for event in events:
        event["inbound_route_mode"] = route_state["mode"]
    return _finalize_claim(
        config_path,
        browser_owner_cwd,
        config,
        policy,
        result,
        events,
        guard,
    )


def gate(config_path: Path, *, lease_seconds: int) -> dict[str, Any]:
    config = autopilot_dispatch.read_json(config_path)
    policy = inbound_policy.InboundPolicy.from_config(config)
    autopilot_contract.load_workspace(config_path, config=config)
    wake_file, state_file = autopilot_dispatch.load_paths(config_path)
    pending_events = autopilot_dispatch.load_wake_events(wake_file)
    route_state, priority_event_ids, excluded_event_ids = route_selection(
        config_path,
        config,
        pending_events,
    )
    pending_event_ids = [str(event["id"]) for event in pending_events]
    selected_events = autopilot_dispatch.select_claim_events(
        pending_events,
        max_events=policy.claim_events,
        priority_author_ids=(
            autopilot_dispatch.configured_priority_author_ids(config)
        ),
        priority_event_ids=priority_event_ids,
        excluded_event_ids=excluded_event_ids,
    )
    selected_event_ids = [str(event["id"]) for event in selected_events]
    queue = autopilot_dispatch.status(
        wake_file,
        state_file,
        lease_seconds=lease_seconds,
    )
    if not pending_event_ids:
        return {
            "status": "idle",
            "dispatch": False,
            "pending_count": 0,
            "event_ids": [],
        }
    guard = resource_guard.check(config_path)
    if queue["owner_busy"] or queue["leased_count"]:
        return {
            "status": "leased_waiting",
            "dispatch": False,
            "pending_count": len(pending_event_ids),
            "event_ids": pending_event_ids,
            "owner_busy": queue["owner_busy"],
            "leased_count": queue["leased_count"],
            "resource_guard": guard,
        }
    repair_owner = active_supervisor_owner(config_path)
    if repair_owner["owner_busy"]:
        return {
            "status": "repair_waiting",
            "dispatch": False,
            "pending_count": len(pending_event_ids),
            "event_ids": pending_event_ids,
            "repair_incident_id": repair_owner["incident_id"],
            "repair_status": repair_owner["status"],
            "repair_lease_expires_at": repair_owner["lease_expires_at"],
            "resource_guard": guard,
        }
    if guard.get("defer"):
        return {
            "status": "resource_deferred",
            "dispatch": False,
            "pending_count": len(pending_event_ids),
            "event_ids": pending_event_ids,
            "resource_guard": guard,
        }
    if not selected_event_ids:
        paused_ids = sorted(
            set(pending_event_ids).intersection(excluded_event_ids),
            key=int,
        )
        return {
            "status": "route_paused",
            "dispatch": False,
            "pending_count": len(pending_event_ids),
            "eligible_count": 0,
            "paused_count": len(paused_ids),
            "paused_event_ids": paused_ids,
            "event_ids": [],
            "queued_event_ids": pending_event_ids,
            "inbound_route_mode": route_state["mode"],
            "resource_guard": guard,
        }
    return {
        "status": "ready",
        "dispatch": True,
        "pending_count": len(pending_event_ids),
        "event_ids": selected_event_ids,
        "queued_event_ids": pending_event_ids,
        "claim_limit": policy.claim_events,
        "inbound_route_mode": route_state["mode"],
        "resource_guard": guard,
    }


def _recover_claim_event_ids(
    config_path: Path,
    state_file: Path,
    claim_token: str,
) -> list[str]:
    state = autopilot_dispatch.load_state(state_file)
    event_ids = autopilot_dispatch.claim_event_ids(state, claim_token)
    if event_ids:
        return event_ids
    path = health_path(config_path)
    previous = autopilot_dispatch.read_json(path) if path.exists() else {}
    if previous.get("claim_token") != claim_token:
        return []
    return sorted(
        (str(value) for value in previous.get("event_ids", [])),
        key=int,
    )


def _pending_event_ids(wake_file: Path) -> list[str]:
    return [
        str(event["id"])
        for event in autopilot_dispatch.load_wake_events(wake_file)
    ]


def _require_not_pending(
    wake_file: Path,
    event_ids: list[str],
    *,
    message_prefix: str,
) -> list[str]:
    pending_ids = _pending_event_ids(wake_file)
    unresolved = sorted(set(event_ids).intersection(pending_ids), key=int)
    if unresolved:
        raise ValueError(message_prefix + ", ".join(unresolved))
    return pending_ids


def _trigger_next_dispatch(
    config_path: Path,
    pending_event_ids: list[str],
) -> dict[str, Any]:
    try:
        return event_dispatch.trigger_pending_events(
            config_path,
            pending_event_ids,
        )
    except (OSError, TypeError, ValueError) as error:
        return {
            "status": "dispatch_trigger_failed",
            "triggered": False,
            "event_ids": pending_event_ids,
            "reason": event_dispatch.CLAIM_COMPLETED_REASON,
            "error": f"{type(error).__name__}: {error}",
        }


def _completed_result(
    config_path: Path,
    *,
    claim_token: str,
    event_ids: list[str],
    pending_event_ids: list[str],
    warning: str | None,
    evidence_manifest: dict[str, Any],
) -> dict[str, Any]:
    status = "completed_with_warning" if warning else "completed"
    write_health(
        config_path,
        status=status,
        event_ids=event_ids,
        claim_token=claim_token,
        error=warning,
    )
    return {
        "status": status,
        "claim_token": claim_token,
        "event_ids": event_ids,
        "pending_count": len(pending_event_ids),
        "next_dispatch": _trigger_next_dispatch(
            config_path,
            pending_event_ids,
        ),
        "warning": warning,
        "evidence_manifest": evidence_manifest,
    }


def mark_completed(
    config_path: Path,
    claim_token: str,
    *,
    warning: str | None = None,
) -> dict[str, Any]:
    wake_file, state_file = autopilot_dispatch.load_paths(config_path)
    event_ids = _recover_claim_event_ids(
        config_path,
        state_file,
        claim_token,
    )
    if not event_ids:
        raise ValueError("claim token is not active or recoverable")
    pending_event_ids = _require_not_pending(
        wake_file,
        event_ids,
        message_prefix="claim still contains pending events: ",
    )
    browser_owner_claim.require_durable_resolutions(
        config_path,
        event_ids,
    )
    evidence_manifest = browser_owner_claim.verify_evidence_manifest(
        config_path,
        claim_token=claim_token,
        event_ids=event_ids,
    )
    finished = autopilot_dispatch.finish(state_file, claim_token)
    if finished["finished"] != event_ids:
        state_warning = (
            "dispatcher cleared claim state after durable resolution; "
            "completion reconciled against the database"
        )
        combined_warning = (
            f"{warning}; {state_warning}" if warning else state_warning
        )
        reconciled = reconcile_completed(
            config_path,
            event_ids,
            warning=combined_warning,
        )
        reconciled["requested_claim_token"] = claim_token
        reconciled["reconciled"] = True
        reconciled["evidence_manifest"] = evidence_manifest
        return reconciled
    return _completed_result(
        config_path,
        claim_token=claim_token,
        event_ids=event_ids,
        pending_event_ids=pending_event_ids,
        warning=warning,
        evidence_manifest=evidence_manifest,
    )


def reconcile_completed(
    config_path: Path,
    event_ids: list[str],
    *,
    warning: str | None = None,
) -> dict[str, Any]:
    requested = sorted(set(event_ids), key=int)
    if not requested or any(not event_id.isdigit() for event_id in requested):
        raise ValueError("event ids must be numeric")
    wake_file, _ = autopilot_dispatch.load_paths(config_path)
    pending_ids = {
        str(event["id"])
        for event in autopilot_dispatch.load_wake_events(wake_file)
    }
    still_pending = sorted(pending_ids.intersection(requested), key=int)
    if still_pending:
        raise ValueError(
            "events are still pending: " + ", ".join(still_pending)
        )
    browser_owner_claim.require_durable_resolutions(
        config_path,
        requested,
    )
    status = "completed_with_warning" if warning else "completed"
    recovery_token = "reconciled:" + autopilot_dispatch.isoformat()
    pending_event_ids = sorted(pending_ids, key=int)
    try:
        next_dispatch = event_dispatch.trigger_pending_events(
            config_path,
            pending_event_ids,
        )
    except (OSError, TypeError, ValueError) as error:
        next_dispatch = {
            "status": "dispatch_trigger_failed",
            "triggered": False,
            "event_ids": pending_event_ids,
            "reason": event_dispatch.CLAIM_COMPLETED_REASON,
            "error": f"{type(error).__name__}: {error}",
        }
    write_health(
        config_path,
        status=status,
        event_ids=requested,
        claim_token=recovery_token,
        error=warning,
    )
    return {
        "status": status,
        "claim_token": recovery_token,
        "event_ids": requested,
        "pending_count": len(pending_ids),
        "next_dispatch": next_dispatch,
        "warning": warning,
    }


def mark_started(config_path: Path, claim_token: str) -> dict[str, Any]:
    _, state_file = autopilot_dispatch.load_paths(config_path)
    state = autopilot_dispatch.load_state(state_file)
    event_ids = autopilot_dispatch.claim_event_ids(state, claim_token)
    if not event_ids:
        raise ValueError("claim token is not active")
    write_health(
        config_path,
        status="work_in_progress",
        event_ids=event_ids,
        claim_token=claim_token,
    )
    return {
        "status": "work_in_progress",
        "claim_token": claim_token,
        "event_ids": event_ids,
    }


def renew_claim(
    config_path: Path,
    claim_token: str,
    *,
    lease_seconds: int,
) -> dict[str, Any]:
    """Renew one exact Browser-owner claim without absorbing new events."""

    _, state_file = autopilot_dispatch.load_paths(config_path)
    renewed = autopilot_dispatch.renew(
        state_file,
        claim_token,
        lease_seconds=lease_seconds,
    )
    event_ids = [str(value) for value in renewed["event_ids"]]
    write_health(
        config_path,
        status="work_in_progress",
        event_ids=event_ids,
        claim_token=claim_token,
    )
    return {
        "status": "work_in_progress",
        **renewed,
    }


def record_route_classification(
    config_path: Path,
    claim_token: str,
    event_id: str,
    route: str,
) -> dict[str, Any]:
    """Persist one live route and defer local-max during the simple wave."""

    config = autopilot_dispatch.read_json(config_path)
    wake_file, state_file = autopilot_dispatch.load_paths(config_path)
    dispatch_state = autopilot_dispatch.load_state(state_file)
    active_ids = autopilot_dispatch.claim_event_ids(
        dispatch_state,
        claim_token,
    )
    path = route_control_path(config_path, config)
    existing_state = inbound_route_control.load(path)
    existing = existing_state["routes"].get(event_id)
    if event_id not in active_ids:
        if existing is not None and existing["route"] == route:
            return {
                "status": "route_already_recorded",
                "claim_token": claim_token,
                "event_id": event_id,
                "route": route,
                "inbound_route_mode": existing_state["mode"],
                "active_event_ids": active_ids,
            }
        raise ValueError("event is not in the active claim")
    route_state = inbound_route_control.record_route(
        path,
        event_id=event_id,
        route=route,
        classified_at=autopilot_dispatch.isoformat(),
    )
    result: dict[str, Any] = {
        "status": "route_recorded",
        "claim_token": claim_token,
        "event_id": event_id,
        "route": route,
        "inbound_route_mode": route_state["mode"],
        "active_event_ids": active_ids,
    }
    if (
        route_state["mode"] != inbound_route_control.SIMPLE_WAVE_MODE
        or route != "local-max"
    ):
        return result

    deferred = autopilot_dispatch.defer_claim_events(
        state_file,
        claim_token,
        [event_id],
    )
    remaining = list(deferred["remaining_event_ids"])
    pending_ids = _pending_event_ids(wake_file)
    status = "local_max_deferred"
    write_health(
        config_path,
        status=status,
        event_ids=remaining or [event_id],
        claim_token=claim_token,
    )
    result.update(
        {
            "status": status,
            "deferred_event_ids": [event_id],
            "remaining_event_ids": remaining,
            "owner_released": bool(deferred["owner_released"]),
            "pending_count": len(pending_ids),
        }
    )
    if deferred["owner_released"]:
        result["next_dispatch"] = _trigger_next_dispatch(
            config_path,
            pending_ids,
        )
    return result


def start_simple_wave(config_path: Path) -> dict[str, Any]:
    """Snapshot the current queue for one ordinary-reply-first drain wave."""

    config = autopilot_dispatch.read_json(config_path)
    wake_file, _ = autopilot_dispatch.load_paths(config_path)
    pending = autopilot_dispatch.load_wake_events(wake_file)
    pending_ids = [str(event["id"]) for event in pending]
    path = route_control_path(config_path, config)
    state = inbound_route_control.set_mode(
        path,
        mode=inbound_route_control.SIMPLE_WAVE_MODE,
        updated_at=autopilot_dispatch.isoformat(),
        updated_by="Alex",
        pending_event_ids=pending_ids,
    )
    return {
        "status": "simple_wave_started",
        "mode": state["mode"],
        "wave_event_ids": state["simple_wave_event_ids"],
        "wave_event_count": len(state["simple_wave_event_ids"]),
    }


def start_author_focus(
    config_path: Path,
    author_ids: list[str],
) -> dict[str, Any]:
    """Temporarily claim only events from immutable selected author IDs."""

    config = autopilot_dispatch.read_json(config_path)
    path = route_control_path(config_path, config)
    state = inbound_route_control.set_mode(
        path,
        mode=inbound_route_control.AUTHOR_FOCUS_MODE,
        updated_at=autopilot_dispatch.isoformat(),
        updated_by="Alex",
        focus_author_ids=author_ids,
    )
    return {
        "status": "author_focus_started",
        "mode": state["mode"],
        "focus_author_ids": state["focus_author_ids"],
    }


def stop_author_focus(config_path: Path) -> dict[str, Any]:
    """Restore normal FIFO without rewriting or resolving queued events."""

    config = autopilot_dispatch.read_json(config_path)
    path = route_control_path(config_path, config)
    state = inbound_route_control.set_mode(
        path,
        mode=inbound_route_control.NORMAL_MODE,
        updated_at=autopilot_dispatch.isoformat(),
        updated_by="Alex",
    )
    return {
        "status": "author_focus_stopped",
        "mode": state["mode"],
    }


def mark_failed(
    config_path: Path,
    claim_token: str,
    error: str,
) -> dict[str, Any]:
    _, state_file = autopilot_dispatch.load_paths(config_path)
    released = autopilot_dispatch.release(state_file, claim_token)
    write_health(
        config_path,
        status="handoff_failed",
        event_ids=list(released["released"]),
        claim_token=claim_token,
        error=error,
    )
    return {
        "status": "handoff_failed",
        "claim_token": claim_token,
        "released": released["released"],
        "error": error,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lease-seconds", type=int, default=1800)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("gate")
    subparsers.add_parser("reserve-handoff")
    release_handoff = subparsers.add_parser("release-handoff")
    release_handoff.add_argument("--reservation-token", required=True)
    release_handoff.add_argument("--reason", required=True)
    subparsers.add_parser("claim")
    started = subparsers.add_parser("started")
    started.add_argument("--claim-token", required=True)
    renew = subparsers.add_parser("renew")
    renew.add_argument("--claim-token", required=True)
    classified = subparsers.add_parser("route-classified")
    classified.add_argument("--claim-token", required=True)
    classified.add_argument("--event-id", required=True)
    classified.add_argument(
        "--route",
        required=True,
        choices=sorted(inbound_route_control.SUPPORTED_ROUTES),
    )
    subparsers.add_parser("start-simple-wave")
    author_focus = subparsers.add_parser("start-author-focus")
    author_focus.add_argument("--author-id", action="append", required=True)
    subparsers.add_parser("stop-author-focus")
    completed = subparsers.add_parser("completed")
    completed.add_argument("--claim-token", required=True)
    completed.add_argument("--warning")
    reconcile = subparsers.add_parser("reconcile-completed")
    reconcile.add_argument("--event-id", action="append", required=True)
    reconcile.add_argument("--warning")
    failed = subparsers.add_parser("failed")
    failed.add_argument("--claim-token", required=True)
    failed.add_argument("--error", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "gate":
        result = gate(
            arguments.config,
            lease_seconds=arguments.lease_seconds,
        )
    elif arguments.command == "reserve-handoff":
        result = reserve_handoff(
            arguments.config,
            lease_seconds=arguments.lease_seconds,
        )
    elif arguments.command == "release-handoff":
        result = release_handoff_reservation(
            arguments.config,
            reservation_token=arguments.reservation_token,
            reason=arguments.reason,
        )
    elif arguments.command == "claim":
        result = claim(
            arguments.config,
            lease_seconds=arguments.lease_seconds,
        )
    elif arguments.command == "started":
        result = mark_started(arguments.config, arguments.claim_token)
    elif arguments.command == "renew":
        result = renew_claim(
            arguments.config,
            arguments.claim_token,
            lease_seconds=arguments.lease_seconds,
        )
    elif arguments.command == "route-classified":
        result = record_route_classification(
            arguments.config,
            arguments.claim_token,
            arguments.event_id,
            arguments.route,
        )
    elif arguments.command == "start-simple-wave":
        result = start_simple_wave(arguments.config)
    elif arguments.command == "start-author-focus":
        result = start_author_focus(
            arguments.config,
            arguments.author_id,
        )
    elif arguments.command == "stop-author-focus":
        result = stop_author_focus(arguments.config)
    elif arguments.command == "completed":
        result = mark_completed(
            arguments.config,
            arguments.claim_token,
            warning=arguments.warning,
        )
    elif arguments.command == "reconcile-completed":
        result = reconcile_completed(
            arguments.config,
            arguments.event_id,
            warning=arguments.warning,
        )
    else:
        result = mark_failed(
            arguments.config,
            arguments.claim_token,
            arguments.error,
        )
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
