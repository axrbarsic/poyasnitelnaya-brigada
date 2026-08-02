#!/usr/bin/env python3
"""Detect active heartbeat automations that target archived Codex sessions."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


def parse_automation(path: Path) -> dict[str, Any]:
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"automation TOML must be a table: {path}")
    return payload


def archived_active_heartbeat_targets(home: Path) -> list[dict[str, str]]:
    automations = home / ".codex" / "automations"
    archived_sessions = home / ".codex" / "archived_sessions"
    if not automations.is_dir() or not archived_sessions.is_dir():
        return []

    offenders: list[dict[str, str]] = []
    for automation_path in sorted(automations.glob("*/automation.toml")):
        try:
            payload = parse_automation(automation_path)
        except OSError:
            continue
        if payload.get("kind") != "heartbeat":
            continue
        if payload.get("status") != "ACTIVE":
            continue
        thread_id = str(payload.get("target_thread_id", "")).strip()
        if not thread_id:
            continue
        matches = sorted(
            archived_sessions.glob(f"*-{thread_id}.jsonl")
        )
        if not matches:
            continue
        offenders.append(
            {
                "automation_id": str(
                    payload.get("id", automation_path.parent.name)
                ),
                "name": str(payload.get("name", "")),
                "thread_id": thread_id,
                "archive_file": str(matches[0]),
            }
        )
    return offenders
