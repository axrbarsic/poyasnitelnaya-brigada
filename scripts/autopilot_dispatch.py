#!/usr/bin/env python3
"""Atomically claim queued X events for a Codex autopilot wake."""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import sys
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    from scripts import inbound_policy, json_contract
except ModuleNotFoundError:
    import inbound_policy  # type: ignore[no-redef]
    import json_contract  # type: ignore[no-redef]

STATE_VERSION = 2
LEGACY_STATE_VERSION = 1
DEFAULT_MAX_CLAIM_EVENTS = inbound_policy.DEFAULT_MAX_CLAIM_EVENTS
X_STATUS_PATH = re.compile(
    r"^/(?:[A-Za-z0-9_]{1,15}|i/web)/status/([0-9]{1,19})$"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def read_json(path: Path) -> dict[str, Any]:
    return json_contract.read_object(path)


atomic_write_json = json_contract.atomic_write


def resolve_path(config_path: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return (config_path.resolve().parent / candidate).resolve()


def load_paths(config_path: Path) -> tuple[Path, Path]:
    config = read_json(config_path)
    wake_file = resolve_path(
        config_path,
        str(config.get("wake_file", "var/wake-request.json")),
    )
    state_file = resolve_path(
        config_path,
        str(config.get("autopilot_state_file", "var/autopilot-dispatch.json")),
    )
    return wake_file, state_file


def configured_priority_author_ids(config: dict[str, Any]) -> frozenset[str]:
    raw_ids = config.get("inbound_priority_author_ids", [])
    if not isinstance(raw_ids, list):
        raise ValueError("inbound_priority_author_ids must be a list")
    result: set[str] = set()
    for raw_id in raw_ids:
        author_id = str(raw_id)
        if not author_id.isdigit() or len(author_id) > 19:
            raise ValueError("inbound_priority_author_ids must be numeric")
        result.add(author_id)
    return frozenset(result)


def _validated_wake_event(
    raw_event: Any,
    seen: set[str],
) -> dict[str, Any]:
    if not isinstance(raw_event, dict):
        raise ValueError("wake-request event must be an object")
    event_id = str(
        raw_event.get("event_id", raw_event.get("id", ""))
    ).strip()
    url = str(raw_event.get("event_url", raw_event.get("url", ""))).strip()
    parsed_url = urllib.parse.urlsplit(url)
    path_match = X_STATUS_PATH.fullmatch(parsed_url.path)
    if (
        not event_id.isdigit()
        or parsed_url.scheme != "https"
        or parsed_url.hostname != "x.com"
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.port is not None
        or parsed_url.query
        or parsed_url.fragment
        or path_match is None
        or path_match.group(1) != event_id
    ):
        raise ValueError("wake-request event must contain a numeric id and X URL")
    username = raw_event.get("username")
    if username is not None and re.fullmatch(
        r"[A-Za-z0-9_]{1,15}",
        str(username),
    ) is None:
        raise ValueError("wake-request username must be a valid X handle")
    author_id = raw_event.get("author_id")
    if author_id is not None and (
        not str(author_id).isdigit() or len(str(author_id)) > 19
    ):
        raise ValueError("wake-request author_id must be numeric")
    conversation_id = raw_event.get("conversation_id")
    if conversation_id is not None and not str(conversation_id).isdigit():
        raise ValueError("wake-request conversation_id must be numeric")
    if event_id in seen:
        raise ValueError(f"wake-request contains duplicate event {event_id}")
    seen.add(event_id)
    return {
        "id": event_id,
        "url": url,
        "author_id": str(author_id) if author_id is not None else None,
        "username": username,
        "conversation_id": conversation_id,
        "created_at": raw_event.get("created_at"),
        "first_seen_at": raw_event.get("first_seen_at"),
        "is_reply": bool(raw_event.get("is_reply")),
    }


def load_wake_events(path: Path) -> list[dict[str, Any]]:
    with locked_wake(path):
        payload = read_json(path)
    events = payload.get("events")
    pending_count = payload.get("pending_count")
    if not isinstance(events, list):
        raise ValueError("wake-request events must be a list")
    if not isinstance(pending_count, int) or pending_count < 0:
        raise ValueError("wake-request pending_count must be a non-negative integer")
    if pending_count != len(events):
        raise ValueError("wake-request pending_count does not match events length")

    seen: set[str] = set()
    return [_validated_wake_event(raw_event, seen) for raw_event in events]


def _default_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "attempts": {}, "owner": None}


def _event_id(value: Any, *, field: str) -> str:
    event_id = str(value or "")
    if not event_id.isdigit() or len(event_id) > 19:
        raise ValueError(f"{field} contains an invalid event ID")
    return event_id


def _validated_attempts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError("autopilot dispatcher attempts must be an object")
    attempts: dict[str, int] = {}
    for raw_event_id, raw_count in value.items():
        event_id = _event_id(raw_event_id, field="dispatcher attempts")
        if not isinstance(raw_count, int) or isinstance(raw_count, bool):
            raise ValueError("dispatcher attempt count must be an integer")
        if raw_count < 0:
            raise ValueError("dispatcher attempt count must not be negative")
        attempts[event_id] = raw_count
    return attempts


def _validated_owner(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("autopilot dispatcher owner must be an object or null")
    claim_token = str(value.get("claim_token") or "").strip()
    if not claim_token:
        raise ValueError("autopilot dispatcher owner lacks a claim token")
    raw_event_ids = value.get("event_ids")
    if not isinstance(raw_event_ids, list):
        raise ValueError("autopilot dispatcher owner event_ids must be an array")
    event_ids = [
        _event_id(item, field="dispatcher owner event_ids")
        for item in raw_event_ids
    ]
    if not event_ids or len(event_ids) != len(set(event_ids)):
        raise ValueError("autopilot dispatcher owner event_ids are invalid")
    claimed_at = str(value.get("claimed_at") or "").strip()
    if parse_time(claimed_at) is None:
        raise ValueError("autopilot dispatcher owner claimed_at is invalid")
    lease_expires_at = value.get("lease_expires_at")
    if lease_expires_at is not None and parse_time(str(lease_expires_at)) is None:
        raise ValueError("autopilot dispatcher owner lease expiry is invalid")
    runtime_id = value.get("runtime_id")
    if runtime_id is not None and not str(runtime_id).strip():
        raise ValueError("autopilot dispatcher owner runtime_id is invalid")
    owner = dict(value)
    owner["claim_token"] = claim_token
    owner["claimed_at"] = claimed_at
    owner["event_ids"] = event_ids
    if runtime_id is not None:
        owner["runtime_id"] = str(runtime_id).strip()
    return owner


def _legacy_owner(events: dict[str, Any]) -> dict[str, Any] | None:
    leases: dict[str, list[tuple[str, datetime]]] = {}
    for raw_event_id, record in events.items():
        event_id = _event_id(raw_event_id, field="legacy dispatcher events")
        if not isinstance(record, dict):
            raise ValueError("legacy dispatcher event record must be an object")
        token = str(record.get("claim_token") or "").strip()
        dispatched_at = parse_time(str(record.get("last_dispatched_at") or ""))
        if not token and dispatched_at is None:
            continue
        if not token or dispatched_at is None:
            raise ValueError("legacy dispatcher lease is incomplete")
        leases.setdefault(token, []).append((event_id, dispatched_at))
    if not leases:
        return None
    if len(leases) != 1:
        raise ValueError("legacy dispatcher state has multiple active owners")
    claim_token, records = next(iter(leases.items()))
    return {
        "claim_token": claim_token,
        "claimed_at": isoformat(min(value for _, value in records)),
        "event_ids": sorted((event_id for event_id, _ in records), key=int),
        "runtime_id": None,
    }


def _migrate_legacy_state(payload: dict[str, Any]) -> dict[str, Any]:
    events = payload.get("events")
    if not isinstance(events, dict):
        raise ValueError("legacy autopilot dispatcher events must be an object")
    attempts: dict[str, int] = {}
    for raw_event_id, record in events.items():
        event_id = _event_id(raw_event_id, field="legacy dispatcher events")
        if not isinstance(record, dict):
            raise ValueError("legacy dispatcher event record must be an object")
        raw_count = record.get("dispatch_count", 0)
        if not isinstance(raw_count, int) or isinstance(raw_count, bool):
            raise ValueError("legacy dispatcher attempt count must be an integer")
        attempts[event_id] = raw_count
    owner = payload.get("owner")
    return {
        "version": STATE_VERSION,
        "attempts": _validated_attempts(attempts),
        "owner": _validated_owner(
            owner if owner is not None else _legacy_owner(events)
        ),
        "updated_at": payload.get("updated_at"),
    }


def _load_state(path: Path) -> tuple[dict[str, Any], bool]:
    if not path.exists():
        return _default_state(), False
    payload = read_json(path)
    version = payload.get("version")
    if version == LEGACY_STATE_VERSION:
        return _migrate_legacy_state(payload), True
    if version != STATE_VERSION:
        raise ValueError("Unsupported autopilot dispatcher state version")
    return (
        {
            "version": STATE_VERSION,
            "attempts": _validated_attempts(payload.get("attempts")),
            "owner": _validated_owner(payload.get("owner")),
            "updated_at": payload.get("updated_at"),
        },
        False,
    )


def load_state(path: Path) -> dict[str, Any]:
    return _load_state(path)[0]


def claim_event_ids(state: dict[str, Any], claim_token: str) -> list[str]:
    owner = state.get("owner")
    if not isinstance(owner, dict) or owner.get("claim_token") != claim_token:
        return []
    return sorted((str(value) for value in owner.get("event_ids", [])), key=int)


@contextmanager
def locked_state(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def locked_wake(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def compact_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        key: event.get(key)
        for key in (
            "id",
            "url",
            "author_id",
            "username",
            "conversation_id",
            "created_at",
            "first_seen_at",
            "is_reply",
        )
    }


def select_claim_events(
    events: list[dict[str, Any]],
    *,
    max_events: int,
    priority_author_ids: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Select a bounded claim, prioritizing requested authors then age."""

    if not 1 <= max_events <= inbound_policy.MAX_SUPPORTED_CLAIM_EVENTS:
        raise ValueError(
            "max claim events must be between 1 and "
            f"{inbound_policy.MAX_SUPPORTED_CLAIM_EVENTS}"
        )
    latest = datetime.max.replace(tzinfo=timezone.utc)

    def priority(event: dict[str, Any]) -> tuple[int, datetime, int]:
        observed_at = parse_time(
            str(event.get("first_seen_at") or event.get("created_at") or "")
        )
        author_rank = (
            0 if str(event.get("author_id") or "") in priority_author_ids else 1
        )
        return author_rank, observed_at or latest, int(str(event["id"]))

    return sorted(events, key=priority)[:max_events]


def _owner_runtime_changed(
    owner: dict[str, Any],
    runtime_id: str | None,
) -> bool:
    owner_runtime_id = owner.get("runtime_id")
    return (
        isinstance(owner_runtime_id, str)
        and bool(owner_runtime_id)
        and isinstance(runtime_id, str)
        and bool(runtime_id)
        and owner_runtime_id != runtime_id
    )


def _owner_lease_expiry(
    owner: dict[str, Any],
    *,
    lease_seconds: int,
) -> tuple[datetime | None, datetime | None, bool]:
    claimed_at = parse_time(owner.get("claimed_at"))
    expires_at = parse_time(owner.get("lease_expires_at"))
    changed = expires_at is None and claimed_at is not None
    if changed:
        expires_at = claimed_at + timedelta(seconds=lease_seconds)
        owner["lease_expires_at"] = isoformat(expires_at)
    return claimed_at, expires_at, changed


def _active_owner_result(
    state_file: Path,
    state: dict[str, Any],
    pending_events: list[dict[str, Any]],
    owner: dict[str, Any],
    *,
    current_time: datetime,
    lease_seconds: int,
    runtime_id: str | None,
    persist_state: bool,
) -> dict[str, Any] | None:
    runtime_changed = _owner_runtime_changed(owner, runtime_id)
    claimed_at, expires_at, lease_changed = _owner_lease_expiry(
        owner,
        lease_seconds=lease_seconds,
    )
    if (
        claimed_at is None
        or runtime_changed
        or expires_at is None
        or current_time >= expires_at
    ):
        return None
    active_ids = [str(value) for value in owner.get("event_ids", [])]
    if persist_state or lease_changed:
        state["updated_at"] = isoformat(current_time)
        atomic_write_json(state_file, state)
    return {
        "dispatch": False,
        "owner_busy": True,
        "active_claim_token": owner.get("claim_token"),
        "active_event_ids": active_ids,
        "pending_count": len(pending_events),
        "leased_count": len(active_ids),
        "lease_seconds": lease_seconds,
    }


def _prune_attempts(
    state: dict[str, Any],
    pending_events: list[dict[str, Any]],
) -> dict[str, Any]:
    pending_ids = {event["id"] for event in pending_events}
    state["attempts"] = {
        event_id: value
        for event_id, value in state["attempts"].items()
        if event_id in pending_ids
    }
    return state["attempts"]


def _idle_claim_result(
    state_file: Path,
    state: dict[str, Any],
    *,
    current_time: datetime,
    lease_seconds: int,
) -> dict[str, Any]:
    state["updated_at"] = isoformat(current_time)
    atomic_write_json(state_file, state)
    return {
        "dispatch": False,
        "owner_busy": False,
        "pending_count": 0,
        "leased_count": 0,
        "lease_seconds": lease_seconds,
    }


def _create_claim(
    state_file: Path,
    state: dict[str, Any],
    attempts: dict[str, int],
    pending_events: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    current_time: datetime,
    lease_seconds: int,
    runtime_id: str | None,
) -> dict[str, Any]:
    claim_token = str(uuid.uuid4())
    claimed_at = isoformat(current_time)
    lease_expires_at = isoformat(
        current_time + timedelta(seconds=lease_seconds)
    )
    for event in events:
        attempts[event["id"]] = attempts.get(event["id"], 0) + 1
    state["owner"] = {
        "claim_token": claim_token,
        "claimed_at": claimed_at,
        "lease_expires_at": lease_expires_at,
        "event_ids": [event["id"] for event in events],
        "runtime_id": runtime_id,
    }
    state["updated_at"] = claimed_at
    atomic_write_json(state_file, state)
    return {
        "dispatch": True,
        "claim_token": claim_token,
        "claimed_at": claimed_at,
        "lease_expires_at": lease_expires_at,
        "lease_seconds": lease_seconds,
        "pending_count": len(pending_events),
        "claimed_count": len(events),
        "events": [compact_event(event) for event in events],
    }


def claim(
    wake_file: Path,
    state_file: Path,
    *,
    lease_seconds: int,
    max_events: int = DEFAULT_MAX_CLAIM_EVENTS,
    now: datetime | None = None,
    runtime_id: str | None = None,
    priority_author_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    current_time = now or utc_now()
    with locked_state(state_file):
        pending_events = load_wake_events(wake_file)
        events = select_claim_events(
            pending_events,
            max_events=max_events,
            priority_author_ids=priority_author_ids,
        )
        state, migrated = _load_state(state_file)
        owner = state.get("owner")
        if isinstance(owner, dict):
            active = _active_owner_result(
                state_file,
                state,
                pending_events,
                owner,
                current_time=current_time,
                lease_seconds=lease_seconds,
                runtime_id=runtime_id,
                persist_state=migrated,
            )
            if active is not None:
                return active
            state["owner"] = None
        attempts = _prune_attempts(state, pending_events)
        if not pending_events:
            return _idle_claim_result(
                state_file,
                state,
                current_time=current_time,
                lease_seconds=lease_seconds,
            )
        return _create_claim(
            state_file,
            state,
            attempts,
            pending_events,
            events,
            current_time=current_time,
            lease_seconds=lease_seconds,
            runtime_id=runtime_id,
        )


def renew(
    state_file: Path,
    claim_token: str,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Extend one exact active claim without changing its event set."""

    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    current_time = now or utc_now()
    with locked_state(state_file):
        state, _ = _load_state(state_file)
        owner = state.get("owner")
        if not isinstance(owner, dict) or owner.get("claim_token") != claim_token:
            raise ValueError("claim token is not the active global owner")
        event_ids = claim_event_ids(state, claim_token)
        if not event_ids:
            raise ValueError("active owner has no event ids")

        renewed_at = isoformat(current_time)
        lease_expires_at = isoformat(
            current_time + timedelta(seconds=lease_seconds)
        )
        owner["last_renewed_at"] = renewed_at
        owner["lease_expires_at"] = lease_expires_at
        state["updated_at"] = renewed_at
        atomic_write_json(state_file, state)
        return {
            "claim_token": claim_token,
            "event_ids": event_ids,
            "renewed_at": renewed_at,
            "lease_expires_at": lease_expires_at,
            "lease_seconds": lease_seconds,
        }


def release(state_file: Path, claim_token: str) -> dict[str, Any]:
    with locked_state(state_file):
        state, _ = _load_state(state_file)
        released = claim_event_ids(state, claim_token)
        owner = state.get("owner")
        if isinstance(owner, dict) and owner.get("claim_token") == claim_token:
            state["owner"] = None
        state["updated_at"] = isoformat()
        atomic_write_json(state_file, state)
        return {"released": sorted(released), "claim_token": claim_token}


def finish(state_file: Path, claim_token: str) -> dict[str, Any]:
    with locked_state(state_file):
        state, _ = _load_state(state_file)
        finished = claim_event_ids(state, claim_token)
        for event_id in finished:
            state["attempts"].pop(event_id, None)
        owner = state.get("owner")
        if isinstance(owner, dict) and owner.get("claim_token") == claim_token:
            state["owner"] = None
        state["updated_at"] = isoformat()
        atomic_write_json(state_file, state)
        return {"finished": finished, "claim_token": claim_token}


def status(wake_file: Path, state_file: Path, *, lease_seconds: int) -> dict[str, Any]:
    with locked_state(state_file):
        events = load_wake_events(wake_file)
        state, migrated = _load_state(state_file)
        current_time = utc_now()
        owner = state.get("owner")
        owner_busy = False
        active_claim_token: str | None = None
        active_event_ids: list[str] = []
        if isinstance(owner, dict):
            claimed_at, lease_expires_at, lease_changed = _owner_lease_expiry(
                owner,
                lease_seconds=lease_seconds,
            )
            if migrated or lease_changed:
                state["updated_at"] = isoformat(current_time)
                atomic_write_json(state_file, state)
            if (
                claimed_at is not None
                and lease_expires_at is not None
                and current_time < lease_expires_at
            ):
                owner_busy = True
                active_claim_token = str(owner.get("claim_token"))
                active_event_ids = [
                    str(value) for value in owner.get("event_ids", [])
                ]
        elif migrated:
            state["updated_at"] = isoformat(current_time)
            atomic_write_json(state_file, state)
        pending_ids = {event["id"] for event in events}
        leased_ids = [
            event_id for event_id in active_event_ids if event_id in pending_ids
        ]
        return {
            "pending_count": len(events),
            "pending_ids": [event["id"] for event in events],
            "leased_count": len(leased_ids),
            "leased_ids": leased_ids,
            "owner_busy": owner_busy,
            "active_claim_token": active_claim_token,
            "active_event_ids": active_event_ids,
            "lease_seconds": lease_seconds,
        }


def snapshot(wake_file: Path) -> dict[str, Any]:
    events = load_wake_events(wake_file)
    return {
        "pending_count": len(events),
        "pending_ids": [event["id"] for event in events],
        "events": [compact_event(event) for event in events],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lease-seconds", type=int, default=1800)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("claim")
    release_parser = subparsers.add_parser("release")
    release_parser.add_argument("--claim-token", required=True)
    renew_parser = subparsers.add_parser("renew")
    renew_parser.add_argument("--claim-token", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("snapshot")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    wake_file, state_file = load_paths(arguments.config)
    if arguments.command == "claim":
        config = read_json(arguments.config)
        max_events = int(
            config.get(
                "autopilot_max_claim_events",
                DEFAULT_MAX_CLAIM_EVENTS,
            )
        )
        result = claim(
            wake_file,
            state_file,
            lease_seconds=arguments.lease_seconds,
            max_events=max_events,
            priority_author_ids=configured_priority_author_ids(config),
        )
    elif arguments.command == "release":
        result = release(state_file, arguments.claim_token)
    elif arguments.command == "renew":
        result = renew(
            state_file,
            arguments.claim_token,
            lease_seconds=arguments.lease_seconds,
        )
    elif arguments.command == "status":
        result = status(
            wake_file,
            state_file,
            lease_seconds=arguments.lease_seconds,
        )
    else:
        result = snapshot(wake_file)
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
