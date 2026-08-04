#!/usr/bin/env python3
"""Parse the flat TOML emitted for Codex automations on Python 3.9+."""

from __future__ import annotations

import json
import re
from typing import Any


ASSIGNMENT = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=\s*(.*?)\s*$")
INTEGER = re.compile(r"^[+-]?[0-9](?:_?[0-9])*$")
FLOAT = re.compile(
    r"^[+-]?(?:[0-9](?:_?[0-9])*)?\."
    r"[0-9](?:_?[0-9])*(?:[eE][+-]?[0-9](?:_?[0-9])*)?$"
)
INLINE_KEY = re.compile(r"\s*([A-Za-z0-9_-]+)\s*=\s*")


def _inline_table(raw: str, *, line_number: int) -> dict[str, Any]:
    inner = raw[1:-1]
    payload: dict[str, Any] = {}
    position = 0
    while position < len(inner):
        match = INLINE_KEY.match(inner, position)
        if match is None:
            raise ValueError(
                f"unsupported TOML inline table on line {line_number}"
            )
        key = match.group(1)
        if key in payload:
            raise ValueError(
                f"duplicate inline TOML key {key} on line {line_number}"
            )
        value_start = match.end()
        quote = inner[value_start : value_start + 1]
        if quote not in {'"', "'"}:
            raise ValueError(
                f"unsupported inline TOML value on line {line_number}"
            )
        escaped = False
        value_end = value_start + 1
        while value_end < len(inner):
            character = inner[value_end]
            if quote == '"' and character == "\\" and not escaped:
                escaped = True
                value_end += 1
                continue
            if character == quote and not escaped:
                value_end += 1
                break
            escaped = False
            value_end += 1
        else:
            raise ValueError(
                f"unterminated inline TOML string on line {line_number}"
            )
        payload[key] = _value(
            inner[value_start:value_end],
            line_number=line_number,
        )
        position = value_end
        while position < len(inner) and inner[position].isspace():
            position += 1
        if position == len(inner):
            break
        if inner[position] != ",":
            raise ValueError(
                f"unsupported TOML inline table on line {line_number}"
            )
        position += 1
    return payload


def _value(raw: str, *, line_number: int) -> Any:
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"invalid TOML basic string on line {line_number}"
            ) from error
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    if raw in {"true", "false"}:
        return raw == "true"
    if INTEGER.fullmatch(raw):
        return int(raw.replace("_", ""))
    if FLOAT.fullmatch(raw):
        return float(raw.replace("_", ""))
    if raw.startswith("[") and raw.endswith("]"):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"unsupported TOML array on line {line_number}"
            ) from error
        if isinstance(value, list):
            return value
    if raw.startswith("{") and raw.endswith("}"):
        return _inline_table(raw, line_number=line_number)
    raise ValueError(f"unsupported TOML value on line {line_number}")


def loads(text: str) -> dict[str, Any]:
    """Parse the flat scalar/array subset written by automation_update."""

    payload: dict[str, Any] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ASSIGNMENT.fullmatch(line)
        if match is None:
            raise ValueError(
                f"automation TOML is not a flat assignment on line "
                f"{line_number}"
            )
        key, raw = match.groups()
        if key in payload:
            raise ValueError(f"duplicate TOML key {key} on line {line_number}")
        payload[key] = _value(raw, line_number=line_number)
    return payload
