#!/usr/bin/env python3
"""Atomically claim queued X events for a Codex autopilot wake."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


STATE_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


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


def load_wake_events(path: Path) -> list[dict[str, Any]]:
    payload = read_json(path)
    events = payload.get("events")
    pending_count = payload.get("pending_count")
    if not isinstance(events, list):
        raise ValueError("wake-request events must be a list")
    if not isinstance(pending_count, int) or pending_count < 0:
        raise ValueError("wake-request pending_count must be a non-negative integer")
    if pending_count != len(events):
        raise ValueError("wake-request pending_count does not match events length")

    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_event in events:
        if not isinstance(raw_event, dict):
            raise ValueError("wake-request event must be an object")
        event_id = str(
            raw_event.get("event_id", raw_event.get("id", ""))
        ).strip()
        url = str(
            raw_event.get("event_url", raw_event.get("url", ""))
        ).strip()
        if not event_id.isdigit() or not url.startswith("https://x.com/"):
            raise ValueError("wake-request event must contain a numeric id and X URL")
        if event_id in seen:
            raise ValueError(f"wake-request contains duplicate event {event_id}")
        seen.add(event_id)
        validated.append(
            {
                "id": event_id,
                "url": url,
                "username": raw_event.get("username"),
                "conversation_id": raw_event.get("conversation_id"),
                "created_at": raw_event.get("created_at"),
                "first_seen_at": raw_event.get("first_seen_at"),
                "is_reply": bool(raw_event.get("is_reply")),
            }
        )
    return validated


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": STATE_VERSION, "events": {}}
    payload = read_json(path)
    if payload.get("version") != STATE_VERSION:
        raise ValueError("Unsupported autopilot dispatcher state version")
    events = payload.get("events")
    if not isinstance(events, dict):
        raise ValueError("autopilot dispatcher events must be an object")
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


def claim(
    wake_file: Path,
    state_file: Path,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    current_time = now or utc_now()

    with locked_state(state_file):
        events = load_wake_events(wake_file)
        state = load_state(state_file)
        state_events = state["events"]
        pending_ids = {event["id"] for event in events}
        state["events"] = {
            event_id: value
            for event_id, value in state_events.items()
            if event_id in pending_ids
        }
        state_events = state["events"]

        eligible: list[dict[str, Any]] = []
        leased_count = 0
        for event in events:
            record = state_events.get(event["id"])
            dispatched_at = (
                parse_time(record.get("last_dispatched_at"))
                if isinstance(record, dict)
                else None
            )
            if dispatched_at is not None:
                age_seconds = (current_time - dispatched_at).total_seconds()
                if age_seconds < lease_seconds:
                    leased_count += 1
                    continue
            eligible.append(event)

        if not eligible:
            state["updated_at"] = isoformat(current_time)
            atomic_write_json(state_file, state)
            return {
                "dispatch": False,
                "pending_count": len(events),
                "leased_count": leased_count,
                "lease_seconds": lease_seconds,
            }

        claim_token = str(uuid.uuid4())
        claimed_at = isoformat(current_time)
        for event in eligible:
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
        state["updated_at"] = claimed_at
        atomic_write_json(state_file, state)
        return {
            "dispatch": True,
            "claim_token": claim_token,
            "claimed_at": claimed_at,
            "lease_seconds": lease_seconds,
            "pending_count": len(events),
            "events": [compact_event(event) for event in eligible],
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
        state["updated_at"] = isoformat()
        atomic_write_json(state_file, state)
        return {"released": sorted(released), "claim_token": claim_token}


def status(wake_file: Path, state_file: Path, *, lease_seconds: int) -> dict[str, Any]:
    with locked_state(state_file):
        events = load_wake_events(wake_file)
        state = load_state(state_file)
        current_time = utc_now()
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
    subparsers.add_parser("status")
    subparsers.add_parser("snapshot")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    wake_file, state_file = load_paths(arguments.config)
    if arguments.command == "claim":
        result = claim(
            wake_file,
            state_file,
            lease_seconds=arguments.lease_seconds,
        )
    elif arguments.command == "release":
        result = release(state_file, arguments.claim_token)
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
