#!/usr/bin/env python3
"""Claim a token-free X queue for one Codex Desktop worker run."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from scripts import autopilot_contract, autopilot_dispatch, resource_guard
except ModuleNotFoundError:
    import autopilot_contract  # type: ignore[no-redef]
    import autopilot_dispatch  # type: ignore[no-redef]
    import resource_guard  # type: ignore[no-redef]

try:
    import xmention_watcher as watcher
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import xmention_watcher as watcher  # type: ignore[no-redef]


LOCAL_MAX_TURN_PROVENANCE = {
    "pro",
    "local_sol_max_and_live_x_dom",
    "poyasnitelnaya_brigada_local_sol_max",
    "poyasnitelnaya_brigada_local_sol_max_and_live_x_dom",
}
SOURCE_URL_PATTERN = re.compile(r"https?://[^\s]+")


def replied_to_status_id(payload: dict[str, Any]) -> str | None:
    references = payload.get("referenced_tweets") or []
    if not isinstance(references, list):
        return None
    for reference in references:
        if not isinstance(reference, dict):
            continue
        if reference.get("type") != "replied_to":
            continue
        status_id = str(reference.get("id", "")).strip()
        if status_id.isdigit():
            return status_id
    return None


def manual_parent_continuation_profile(
    connection: sqlite3.Connection,
    event_id: str,
    *,
    configured_user_id: str,
) -> dict[str, Any] | None:
    """Classify how to continue an exact Alex parent without guessing origin."""

    event = connection.execute(
        """
        SELECT in_reply_to_user_id, payload_json
        FROM events
        WHERE event_id = ?
        """,
        (event_id,),
    ).fetchone()
    if event is None:
        return None
    try:
        payload = json.loads(str(event["payload_json"]))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    parent_status_id = replied_to_status_id(payload)
    if parent_status_id is None:
        return None
    is_direct_reply = (
        bool(configured_user_id)
        and str(event["in_reply_to_user_id"] or "") == configured_user_id
    )
    parent = connection.execute(
        """
        SELECT actor, exact_text, provenance
        FROM conversation_turns
        WHERE status_id = ?
        """,
        (parent_status_id,),
    ).fetchone()
    if parent is None:
        if not is_direct_reply:
            return None
        return {
            "parent_status_id": parent_status_id,
            "parent_history_status": "missing_exact_alex_parent",
            "parent_origin_provenance": "unknown",
            "origin_proven": False,
            "recommended_route": "pending_exact_parent_restore",
            "required_action": (
                "restore_exact_live_x_parent_then_classify_adaptively"
            ),
            "chatgpt_web_allowed": False,
        }
    if str(parent["actor"]) != "alex":
        return None

    exact_text = str(parent["exact_text"] or "")
    provenance = str(parent["provenance"] or "").strip()
    paragraphs = [
        value.strip()
        for value in re.split(r"\n\s*\n", exact_text)
        if value.strip()
    ]
    source_url_count = len(SOURCE_URL_PATTERN.findall(exact_text))
    proven_local_max = provenance in LOCAL_MAX_TURN_PROVENANCE
    substantive = (
        len(exact_text) >= 500
        or len(paragraphs) >= 3
        or source_url_count >= 1
    )
    adaptive_local_max = proven_local_max or substantive
    if proven_local_max:
        basis = "proven_local_max_origin"
    elif substantive:
        basis = "adaptive_manual_parent_content"
    else:
        basis = "concise_manual_parent_content"
    return {
        "parent_status_id": parent_status_id,
        "parent_history_status": "exact_alex_parent",
        "parent_origin_provenance": provenance or "manual_unknown",
        "origin_proven": bool(provenance),
        "content_profile": {
            "code_points": len(exact_text),
            "paragraph_count": len(paragraphs),
            "source_url_count": source_url_count,
            "substantive": substantive,
        },
        "adaptive_local_max": adaptive_local_max,
        "recommended_route": "local-max" if adaptive_local_max else "short",
        "continuation_basis": basis,
        "required_action": (
            "use_complete_local_history_and_poyasnitelnaya_brigada"
            if adaptive_local_max
            else "use_complete_local_history_and_sol_short"
        ),
        "chatgpt_web_allowed": False,
    }


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


def max_claim_events(config_path: Path) -> int:
    config = autopilot_dispatch.read_json(config_path)
    value = int(
        config.get(
            "autopilot_max_claim_events",
            autopilot_dispatch.DEFAULT_MAX_CLAIM_EVENTS,
        )
    )
    if value <= 0:
        raise ValueError("autopilot_max_claim_events must be positive")
    return value


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


def claim(config_path: Path, *, lease_seconds: int) -> dict[str, Any]:
    browser_owner_cwd = autopilot_contract.load_workspace(config_path)
    wake_file, state_file = autopilot_dispatch.load_paths(config_path)
    guard = resource_guard.check(config_path)
    if guard.get("defer"):
        pending = autopilot_dispatch.load_wake_events(wake_file)
        return {
            "status": "memory_deferred",
            "dispatch": False,
            "pending_count": len(pending),
            "event_ids": [str(event["id"]) for event in pending],
            "resource_guard": guard,
        }
    result = autopilot_dispatch.claim(
        wake_file,
        state_file,
        lease_seconds=lease_seconds,
        max_events=max_claim_events(config_path),
        runtime_id=resource_guard.codex_runtime_id(),
    )
    if not result["dispatch"]:
        pending = autopilot_dispatch.load_wake_events(wake_file)
        if result.get("owner_busy"):
            path = health_path(config_path)
            previous = (
                autopilot_dispatch.read_json(path) if path.exists() else {}
            )
            status = str(previous.get("status", "leased_waiting"))
            return {
                "status": status,
                **result,
                "resource_guard": guard,
            }
        status = "leased_waiting" if pending else "idle"
        write_health(
            config_path,
            status=status,
            event_ids=[str(event["id"]) for event in pending],
        )
        return {"status": status, **result, "resource_guard": guard}

    events = list(result["events"])
    watcher_config = watcher.load_config(config_path)
    connection = watcher.connect_database(watcher_config.database)
    try:
        enriched_events: list[dict[str, Any]] = []
        for event in events:
            event_id = str(event["id"])
            try:
                memory = watcher.commenter_history_for_event(
                    connection,
                    event_id,
                    limit=watcher_config.commenter_memory_limit,
                )
            except (KeyError, ValueError):
                memory = {
                    "event_id": str(event["id"]),
                    "identity_kind": "unavailable",
                    "total_prior_interactions": 0,
                    "returned_interactions": 0,
                    "interactions": [],
                }
            enriched_event = {
                **event,
                "commenter_memory": memory,
            }
            parent_profile = manual_parent_continuation_profile(
                connection,
                event_id,
                configured_user_id=watcher_config.user_id,
            )
            if parent_profile is not None:
                enriched_event["manual_parent_continuation"] = parent_profile
            existing_resolution = connection.execute(
                """
                SELECT disposition, reason, blocker_code, resolved_at
                FROM event_resolutions
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
            if existing_resolution is not None:
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
                if (
                    requeue_reason
                    == "local_poyasnitelnaya_brigada_skill_recovery"
                ):
                    revision_reason = (
                        "The local poyasnitelnaya-brigada skill removed "
                        "the legacy ChatGPT dependency"
                    )
                else:
                    revision_reason = (
                        "Auditable requeue after durable resolution: "
                        + requeue_reason
                    )
                enriched_event["resolution_recovery"] = {
                    "supersedes_existing_resolution": True,
                    "existing_disposition": existing_resolution[
                        "disposition"
                    ],
                    "existing_blocker_code": existing_resolution[
                        "blocker_code"
                    ],
                    "existing_resolved_at": existing_resolution[
                        "resolved_at"
                    ],
                    "requeue_reason": requeue_reason,
                    "resolution_revision_reason": revision_reason,
                }
            enriched_events.append(
                enriched_event
            )
        events = enriched_events
    finally:
        connection.close()
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


