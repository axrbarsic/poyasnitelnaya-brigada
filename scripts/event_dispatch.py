#!/usr/bin/env python3
"""Kick the model-free dispatcher when durable X work becomes ready."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

try:
    from scripts import autopilot_dispatch
except ModuleNotFoundError:
    import autopilot_dispatch  # type: ignore[no-redef]


DEFAULT_LABEL = "com.axrbarsic.xmention.dispatch"
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]+$")
NEW_EVENTS_REASON = "new_poll_events"
CLAIM_COMPLETED_REASON = "claim_completed_with_pending_queue"
DEFAULT_RUNNER = subprocess.run


def _state_path(config_path: Path, config: dict[str, Any]) -> Path:
    raw = Path(
        str(
            config.get(
                "event_dispatch_state_file",
                "var/event-dispatch.json",
            )
        )
    ).expanduser()
    return raw if raw.is_absolute() else (config_path.parent / raw).resolve()


def _normalize_event_ids(raw_ids: Any) -> list[str]:
    if not isinstance(raw_ids, list):
        return []
    return sorted(
        {
            str(event_id)
            for event_id in raw_ids
            if str(event_id).isdigit()
        },
        key=int,
    )


def _event_ids(result: dict[str, Any]) -> list[str]:
    return _normalize_event_ids(result.get("new_event_ids", []))


def trigger_event_ids(
    config_path: Path,
    event_ids: list[str],
    *,
    reason: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Record and kick one bounded model-free delivery request."""

    normalized_ids = _normalize_event_ids(event_ids)
    if not normalized_ids:
        return {
            "status": "not_needed",
            "triggered": False,
            "event_ids": normalized_ids,
            "reason": reason,
        }

    resolved_config = config_path.expanduser().resolve()
    config = autopilot_dispatch.read_json(resolved_config)
    enabled = bool(config.get("event_dispatch_on_new_events", False))
    if not enabled:
        return {
            "status": "disabled",
            "triggered": False,
            "event_ids": normalized_ids,
            "reason": reason,
        }

    label = str(
        config.get("event_dispatch_launchagent_label", DEFAULT_LABEL)
    ).strip()
    if not LABEL_PATTERN.fullmatch(label):
        raise ValueError("event dispatch LaunchAgent label is invalid")

    requested_at = autopilot_dispatch.isoformat()
    state_path = _state_path(resolved_config, config)
    request_payload: dict[str, Any] = {
        "version": 1,
        "status": "dispatch_requested",
        "triggered": False,
        "reason": reason,
        "event_ids": normalized_ids,
        "launchagent_label": label,
        "requested_at": requested_at,
        "completed_at": None,
        "elapsed_ms": 0.0,
        "returncode": None,
    }
    autopilot_dispatch.atomic_write_json(state_path, request_payload)

    started = time.monotonic()
    command = [
        "/bin/launchctl",
        "kickstart",
        f"gui/{os.getuid()}/{label}",
    ]
    status = "fallback_scheduled"
    triggered = False
    error: str | None = None
    returncode: int | None = None
    run = runner or DEFAULT_RUNNER
    try:
        completed = run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        returncode = int(completed.returncode)
        if returncode == 0:
            status = "dispatch_kicked"
            triggered = True
        else:
            error = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or f"launchctl exited with {returncode}"
            )[-500:]
    except (OSError, subprocess.SubprocessError) as caught:
        error = f"{type(caught).__name__}: {caught}"[-500:]

    payload = {
        **request_payload,
        "status": status,
        "triggered": triggered,
        "completed_at": autopilot_dispatch.isoformat(),
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        "returncode": returncode,
    }
    if error:
        payload["error"] = error
    autopilot_dispatch.atomic_write_json(state_path, payload)
    return payload


def trigger_pending_events(
    config_path: Path,
    event_ids: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Kick the next delivery after a claim leaves pending queue work."""

    return trigger_event_ids(
        config_path,
        event_ids,
        reason=CLAIM_COMPLETED_REASON,
        runner=runner,
    )


def trigger_from_poll_result(
    config_path: Path,
    result: dict[str, Any],
    *,
    live_poll: bool,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Kick launchd once for a live poll that durably queued new events."""

    event_ids = _event_ids(result)
    if not live_poll or not event_ids:
        return {
            "status": "not_needed",
            "triggered": False,
            "event_ids": event_ids,
        }

    return trigger_event_ids(
        config_path,
        event_ids,
        reason=NEW_EVENTS_REASON,
        runner=runner,
    )
