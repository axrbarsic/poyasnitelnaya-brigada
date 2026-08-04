#!/usr/bin/env python3
"""Health state, failure recording, and watchdog evaluation for the X watcher."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from scripts import (
    watcher_constants,
    watcher_events,
    watcher_io,
    watcher_time,
)


@dataclass(frozen=True)
class WatchdogDependencies:
    notify_macos: Callable[[Any, str, str], None]
    sleep: Callable[[float], None] = time.sleep


def safe_error_message(error: Exception) -> str:
    message = str(error)
    message = re.sub(r"(?i)bearer\s+[^\s\"']+", "Bearer [REDACTED]", message)
    for name in watcher_constants.TOKEN_ENV_NAMES:
        token = os.environ.get(name)
        if token:
            message = message.replace(token, "[REDACTED]")
    return message[:500]


def write_health(
    config: Any,
    connection: sqlite3.Connection,
    *,
    last_new_count: int,
) -> dict[str, Any]:
    failures = int(watcher_events.get_meta(connection, "consecutive_failures") or "0")
    status = "healthy" if failures == 0 else "degraded"
    payload = {
        "schema_version": watcher_constants.SCHEMA_VERSION,
        "status": status,
        "updated_at": watcher_time.isoformat(),
        "first_success_at": watcher_events.get_meta(connection, "first_success_at"),
        "last_success_at": watcher_events.get_meta(connection, "last_success_at"),
        "last_new_event_at": watcher_events.get_meta(
            connection,
            "last_new_event_at",
        ),
        "last_new_count": last_new_count,
        "last_seen_id": watcher_events.get_meta(connection, "since_id"),
        "consecutive_failures": failures,
        "last_error_class": watcher_events.get_meta(connection, "last_error_class"),
        "last_error_message": watcher_events.get_meta(
            connection,
            "last_error_message",
        ),
        "queued_events": len(watcher_events.queued_events(connection)),
        "poll_interval_seconds": config.poll_interval_seconds,
    }
    watcher_io.atomic_write_json(config.health_file, payload)
    return payload


def record_failure(
    config: Any,
    connection: sqlite3.Connection,
    error: Exception,
    *,
    source: str,
    started_at: str,
) -> dict[str, Any]:
    completed_at = watcher_time.isoformat()
    previous_failures = int(
        watcher_events.get_meta(connection, "consecutive_failures") or "0"
    )
    failures = previous_failures + 1
    safe_message = safe_error_message(error)
    with connection:
        watcher_events.set_meta(connection, "consecutive_failures", str(failures))
        watcher_events.set_meta(connection, "last_error_class", type(error).__name__)
        watcher_events.set_meta(connection, "last_error_message", safe_message)
        connection.execute(
            """
            INSERT INTO poll_runs(
                started_at, completed_at, source, status, new_count,
                error_class, error_message
            ) VALUES(?, ?, ?, 'failure', 0, ?, ?)
            """,
            (started_at, completed_at, source, type(error).__name__, safe_message),
        )
    return write_health(config, connection, last_new_count=0)


def evaluate_health(
    config: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or watcher_time.utc_now()
    if not config.health_file.exists():
        return {
            "status": "never_started",
            "healthy": False,
            "reason": "health_file_missing",
        }
    health = watcher_io.read_json(config.health_file)
    first_success = watcher_time.parse_time(health.get("first_success_at"))
    last_success = watcher_time.parse_time(health.get("last_success_at"))
    last_new = watcher_time.parse_time(health.get("last_new_event_at"))
    failures = int(health.get("consecutive_failures") or 0)
    last_error_class = health.get("last_error_class")
    last_error_message = health.get("last_error_message")

    if str(last_error_message or "").startswith("X API HTTP 402"):
        status = "billing_blocked"
        reason = "x_api_credits_depleted"
        healthy = False
    elif last_success is None:
        status = "never_succeeded"
        reason = "last_success_missing"
        healthy = False
    else:
        success_age = (current - last_success).total_seconds()
        if success_age > config.stale_after_seconds:
            status = "stale"
            reason = "last_success_too_old"
            healthy = False
        elif failures >= config.failure_threshold:
            status = "failing"
            reason = "failure_threshold_reached"
            healthy = False
        elif (
            (last_new or first_success) is not None
            and (current - (last_new or first_success)).total_seconds()
            > config.silence_review_seconds
        ):
            status = "healthy_silence_review"
            reason = "no_new_events_review_due"
            healthy = True
        else:
            status = "healthy"
            reason = "polling_current"
            healthy = True

    return {
        "status": status,
        "healthy": healthy,
        "reason": reason,
        "checked_at": watcher_time.isoformat(current),
        "first_success_at": health.get("first_success_at"),
        "last_success_at": health.get("last_success_at"),
        "last_new_event_at": health.get("last_new_event_at"),
        "consecutive_failures": failures,
        "queued_events": int(health.get("queued_events") or 0),
        "last_error_class": last_error_class,
        "last_error_message": last_error_message,
    }


def run_watchdog(
    config: Any,
    *,
    loop: bool,
    dependencies: WatchdogDependencies,
) -> int:
    previous_status: str | None = None
    if config.alert_file.exists():
        try:
            previous_status = (
                str(watcher_io.read_json(config.alert_file).get("status") or "") or None
            )
        except (OSError, ValueError):
            previous_status = None
    while True:
        result = evaluate_health(config)
        if result["status"] != previous_status:
            watcher_io.atomic_write_json(config.alert_file, result)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
            if result["status"] == "healthy":
                dependencies.notify_macos(
                    config,
                    "X watcher восстановлен",
                    "Проверка X снова работает.",
                )
            else:
                dependencies.notify_macos(
                    config,
                    "Требуется проверка X watcher",
                    f"Состояние: {result['status']}. Причина: {result['reason']}.",
                )
            previous_status = str(result["status"])
        if not loop:
            return 0 if result["healthy"] else 2
        dependencies.sleep(config.watchdog_interval_seconds)
