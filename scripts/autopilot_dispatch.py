#!/usr/bin/env python3
"""Atomically claim queued X events for a Codex autopilot wake."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
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

STATE_VERSION = 1
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


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


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
    conversation_id = raw_event.get("conversation_id")
    if conversation_id is not None and not str(conversation_id).isdigit():
        raise ValueError("wake-request conversation_id must be numeric")
    if event_id in seen:
        raise ValueError(f"wake-request contains duplicate event {event_id}")
    seen.add(event_id)
    return {
        "id": event_id,
        "url": url,
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


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": STATE_VERSION, "events": {}, "owner": None}
    payload = read_json(path)
    if payload.get("version") != STATE_VERSION:
        raise ValueError("Unsupported autopilot dispatcher state version")
    events = payload.get("events")
    if not isinstance(events, dict):
        raise ValueError("autopilot dispatcher events must be an object")
    owner = payload.get("owner")
    if owner is not None and not isinstance(owner, dict):
        raise ValueError("autopilot dispatcher owner must be an object or null")
    payload.setdefault("owner", None)
    return payload


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
) -> list[dict[str, Any]]:
    """Select one bounded oldest-first claim without mutating the queue."""

    if not 1 <= max_events <= inbound_policy.MAX_SUPPORTED_CLAIM_EVENTS:
        raise ValueError(
            "max claim events must be between 1 and "
            f"{inbound_policy.MAX_SUPPORTED_CLAIM_EVENTS}"
        )
    latest = datetime.max.replace(tzinfo=timezone.utc)

    def priority(event: dict[str, Any]) -> tuple[datetime, int]:
        observed_at = parse_time(
            str(event.get("first_seen_at") or event.get("created_at") or "")
        )
        return observed_at or latest, int(str(event["id"]))

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
) -> tuple[datetime | None, datetime | None]:
    claimed_at = parse_time(owner.get("claimed_at"))
    expires_at = parse_time(owner.get("lease_expires_at"))
    if expires_at is None and claimed_at is not None:
        expires_at = claimed_at + timedelta(seconds=lease_seconds)
        owner["lease_expires_at"] = isoformat(expires_at)
    return claimed_at, expires_at


def _active_owner_result(
    state_file: Path,
    state: dict[str, Any],
    pending_events: list[dict[str, Any]],
    owner: dict[str, Any],
    *,
    current_time: datetime,
    lease_seconds: int,
    runtime_id: str | None,
) -> dict[str, Any] | None:
    runtime_changed = _owner_runtime_changed(owner, runtime_id)
    claimed_at, expires_at = _owner_lease_expiry(
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


def _release_changed_runtime_owner(
    state_events: dict[str, Any],
    owner: dict[str, Any],
    runtime_id: str | None,
) -> None:
    if not _owner_runtime_changed(owner, runtime_id):
        return
    owner_token = owner.get("claim_token")
    for event_id in owner.get("event_ids", []):
        record = state_events.get(str(event_id))
        if (
            isinstance(record, dict)
            and record.get("claim_token") == owner_token
        ):
            record.pop("claim_token", None)
            record.pop("last_dispatched_at", None)


def _legacy_active_result(
    state_events: dict[str, Any],
    pending_events: list[dict[str, Any]],
    *,
    current_time: datetime,
    lease_seconds: int,
) -> dict[str, Any] | None:
    active: list[tuple[str, dict[str, Any]]] = []
    for event_id, record in state_events.items():
        if not isinstance(record, dict):
            continue
        dispatched_at = parse_time(record.get("last_dispatched_at"))
        if dispatched_at is None:
            continue
        age_seconds = (current_time - dispatched_at).total_seconds()
        if age_seconds < lease_seconds and record.get("claim_token"):
            active.append((event_id, record))
    if not active:
        return None
    tokens = sorted({str(record["claim_token"]) for _, record in active})
    active_ids = sorted((event_id for event_id, _ in active), key=int)
    return {
        "dispatch": False,
        "owner_busy": True,
        "active_claim_token": tokens[0] if len(tokens) == 1 else None,
        "active_claim_tokens": tokens,
        "active_event_ids": active_ids,
        "pending_count": len(pending_events),
        "leased_count": len(active_ids),
        "lease_seconds": lease_seconds,
    }


def _prune_state_events(
    state: dict[str, Any],
    pending_events: list[dict[str, Any]],
) -> dict[str, Any]:
    pending_ids = {event["id"] for event in pending_events}
    state["events"] = {
        event_id: value
        for event_id, value in state["events"].items()
        if event_id in pending_ids
    }
    return state["events"]


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
    state_events: dict[str, Any],
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
        previous = state_events.get(event["id"])
        dispatch_count = (
            int(previous.get("dispatch_count", 0))
            if isinstance(previous, dict)
            else 0
        )
        state_events[event["id"]] = {
            "claim_token": claim_token,
            "dispatch_count": dispatch_count + 1,
            "last_dispatched_at": claimed_at,
        }
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
) -> dict[str, Any]:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    current_time = now or utc_now()
    with locked_state(state_file):
        pending_events = load_wake_events(wake_file)
        events = select_claim_events(pending_events, max_events=max_events)
        state = load_state(state_file)
        state_events = state["events"]
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
            )
            if active is not None:
                return active
            _release_changed_runtime_owner(
                state_events,
                owner,
                runtime_id,
            )
            state["owner"] = None
        legacy = _legacy_active_result(
            state_events,
            pending_events,
            current_time=current_time,
            lease_seconds=lease_seconds,
        )
        if legacy is not None:
            return legacy
        state_events = _prune_state_events(state, pending_events)
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
            state_events,
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
        state = load_state(state_file)
        owner = state.get("owner")
        if not isinstance(owner, dict) or owner.get("claim_token") != claim_token:
            raise ValueError("claim token is not the active global owner")
        event_ids = [str(value) for value in owner.get("event_ids", [])]
        if not event_ids:
            raise ValueError("active owner has no event ids")
        state_events = state["events"]
        for event_id in event_ids:
            record = state_events.get(event_id)
            if (
                not isinstance(record, dict)
                or record.get("claim_token") != claim_token
            ):
                raise ValueError(
                    f"claim state is inconsistent for event {event_id}"
                )

        renewed_at = isoformat(current_time)
        lease_expires_at = isoformat(
            current_time + timedelta(seconds=lease_seconds)
        )
        owner["last_renewed_at"] = renewed_at
        owner["lease_expires_at"] = lease_expires_at
        for event_id in event_ids:
            state_events[event_id]["last_dispatched_at"] = renewed_at
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
        state = load_state(state_file)
        released: list[str] = []
        for event_id, record in state["events"].items():
            if (
                isinstance(record, dict)
                and record.get("claim_token") == claim_token
            ):
                record.pop("claim_token", None)
                record.pop("last_dispatched_at", None)
                released.append(event_id)
        owner = state.get("owner")
        if isinstance(owner, dict) and owner.get("claim_token") == claim_token:
            state["owner"] = None
        state["updated_at"] = isoformat()
        atomic_write_json(state_file, state)
        return {"released": sorted(released), "claim_token": claim_token}


def finish(state_file: Path, claim_token: str) -> dict[str, Any]:
    with locked_state(state_file):
        state = load_state(state_file)
        finished = sorted(
            (
                event_id
                for event_id, record in state["events"].items()
                if isinstance(record, dict)
                and record.get("claim_token") == claim_token
            ),
            key=int,
        )
        for event_id in finished:
            del state["events"][event_id]
        owner = state.get("owner")
        if isinstance(owner, dict) and owner.get("claim_token") == claim_token:
            state["owner"] = None
        state["updated_at"] = isoformat()
        atomic_write_json(state_file, state)
        return {"finished": finished, "claim_token": claim_token}


def status(wake_file: Path, state_file: Path, *, lease_seconds: int) -> dict[str, Any]:
    with locked_state(state_file):
        events = load_wake_events(wake_file)
        state = load_state(state_file)
        current_time = utc_now()
        owner = state.get("owner")
        owner_busy = False
        active_claim_token: str | None = None
        active_event_ids: list[str] = []
        if isinstance(owner, dict):
            claimed_at = parse_time(owner.get("claimed_at"))
            lease_expires_at = parse_time(owner.get("lease_expires_at"))
            if lease_expires_at is None and claimed_at is not None:
                lease_expires_at = claimed_at + timedelta(
                    seconds=lease_seconds
                )
                owner["lease_expires_at"] = isoformat(lease_expires_at)
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
        leased_ids: list[str] = []
        for event in events:
            record = state["events"].get(event["id"])
            dispatched_at = (
                parse_time(record.get("last_dispatched_at"))
                if isinstance(record, dict)
                else None
            )
            if (
                dispatched_at is not None
                and (current_time - dispatched_at).total_seconds() < lease_seconds
            ):
                leased_ids.append(event["id"])
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
