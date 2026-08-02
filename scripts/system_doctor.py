#!/usr/bin/env python3
"""Read-only recovery doctor for the X autopilot system."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from scripts import (
        automation_toml,
        codex_thread_state,
        json_contract,
        keychain_bundle,
        personality_policy,
        system_doctor_contract,
        system_doctor_runtime,
    )
except ModuleNotFoundError:
    import automation_toml  # type: ignore[no-redef]
    import codex_thread_state  # type: ignore[no-redef]
    import json_contract  # type: ignore[no-redef]
    import keychain_bundle  # type: ignore[no-redef]
    import personality_policy  # type: ignore[no-redef]
    import system_doctor_contract  # type: ignore[no-redef]
    import system_doctor_runtime  # type: ignore[no-redef]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = PROJECT_ROOT / "recovery" / "system-contract.json"
DEFAULT_CONFIG = PROJECT_ROOT / "config.json"
VALID_STATUSES = system_doctor_runtime.VALID_STATUSES
REASONING_EFFORT_ORDER = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
ACTIVE_REPAIR_STATUSES = system_doctor_runtime.ACTIVE_REPAIR_STATUSES
ACTIVE_X_DELIVERY_STATUSES = (
    system_doctor_runtime.ACTIVE_X_DELIVERY_STATUSES
)
SUPPORTED_CONTRACT_SCHEMA_VERSIONS = {1, 2}


def contract_schema_version(contract: dict[str, Any]) -> int:
    version = contract.get("schema_version")
    if version not in SUPPORTED_CONTRACT_SCHEMA_VERSIONS:
        raise ValueError("unsupported recovery contract schema")
    return int(version)
ACTIVE_EVENT_DISPATCH_STATUSES = (
    system_doctor_runtime.ACTIVE_EVENT_DISPATCH_STATUSES
)
CLAIM_COMPLETED_DISPATCH_REASON = (
    system_doctor_runtime.CLAIM_COMPLETED_DISPATCH_REASON
)
Check = system_doctor_runtime.Check


def read_json(path: Path) -> Any:
    return json_contract.read(path)


def resolve_project_path(root: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (root / candidate)


def resolve_home_path(home: Path, value: str) -> Path:
    if value.startswith(".codex/"):
        return home / value
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (home / candidate)


def parse_simple_toml(path: Path) -> dict[str, Any]:
    payload = automation_toml.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"TOML root must be a table: {path}")
    return payload


def tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for candidate in sorted(path.rglob("*")):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(path)
        if "__pycache__" in relative.parts or candidate.suffix == ".pyc":
            continue
        digest.update(str(relative).encode("utf-8"))
        digest.update(b"\0")
        digest.update(candidate.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def state_database(home: Path) -> Path | None:
    return codex_thread_state.state_database(home / ".codex")


parse_timestamp = system_doctor_runtime.parse_timestamp
completion_dispatch_progress = (
    system_doctor_runtime.completion_dispatch_progress
)


def oldest_queue_event_id(events: list[Any]) -> str | None:
    observed: list[tuple[str, datetime]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("id") or event.get("event_id") or "")
        first_seen = parse_timestamp(event.get("first_seen_at"))
        if event_id and first_seen is not None:
            observed.append((event_id, first_seen))
    if not observed:
        return None
    return min(observed, key=lambda item: item[1])[0]


def latest_poll_context(database: Path) -> dict[str, Any]:
    uri = f"{database.resolve().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            row = connection.execute(
                """
                SELECT source, status, completed_at, error_class, error_message
                FROM poll_runs
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            tail_success = connection.execute(
                "SELECT value FROM meta WHERE key = ?",
                ("conversation_tail_last_success_at",),
            ).fetchone()
    except sqlite3.Error:
        return {}
    return {
        "latest_source": row[0] if row else None,
        "latest_status": row[1] if row else None,
        "latest_completed_at": row[2] if row else None,
        "latest_error_class": row[3] if row else None,
        "latest_error_message": row[4] if row else None,
        "conversation_tail_last_success_at": (
            tail_success[0] if tail_success else None
        ),
    }


def database_integrity(
    database: Path,
    *,
    attempts: int = 3,
    retry_delay_seconds: float = 0.1,
) -> tuple[bool, dict[str, Any] | None]:
    if attempts < 1:
        raise ValueError("database integrity attempts must be positive")
    uri = f"{database.resolve().as_uri()}?mode=ro"
    for attempt in range(1, attempts + 1):
        try:
            with closing(
                sqlite3.connect(uri, uri=True, timeout=5.0)
            ) as connection:
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute("BEGIN")
                quick_rows = [
                    str(row[0])
                    for row in connection.execute(
                        "PRAGMA quick_check"
                    ).fetchall()
                ]
                foreign_rows = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                connection.rollback()
            healthy = quick_rows == ["ok"] and not foreign_rows
            if healthy:
                return True, None
            return False, {
                "attempts": attempt,
                "foreign_key_violation_count": len(foreign_rows),
                "quick_check": quick_rows[:20],
            }
        except sqlite3.Error as error:
            message = str(error)
            lowered = message.lower()
            transient = any(
                marker in lowered
                for marker in (
                    "database is locked",
                    "database table is locked",
                    "database schema is locked",
                    "database is busy",
                )
            )
            retryable = transient or "unable to open database file" in lowered
            if retryable and attempt < attempts:
                time.sleep(retry_delay_seconds)
                continue
            return False, {
                "attempts": attempt,
                "error_class": type(error).__name__,
                "error_message": message,
                "retryable": retryable,
                "transient": transient,
            }
    raise AssertionError("database integrity retry loop did not return")


