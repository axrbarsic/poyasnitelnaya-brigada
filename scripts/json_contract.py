#!/usr/bin/env python3
"""Strict JSON decoding for durable runtime and recovery contracts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


class DuplicateKeyError(ValueError):
    """Raised when a JSON object repeats a key."""


def _unique_object(
    pairs: Iterable[tuple[str, Any]],
    *,
    source: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise DuplicateKeyError(
                f"Duplicate JSON key {key!r} in {source}"
            )
        payload[key] = value
    return payload


def loads(text: str, *, source: str = "JSON payload") -> Any:
    """Decode JSON while rejecting duplicate keys at every object depth."""

    return json.loads(
        text,
        object_pairs_hook=lambda pairs: _unique_object(
            pairs,
            source=source,
        ),
    )


def read(path: Path) -> Any:
    return loads(path.read_text(encoding="utf-8"), source=str(path))


def read_object(path: Path) -> dict[str, Any]:
    payload = read(path)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


def atomic_write_text(path: Path, text: str) -> None:
    """Durably replace one UTF-8 text file without a partial state."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write(path: Path, payload: Any) -> None:
    """Durably replace one JSON document without exposing a partial file."""

    atomic_write_text(
        path,
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
