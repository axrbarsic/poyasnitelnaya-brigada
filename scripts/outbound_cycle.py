#!/usr/bin/env python3
"""Serialize scheduled outbound X runs and track bounded catch-up work."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts import autopilot_dispatch
except ModuleNotFoundError:
    import autopilot_dispatch  # type: ignore[no-redef]


STATE_VERSION = 1
MAX_RUN_HISTORY = 500
ADJUSTMENT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
STATUS_ID = re.compile(r"^[0-9]{1,19}$")
X_STATUS_PATH = re.compile(
    r"^/(?:[A-Za-z0-9_]{1,15}|i/web)/status/([0-9]{1,19})$"
)


def default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "catchup_remaining": 0,
        "adjustments": [],
        "runs": [],
        "owner": None,
        "updated_at": autopilot_dispatch.isoformat(),
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_state()
    payload = autopilot_dispatch.read_json(path)
    if payload.get("version") != STATE_VERSION:
        raise ValueError("unsupported outbound cycle state version")
    remaining = payload.get("catchup_remaining")
    if not isinstance(remaining, int) or remaining < 0:
        raise ValueError("catchup_remaining must be a non-negative integer")
    for key in ("adjustments", "runs"):
        if not isinstance(payload.get(key), list):
            raise ValueError(f"{key} must be a list")
    if payload.get("owner") is not None and not isinstance(
        payload.get("owner"), dict
    ):
        raise ValueError("owner must be an object or null")
    return payload


def write_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = autopilot_dispatch.isoformat()
    state["runs"] = list(state.get("runs", []))[-MAX_RUN_HISTORY:]
    autopilot_dispatch.atomic_write_json(path, state)


def parse_aware_time(value: str, *, field: str) -> datetime:
    parsed = autopilot_dispatch.parse_time(value)
    if parsed is None or parsed.tzinfo is None:
        raise ValueError(f"{field} must be a timezone-aware timestamp")
    return parsed


def add_catchup(
    path: Path,
    *,
    adjustment_id: str,
    count: int,
    reason: str,
    window_start: str,
    window_end: str,
) -> dict[str, Any]:
    identifier = adjustment_id.strip()
    explanation = reason.strip()
    if ADJUSTMENT_ID.fullmatch(identifier) is None:
        raise ValueError("adjustment_id has an invalid format")
    if count <= 0:
        raise ValueError("count must be positive")
    if not explanation:
        raise ValueError("reason must not be empty")
    start = parse_aware_time(window_start, field="window_start")
    end = parse_aware_time(window_end, field="window_end")
    if end <= start:
        raise ValueError("window_end must be later than window_start")

    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        expected = {
            "adjustment_id": identifier,
            "count": count,
            "reason": explanation,
            "window_start": autopilot_dispatch.isoformat(start),
            "window_end": autopilot_dispatch.isoformat(end),
        }
        for previous in state["adjustments"]:
            if previous.get("adjustment_id") != identifier:
                continue
            comparable = {
                key: previous.get(key)
                for key in (
                    "adjustment_id",
                    "count",
                    "reason",
                    "window_start",
                    "window_end",
                )
            }
            if comparable != expected:
                raise ValueError("adjustment_id already exists with other data")
            return {
                "status": "already_applied",
                "catchup_remaining": state["catchup_remaining"],
                **expected,
            }
        record = {
            **expected,
            "created_at": autopilot_dispatch.isoformat(),
        }
        state["adjustments"].append(record)
        state["catchup_remaining"] += count
        write_state(path, state)
    return {
        "status": "catchup_added",
        "catchup_remaining": state["catchup_remaining"],
        **record,
    }


def owner_is_active(owner: dict[str, Any], now: datetime) -> bool:
    expires_at = parse_aware_time(
        str(owner.get("expires_at", "")),
        field="owner.expires_at",
    )
    return expires_at > now


def append_expired_run(state: dict[str, Any], owner: dict[str, Any]) -> None:
    state["runs"].append(
        {
            "claim_token": owner.get("claim_token"),
            "claimed_at": owner.get("claimed_at"),
            "started_at": owner.get("started_at"),
            "finished_at": autopilot_dispatch.isoformat(),
            "status": "expired",
            "target_limit": owner.get("target_limit"),
            "publications": [],
            "catchup_consumed": 0,
        }
    )


def claim(
    path: Path,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    checked_at = now or autopilot_dispatch.utc_now()
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        owner = state.get("owner")
        if owner is not None and owner_is_active(owner, checked_at):
            return {
                "status": "owner_busy",
                "dispatch": False,
                "claim_token": owner.get("claim_token"),
                "expires_at": owner.get("expires_at"),
                "target_limit": owner.get("target_limit"),
                "catchup_remaining": state["catchup_remaining"],
            }
        if owner is not None:
            append_expired_run(state, owner)
        token = str(uuid.uuid4())
        target_limit = 2 if state["catchup_remaining"] > 0 else 1
        expires_at = checked_at + timedelta(seconds=lease_seconds)
        state["owner"] = {
            "claim_token": token,
            "status": "claimed",
            "claimed_at": autopilot_dispatch.isoformat(checked_at),
            "expires_at": autopilot_dispatch.isoformat(expires_at),
            "target_limit": target_limit,
            "catchup_remaining_at_claim": state["catchup_remaining"],
        }
        write_state(path, state)
    return {
        "status": "claimed",
        "dispatch": True,
        "claim_token": token,
        "expires_at": autopilot_dispatch.isoformat(expires_at),
        "target_limit": target_limit,
        "catchup_remaining": state["catchup_remaining"],
    }


def require_owner(
    state: dict[str, Any],
    *,
    claim_token: str,
) -> dict[str, Any]:
    token = claim_token.strip()
    if not token:
        raise ValueError("claim_token must not be empty")
    owner = state.get("owner")
    if owner is None:
        raise ValueError("no active outbound owner")
    if owner.get("claim_token") != token:
        raise ValueError("claim_token does not match active outbound owner")
    return owner


def started(path: Path, *, claim_token: str) -> dict[str, Any]:
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        owner = require_owner(state, claim_token=claim_token)
        if owner.get("status") not in {"claimed", "work_in_progress"}:
            raise ValueError("outbound owner cannot transition to started")
        owner["status"] = "work_in_progress"
        owner.setdefault("started_at", autopilot_dispatch.isoformat())
        write_state(path, state)
    return {
        "status": "work_in_progress",
        "claim_token": claim_token,
        "target_limit": owner["target_limit"],
        "catchup_remaining": state["catchup_remaining"],
    }


def renew(
    path: Path,
    *,
    claim_token: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    checked_at = now or autopilot_dispatch.utc_now()
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        owner = require_owner(state, claim_token=claim_token)
        if owner.get("status") != "work_in_progress":
            raise ValueError("renew requires a started outbound owner")
        expires_at = checked_at + timedelta(seconds=lease_seconds)
        owner["expires_at"] = autopilot_dispatch.isoformat(expires_at)
        owner["renewed_at"] = autopilot_dispatch.isoformat(checked_at)
        write_state(path, state)
    return {
        "status": "work_in_progress",
        "claim_token": claim_token,
        "expires_at": autopilot_dispatch.isoformat(expires_at),
        "target_limit": owner["target_limit"],
        "catchup_remaining": state["catchup_remaining"],
    }


def validate_publication(value: str) -> dict[str, str]:
    target_status_id, separator, reply_url = value.strip().partition("=")
    if not separator or STATUS_ID.fullmatch(target_status_id) is None:
        raise ValueError("publication must use TARGET_STATUS_ID=REPLY_URL")
    parsed = urllib.parse.urlsplit(reply_url)
    match = X_STATUS_PATH.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "x.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        raise ValueError("publication reply URL must be canonical x.com status")
    if target_status_id == match.group(1):
        raise ValueError("publication reply must differ from its target")
    return {
        "target_status_id": target_status_id,
        "reply_status_id": match.group(1),
        "reply_url": reply_url,
    }


def completed(
    path: Path,
    *,
    claim_token: str,
    publications: Sequence[dict[str, str]],
) -> dict[str, Any]:
    target_ids = [item["target_status_id"] for item in publications]
    reply_ids = [item["reply_status_id"] for item in publications]
    if len(target_ids) != len(set(target_ids)):
        raise ValueError("publications contain duplicate target status IDs")
    if len(reply_ids) != len(set(reply_ids)):
        raise ValueError("publications contain duplicate reply status IDs")
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        owner = require_owner(state, claim_token=claim_token)
        if owner.get("status") != "work_in_progress":
            raise ValueError("completed requires a started outbound owner")
        target_limit = int(owner["target_limit"])
        if len(publications) > target_limit:
            raise ValueError("publication count exceeds target_limit")
        catchup_consumed = min(
            max(0, len(publications) - 1),
            state["catchup_remaining"],
        )
        state["catchup_remaining"] -= catchup_consumed
        state["runs"].append(
            {
                "claim_token": claim_token,
                "claimed_at": owner.get("claimed_at"),
                "started_at": owner.get("started_at"),
                "finished_at": autopilot_dispatch.isoformat(),
                "status": "completed",
                "target_limit": target_limit,
                "publications": list(publications),
                "catchup_consumed": catchup_consumed,
            }
        )
        state["owner"] = None
        write_state(path, state)
    return {
        "status": "completed",
        "claim_token": claim_token,
        "published_count": len(publications),
        "catchup_consumed": catchup_consumed,
        "catchup_remaining": state["catchup_remaining"],
    }


def failed(path: Path, *, claim_token: str, reason: str) -> dict[str, Any]:
    explanation = reason.strip()
    if not explanation:
        raise ValueError("reason must not be empty")
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        owner = require_owner(state, claim_token=claim_token)
        state["runs"].append(
            {
                "claim_token": claim_token,
                "claimed_at": owner.get("claimed_at"),
                "started_at": owner.get("started_at"),
                "finished_at": autopilot_dispatch.isoformat(),
                "status": "failed",
                "target_limit": owner.get("target_limit"),
                "publications": [],
                "catchup_consumed": 0,
                "reason": explanation,
            }
        )
        state["owner"] = None
        write_state(path, state)
    return {
        "status": "failed",
        "claim_token": claim_token,
        "reason": explanation,
        "catchup_remaining": state["catchup_remaining"],
    }


def status(path: Path) -> dict[str, Any]:
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
    return {
        "status": "busy" if state.get("owner") else "idle",
        "owner": state.get("owner"),
        "catchup_remaining": state["catchup_remaining"],
        "adjustment_count": len(state["adjustments"]),
        "run_count": len(state["runs"]),
        "updated_at": state.get("updated_at"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("var/outbound-cycle.json"),
    )
    parser.add_argument("--lease-seconds", type=int, default=1800)
    commands = parser.add_subparsers(dest="command", required=True)

    catchup = commands.add_parser("catchup-add")
    catchup.add_argument("--adjustment-id", required=True)
    catchup.add_argument("--count", required=True, type=int)
    catchup.add_argument("--reason", required=True)
    catchup.add_argument("--window-start", required=True)
    catchup.add_argument("--window-end", required=True)

    commands.add_parser("claim")
    started_parser = commands.add_parser("started")
    started_parser.add_argument("--claim-token", required=True)
    renew_parser = commands.add_parser("renew")
    renew_parser.add_argument("--claim-token", required=True)
    completed_parser = commands.add_parser("completed")
    completed_parser.add_argument("--claim-token", required=True)
    completed_parser.add_argument(
        "--publication",
        action="append",
        default=[],
        metavar="TARGET_STATUS_ID=REPLY_URL",
    )
    failed_parser = commands.add_parser("failed")
    failed_parser.add_argument("--claim-token", required=True)
    failed_parser.add_argument("--reason", required=True)
    commands.add_parser("status")
    return parser


def run(arguments: argparse.Namespace) -> dict[str, Any]:
    path = arguments.state.expanduser().resolve()
    if arguments.command == "catchup-add":
        return add_catchup(
            path,
            adjustment_id=arguments.adjustment_id,
            count=arguments.count,
            reason=arguments.reason,
            window_start=arguments.window_start,
            window_end=arguments.window_end,
        )
    if arguments.command == "claim":
        return claim(path, lease_seconds=arguments.lease_seconds)
    if arguments.command == "started":
        return started(path, claim_token=arguments.claim_token)
    if arguments.command == "renew":
        return renew(
            path,
            claim_token=arguments.claim_token,
            lease_seconds=arguments.lease_seconds,
        )
    if arguments.command == "completed":
        publications = [
            validate_publication(value) for value in arguments.publication
        ]
        return completed(
            path,
            claim_token=arguments.claim_token,
            publications=publications,
        )
    if arguments.command == "failed":
        return failed(
            path,
            claim_token=arguments.claim_token,
            reason=arguments.reason,
        )
    if arguments.command == "status":
        return status(path)
    raise AssertionError(f"unknown command: {arguments.command}")


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        payload = run(arguments)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_class": type(error).__name__,
                    "message": str(error),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
