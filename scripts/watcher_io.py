#!/usr/bin/env python3
"""Atomic JSON file primitives for the X watcher."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts import json_contract


atomic_write_json = json_contract.atomic_write


def read_json(path: Path) -> dict[str, Any]:
    return json_contract.read_object(path)
