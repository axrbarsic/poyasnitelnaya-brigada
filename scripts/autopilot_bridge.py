#!/usr/bin/env python3
"""Claim a token-free X queue for one Codex Desktop worker run."""

from __future__ import annotations

import argparse
import json
import os
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


def codex_state_database_candidates(config: dict[str, Any]) -> list[Path]:
    explicit = str(config.get("codex_state_database", "")).strip()
    if explicit:
        return [Path(explicit).expanduser()]
    codex_home = Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).expanduser()
    return sorted(
        codex_home.glob("state_*.sqlite"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def browser_owner_rollout_path(
    config: dict[str, Any],
    *,
    thread_id: str,
) -> Path | None:
    for database_path in codex_state_database_candidates(config):
        if not database_path.is_file():
            continue
        try:
            database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
            with closing(
                sqlite3.connect(database_uri, uri=True)
            ) as connection:
                row = connection.execute(
                    "SELECT rollout_path FROM threads WHERE id = ?",
                    (thread_id,),
                ).fetchone()
        except (OSError, sqlite3.Error):
            continue
        if row is None or not isinstance(row[0], str) or not row[0]:
            continue
        rollout_path = Path(row[0]).expanduser()
        if not rollout_path.is_absolute():
            rollout_path = database_path.parent / rollout_path
        return rollout_path.resolve()
    return None


def browser_owner_activity(
    config_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read the durable owner rollout without loading or waking the thread."""

    config = autopilot_dispatch.read_json(config_path)
    thread_id = str(config.get("browser_owner_thread_id", "")).strip()
    if not thread_id:
        return {"enabled": False, "defer": False, "status": "not_configured"}
    grace_seconds = int(
        config.get("command_center_idle_grace_seconds", 60)
    )
    if grace_seconds < 0:
        raise ValueError("command center idle grace seconds must not be negative")
    rollout_path = browser_owner_rollout_path(
        config,
        thread_id=thread_id,
    )
    if rollout_path is None or not rollout_path.is_file():
        return {
            "enabled": True,
            "defer": True,
            "status": "owner_thread_status_unavailable",
            "thread_id": thread_id,
        }

    latest_turn_id: str | None = None
    latest_turn_started_at = None
    latest_turn_terminal = False
    last_activity_at = None
    try:
        with rollout_path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return {
                        "enabled": True,
                        "defer": True,
                        "status": "owner_thread_status_unavailable",
                        "thread_id": thread_id,
                    }
                if not isinstance(record, dict):
                    continue
                timestamp = autopilot_dispatch.parse_time(
                    str(record.get("timestamp", ""))
                )
                if timestamp is not None:
                    last_activity_at = timestamp
                if record.get("type") != "event_msg":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                event_type = str(payload.get("type", ""))
                turn_id = str(
                    payload.get("turn_id")
                    or payload.get("turnId")
                    or payload.get("id")
                    or ""
                )
                if event_type == "task_started" and turn_id:
                    latest_turn_id = turn_id
                    latest_turn_started_at = timestamp
                    latest_turn_terminal = False
                elif (
                    event_type in {"task_complete", "turn_aborted"}
                    and turn_id
                    and turn_id == latest_turn_id
                ):
                    latest_turn_terminal = True
    except OSError:
        return {
            "enabled": True,
            "defer": True,
            "status": "owner_thread_status_unavailable",
            "thread_id": thread_id,
        }

    if latest_turn_id is None or latest_turn_started_at is None:
        return {
            "enabled": True,
            "defer": True,
            "status": "owner_thread_status_unavailable",
            "thread_id": thread_id,
        }
    if not latest_turn_terminal:
        return {
            "enabled": True,
            "defer": True,
            "status": "owner_thread_active",
            "thread_id": thread_id,
            "turn_id": latest_turn_id,
            "turn_started_at": autopilot_dispatch.isoformat(
                latest_turn_started_at
            ),
        }

    current = now or autopilot_dispatch.utc_now()
    activity_age = (
        max(0.0, (current - last_activity_at).total_seconds())
        if last_activity_at is not None
        else 0.0
    )
    if activity_age < grace_seconds:
        return {
            "enabled": True,
            "defer": True,
            "status": "owner_thread_cooldown",
            "thread_id": thread_id,
            "turn_id": latest_turn_id,
            "activity_age_seconds": round(activity_age, 1),
            "grace_seconds": grace_seconds,
        }
    return {
        "enabled": True,
        "defer": False,
        "status": "owner_thread_idle",
        "thread_id": thread_id,
        "turn_id": latest_turn_id,
        "activity_age_seconds": round(activity_age, 1),
        "grace_seconds": grace_seconds,
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
    owner_activity = browser_owner_activity(config_path)
    if owner_activity.get("defer"):
        return {
            **ready,
            "dispatch": False,
            "status": owner_activity["status"],
            "owner_activity": owner_activity,
        }
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
            try:
                memory = watcher.commenter_history_for_event(
                    connection,
                    str(event["id"]),
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
            enriched_events.append(
                {
                    **event,
                    "commenter_memory": memory,
                }
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
    queue = autopilot_dispatch.status(
        wake_file,
        state_file,
        lease_seconds=lease_seconds,
    )
    event_ids = [str(value) for value in queue["pending_ids"]]
    if not event_ids:
        return {
            "status": "idle",
            "dispatch": False,
            "pending_count": 0,
            "event_ids": [],
        }
    guard = resource_guard.check(config_path)
    if guard.get("defer"):
        return {
            "status": "resource_deferred",
            "dispatch": False,
            "pending_count": len(event_ids),
            "event_ids": event_ids,
            "resource_guard": guard,
        }
    if queue["owner_busy"] or queue["leased_count"]:
        return {
            "status": "leased_waiting",
            "dispatch": False,
            "pending_count": len(event_ids),
            "event_ids": event_ids,
            "owner_busy": queue["owner_busy"],
            "leased_count": queue["leased_count"],
            "resource_guard": guard,
        }
    return {
        "status": "ready",
        "dispatch": True,
        "pending_count": len(event_ids),
        "event_ids": event_ids,
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
        raise ValueError("claim state changed before completion")
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
        "pending_count": 0,
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
