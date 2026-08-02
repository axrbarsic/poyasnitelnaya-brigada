#!/usr/bin/env python3
"""Shared dependency and state types for system doctor checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol


class DatabaseIntegrityCheck(Protocol):
    def __call__(
        self,
        database: Path,
        *,
        expected_schema_version: int | None = None,
        expected_application_id: int | None = None,
    ) -> tuple[bool, dict[str, Any] | None]: ...


@dataclass(frozen=True)
class Dependencies:
    make_check: Callable[..., Any]
    read_json: Callable[[Path], Any]
    resolve_project_path: Callable[[Path, str], Path]
    resolve_home_path: Callable[[Path, str], Path]
    state_database: Callable[[Path], Path | None]
    thread_row: Callable[..., dict[str, Any] | None]
    reasoning_effort_meets_minimum: Callable[[Any, Any], bool]
    parse_simple_toml: Callable[[Path], dict[str, Any]]
    tree_digest: Callable[[Path], str]
    git_origin: Callable[[Path], str]
    launchagent_loaded: Callable[[str], tuple[bool, str]]
    database_integrity: DatabaseIntegrityCheck
    parse_timestamp: Callable[[Any], datetime | None]
    relay_progress_check: Callable[..., Any]
    oldest_queue_event_id: Callable[[list[Any]], str | None]
    queue_latency_check: Callable[..., Any]
    latest_poll_context: Callable[[Path], dict[str, Any]]
    keychain_bundle: Any
    personality_policy: Any


@dataclass(frozen=True)
class QueueContext:
    database: Path
    events: list[dict[str, Any]]
    pending: int
