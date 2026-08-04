#!/usr/bin/env python3
"""Shared validation primitives for durable X watcher records."""

from __future__ import annotations

import sqlite3
from typing import Any


def required_text(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise ValueError("Missing required history field: " + " or ".join(names))


def required_exact_text(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        if name in payload and payload[name] is not None:
            return str(payload[name])
    raise ValueError("Missing required history field: " + " or ".join(names))


def optional_text(payload: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def validate_status_id(value: str, field_name: str) -> str:
    if not value.isdigit() or len(value) > 19:
        raise ValueError(f"{field_name} must be a numeric X status ID")
    return value


def assert_existing_values(
    existing: sqlite3.Row,
    expected: dict[str, Any],
    *,
    resource: str,
) -> None:
    mismatches = [
        name
        for name, value in expected.items()
        if existing[name] is not None
        and value is not None
        and existing[name] != value
    ]
    if mismatches:
        raise ValueError(
            f"Append-only history conflict for {resource}: "
            + ", ".join(sorted(mismatches))
        )
