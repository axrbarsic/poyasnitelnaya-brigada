#!/usr/bin/env python3
"""Validated watcher configuration loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts import watcher_io


@dataclass(frozen=True)
class Config:
    source_path: Path
    user_id: str
    database: Path
    lock_file: Path
    health_file: Path
    wake_file: Path
    alert_file: Path
    poll_interval_seconds: int
    stale_after_seconds: int
    failure_threshold: int
    silence_review_seconds: int
    watchdog_interval_seconds: int
    request_timeout_seconds: int
    max_pages_per_poll: int
    api_base: str
    keychain_helper: Path
    keychain_service: str
    keychain_account: str
    queue_direct_replies_only: bool
    mandatory_response_mode: bool
    conversation_tail_enabled: bool
    conversation_tail_poll_interval_seconds: int
    conversation_tail_watch_hours: int
    conversation_tail_initial_lookback_hours: int
    conversation_tail_overlap_seconds: int
    conversation_tail_max_conversations: int
    conversation_tail_daily_post_read_limit: int
    conversation_tail_max_post_reads_per_poll: int
    commenter_memory_limit: int
    notifications_enabled: bool


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def load_config(path: Path) -> Config:
    raw = watcher_io.read_json(path)
    base = path.resolve().parent
    config = Config(
        source_path=path.resolve(),
        user_id=str(raw.get("user_id", "")).strip(),
        database=_resolve(base, raw.get("database", "var/watcher.sqlite3")),
        lock_file=_resolve(base, raw.get("lock_file", "var/poll.lock")),
        health_file=_resolve(base, raw.get("health_file", "var/health.json")),
        wake_file=_resolve(base, raw.get("wake_file", "var/wake-request.json")),
        alert_file=_resolve(base, raw.get("alert_file", "var/watchdog-alert.json")),
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 300)),
        stale_after_seconds=int(raw.get("stale_after_seconds", 900)),
        failure_threshold=int(raw.get("failure_threshold", 2)),
        silence_review_seconds=int(raw.get("silence_review_seconds", 21600)),
        watchdog_interval_seconds=int(raw.get("watchdog_interval_seconds", 60)),
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 30)),
        max_pages_per_poll=int(raw.get("max_pages_per_poll", 20)),
        api_base=str(raw.get("api_base", "https://api.x.com/2")).rstrip("/"),
        keychain_helper=_resolve(base, raw.get("keychain_helper", "var/keychain-helper")),
        keychain_service=str(raw.get("keychain_service", "")).strip(),
        keychain_account=str(raw.get("keychain_account", "")).strip(),
        queue_direct_replies_only=bool(raw.get("queue_direct_replies_only", True)),
        mandatory_response_mode=bool(raw.get("mandatory_response_mode", False)),
        conversation_tail_enabled=bool(
            raw.get("conversation_tail_enabled", False)
        ),
        conversation_tail_poll_interval_seconds=int(
            raw.get("conversation_tail_poll_interval_seconds", 300)
        ),
        conversation_tail_watch_hours=int(
            raw.get("conversation_tail_watch_hours", 24)
        ),
        conversation_tail_initial_lookback_hours=int(
            raw.get("conversation_tail_initial_lookback_hours", 3)
        ),
        conversation_tail_overlap_seconds=int(
            raw.get("conversation_tail_overlap_seconds", 120)
        ),
        conversation_tail_max_conversations=int(
            raw.get("conversation_tail_max_conversations", 80)
        ),
        conversation_tail_daily_post_read_limit=int(
            raw.get("conversation_tail_daily_post_read_limit", 200)
        ),
        conversation_tail_max_post_reads_per_poll=int(
            raw.get("conversation_tail_max_post_reads_per_poll", 50)
        ),
        commenter_memory_limit=int(raw.get("commenter_memory_limit", 12)),
        notifications_enabled=bool(raw.get("notifications_enabled", True)),
    )
    positive_values = {
        "poll_interval_seconds": config.poll_interval_seconds,
        "stale_after_seconds": config.stale_after_seconds,
        "failure_threshold": config.failure_threshold,
        "silence_review_seconds": config.silence_review_seconds,
        "watchdog_interval_seconds": config.watchdog_interval_seconds,
        "request_timeout_seconds": config.request_timeout_seconds,
        "max_pages_per_poll": config.max_pages_per_poll,
        "conversation_tail_poll_interval_seconds": (
            config.conversation_tail_poll_interval_seconds
        ),
        "conversation_tail_watch_hours": config.conversation_tail_watch_hours,
        "conversation_tail_initial_lookback_hours": (
            config.conversation_tail_initial_lookback_hours
        ),
        "conversation_tail_overlap_seconds": (
            config.conversation_tail_overlap_seconds
        ),
        "conversation_tail_max_conversations": (
            config.conversation_tail_max_conversations
        ),
        "conversation_tail_daily_post_read_limit": (
            config.conversation_tail_daily_post_read_limit
        ),
        "conversation_tail_max_post_reads_per_poll": (
            config.conversation_tail_max_post_reads_per_poll
        ),
        "commenter_memory_limit": config.commenter_memory_limit,
    }
    invalid = [name for name, value in positive_values.items() if value <= 0]
    if invalid:
        raise ValueError("Config values must be positive: " + ", ".join(invalid))
    return config
