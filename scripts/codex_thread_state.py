#!/usr/bin/env python3
"""Read-only access to the canonical Codex thread registry."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


THREAD_FIELDS = (
    "id",
    "archived",
    "is_pinned",
    "title",
    "model",
    "reasoning_effort",
    "cwd",
)


def state_database(codex_home: Path) -> Path | None:
    """Return the newest Codex state database without mutating it."""

    candidates = sorted(
        codex_home.expanduser().glob("state_*.sqlite"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def thread_row(codex_home: Path, thread_id: str) -> dict[str, Any] | None:
    """Read one thread from Codex state through a read-only SQLite URI."""

    database = state_database(codex_home)
    if database is None:
        raise ValueError("Codex state database is missing")
    identifier = thread_id.strip()
    if not identifier:
        raise ValueError("thread id is required")
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            f"SELECT {', '.join(THREAD_FIELDS)} FROM threads WHERE id = ?",
            (identifier,),
        ).fetchone()
    return dict(row) if row is not None else None
