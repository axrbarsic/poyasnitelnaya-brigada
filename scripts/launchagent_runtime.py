#!/usr/bin/env python3
"""Validate the Python and SQLite runtime used by macOS LaunchAgents."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    from scripts import json_contract
except ModuleNotFoundError:
    import json_contract  # type: ignore[no-redef]


DEFAULT_MINIMUM_PYTHON = "3.10.0"
DEFAULT_MINIMUM_SQLITE = "3.51.3"
PROBE_CODE = (
    "import json,sqlite3,sys;"
    "print(json.dumps({"
    "'python_version':'.'.join(map(str,sys.version_info[:3])),"
    "'sqlite_version':sqlite3.sqlite_version"
    "},sort_keys=True))"
)


def parse_version(value: str) -> tuple[int, int, int]:
    parts = value.strip().split(".")
    if len(parts) < 3:
        raise ValueError(f"Invalid semantic version: {value!r}")
    try:
        return tuple(int(part) for part in parts[:3])  # type: ignore[return-value]
    except ValueError as error:
        raise ValueError(f"Invalid semantic version: {value!r}") from error


@dataclass(frozen=True)
class RuntimeInfo:
    executable: Path
    python_version: str
    sqlite_version: str

    def as_dict(self) -> dict[str, str]:
        return {
            "executable": str(self.executable),
            "python_version": self.python_version,
            "sqlite_version": self.sqlite_version,
        }


def configured_executable(
    config: Mapping[str, Any],
    *,
    fallback: Path | None = None,
) -> Path:
    raw = str(config.get("launchagent_python_executable", "")).strip()
    candidate = Path(raw).expanduser() if raw else (fallback or Path(sys.executable))
    if not candidate.is_absolute():
        raise ValueError("launchagent_python_executable must be absolute")
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ValueError(
            f"LaunchAgent Python is not executable: {candidate}"
        )
    return candidate


def probe(executable: Path) -> RuntimeInfo:
    completed = subprocess.run(
        [str(executable), "-c", PROBE_CODE],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(
            f"LaunchAgent Python probe failed: {detail[-1000:]}"
        )
    payload = json_contract.loads(
        completed.stdout,
        source=f"runtime probe for {executable}",
    )
    if not isinstance(payload, dict):
        raise ValueError("LaunchAgent Python probe returned non-object JSON")
    python_version = str(payload.get("python_version", "")).strip()
    sqlite_version = str(payload.get("sqlite_version", "")).strip()
    parse_version(python_version)
    parse_version(sqlite_version)
    return RuntimeInfo(
        executable=executable,
        python_version=python_version,
        sqlite_version=sqlite_version,
    )


def require_safe(
    executable: Path,
    *,
    minimum_python: str = DEFAULT_MINIMUM_PYTHON,
    minimum_sqlite: str = DEFAULT_MINIMUM_SQLITE,
) -> RuntimeInfo:
    runtime = probe(executable)
    if parse_version(runtime.python_version) < parse_version(minimum_python):
        raise ValueError(
            "LaunchAgent Python is too old: "
            f"{runtime.python_version} < {minimum_python}"
        )
    if parse_version(runtime.sqlite_version) < parse_version(minimum_sqlite):
        raise ValueError(
            "LaunchAgent SQLite is too old: "
            f"{runtime.sqlite_version} < {minimum_sqlite}"
        )
    return runtime