def gate(config_path: Path, *, lease_seconds: int) -> dict[str, Any]:
    autopilot_contract.load_workspace(config_path)
    wake_file, state_file = autopilot_dispatch.load_paths(config_path)
    pending_events = autopilot_dispatch.load_wake_events(wake_file)
    pending_event_ids = [str(event["id"]) for event in pending_events]
    selected_events = autopilot_dispatch.select_claim_events(
        pending_events,
        max_events=max_claim_events(config_path),
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
    return {
        "status": "ready",
        "dispatch": True,
        "pending_count": len(pending_event_ids),
        "event_ids": selected_event_ids,
        "queued_event_ids": pending_event_ids,
        "claim_limit": max_claim_events(config_path),
        "resource_guard": guard,
    }


def mark_completed(
    config_path: Path,
    claim_token: str,
    *,
    warning: str | None = None,
) -> dict[str, Any]:
    wake_file, state_file = autopilot_dispatch.load_paths(config_path)
    state = autopilot_dispatch.load_state(state_file)
    event_ids = sorted(
        (
            event_id
            for event_id, record in state["events"].items()
            if isinstance(record, dict)
            and record.get("claim_token") == claim_token
        ),
        key=int,
    )
    if not event_ids:
        previous = {}
        path = health_path(config_path)
        if path.exists():
            previous = autopilot_dispatch.read_json(path)
        if previous.get("claim_token") == claim_token:
            event_ids = sorted(
                (str(value) for value in previous.get("event_ids", [])),
                key=int,
            )
    if not event_ids:
        raise ValueError("claim token is not active or recoverable")
    pending_ids = {
        str(event["id"])
        for event in autopilot_dispatch.load_wake_events(wake_file)
    }
    unresolved = sorted(
        (event_id for event_id in event_ids if event_id in pending_ids),
        key=int,
    )
    if unresolved:
        raise ValueError(
            "claim still contains pending events: " + ", ".join(unresolved)
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
        return reconciled
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
        "pending_count": len(autopilot_dispatch.load_wake_events(wake_file)),
        "warning": warning,
    }


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
    raw_config = autopilot_dispatch.read_json(config_path)
    database = resolve_path(
        config_path,
        str(raw_config.get("database", "var/watcher.sqlite3")),
    )
    placeholders = ",".join("?" for _ in requested)
    with closing(
        sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    ) as connection:
        rows = connection.execute(
            f"""
            SELECT event_id
            FROM event_resolutions
            WHERE event_id IN ({placeholders})
            """,
            requested,
        ).fetchall()
    resolved = sorted((str(row[0]) for row in rows), key=int)
    if resolved != requested:
        missing = sorted(set(requested) - set(resolved), key=int)
        raise ValueError(
            "events lack durable resolution: " + ", ".join(missing)
        )
    status = "completed_with_warning" if warning else "completed"
    recovery_token = "reconciled:" + autopilot_dispatch.isoformat()
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
        "warning": warning,
    }


def mark_started(config_path: Path, claim_token: str) -> dict[str, Any]:
    _, state_file = autopilot_dispatch.load_paths(config_path)
    state = autopilot_dispatch.load_state(state_file)
    event_ids = sorted(
        (
            event_id
            for event_id, record in state["events"].items()
            if isinstance(record, dict)
            and record.get("claim_token") == claim_token
        ),
        key=int,
    )
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