relay_progress_check = system_doctor_runtime.relay_progress_check
queue_latency_check = system_doctor_runtime.queue_latency_check


def reasoning_effort_meets_minimum(
    actual: Any,
    minimum: Any,
) -> bool:
    try:
        actual_rank = REASONING_EFFORT_ORDER.index(str(actual))
        minimum_rank = REASONING_EFFORT_ORDER.index(str(minimum))
    except ValueError:
        return False
    return actual_rank >= minimum_rank


def thread_row(database: Path, thread_id: str) -> dict[str, Any] | None:
    codex_home = database.parent
    actual_database = codex_thread_state.state_database(codex_home)
    if actual_database is None or actual_database.resolve() != database.resolve():
        raise ValueError("Codex state database changed during doctor run")
    return codex_thread_state.thread_row(codex_home, thread_id)


def git_origin(root: Path) -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def launchagent_loaded(label: str) -> tuple[bool, str]:
    target = f"gui/{os.getuid()}/{label}"
    completed = subprocess.run(
        ["/bin/launchctl", "print", target],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    detail = completed.stderr.strip() or completed.stdout.strip()
    return completed.returncode == 0, detail[-500:]


def check_contract(
    root: Path,
    home: Path,
    contract: dict[str, Any],
    config_path: Path,
) -> list[Check]:
    dependencies = system_doctor_contract.Dependencies(
        make_check=Check,
        read_json=read_json,
        resolve_project_path=resolve_project_path,
        resolve_home_path=resolve_home_path,
        state_database=state_database,
        thread_row=thread_row,
        reasoning_effort_meets_minimum=reasoning_effort_meets_minimum,
        parse_simple_toml=parse_simple_toml,
        tree_digest=tree_digest,
        git_origin=git_origin,
        launchagent_loaded=launchagent_loaded,
        database_integrity=database_integrity,
        parse_timestamp=parse_timestamp,
        relay_progress_check=relay_progress_check,
        oldest_queue_event_id=oldest_queue_event_id,
        queue_latency_check=queue_latency_check,
        latest_poll_context=latest_poll_context,
        keychain_bundle=keychain_bundle,
        personality_policy=personality_policy,
    )
    return system_doctor_contract.check_contract(
        root,
        home,
        contract,
        config_path,
        dependencies=dependencies,
    )


def render_markdown(
    contract: dict[str, Any],
    checks: Iterable[Check],
) -> str:
    items = list(checks)
    counts = {
        status: sum(item.status == status for item in items)
        for status in ("pass", "warn", "fail")
    }
    generated = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    lines = [
        f"# System doctor: {contract['system_id']}",
        "",
        f"Проверено: `{generated}`",
        "",
        (
            f"Итог: PASS {counts['pass']}, WARN {counts['warn']}, "
            f"FAIL {counts['fail']}"
        ),
        "",
        "| Проверка | Статус | Результат |",
        "|---|---:|---|",
    ]
    for item in items:
        lines.append(
            f"| `{item.identifier}` | {item.status.upper()} | "
            f"{item.summary} |"
        )
    repairs = [item for item in items if item.status != "pass" and item.repair]
    if repairs:
        lines.extend(["", "## План восстановления", ""])
        for item in repairs:
            lines.append(
                f"1. `{item.identifier}`: {item.repair}"
            )
    return "\n".join(lines) + "\n"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--root", type=Path, default=PROJECT_ROOT)
    root.add_argument("--home", type=Path, default=Path.home())
    root.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    root.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    root.add_argument("--json", action="store_true")
    root.add_argument("--report", type=Path)
    root.add_argument("--strict-warnings", action="store_true")
    return root


def main() -> int:
    arguments = parser().parse_args()
    root = arguments.root.expanduser().resolve()
    home = arguments.home.expanduser().resolve()
    contract_path = arguments.contract.expanduser().resolve()
    config_path = arguments.config.expanduser().resolve()
    contract = read_json(contract_path)
    contract_version = contract_schema_version(contract)
    checks = check_contract(root, home, contract, config_path)
    payload = {
        "schema_version": contract_version,
        "system_id": contract["system_id"],
        "checks": [asdict(item) for item in checks],
        "summary": {
            status: sum(item.status == status for item in checks)
            for status in ("pass", "warn", "fail")
        },
    }
    output = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if arguments.json
        else render_markdown(contract, checks)
    )
    print(output, end="")
    if arguments.report:
        report = arguments.report.expanduser().resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(output, encoding="utf-8")
    if payload["summary"]["fail"]:
        return 1
    if arguments.strict_warnings and payload["summary"]["warn"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
