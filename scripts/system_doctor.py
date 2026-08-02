#!/usr/bin/env python3
"""Read-only recovery doctor for the X autopilot system."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from scripts import (
        keychain_bundle,
        personality_policy,
        system_doctor_contract,
    )
except ModuleNotFoundError:
    import keychain_bundle  # type: ignore[no-redef]
    import personality_policy  # type: ignore[no-redef]
    import system_doctor_contract  # type: ignore[no-redef]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = PROJECT_ROOT / "recovery" / "system-contract.json"
DEFAULT_CONFIG = PROJECT_ROOT / "config.json"
VALID_STATUSES = {"pass", "warn", "fail"}
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
ACTIVE_REPAIR_STATUSES = {
    "repair_observing",
    "escalation_pending",
    "handoff_pending",
    "claimed",
    "work_in_progress",
}
ACTIVE_X_DELIVERY_STATUSES = {
    "desktop_launched_waiting_relay",
    "desktop_ready_waiting_relay",
}
ACTIVE_EVENT_DISPATCH_STATUSES = {
    "dispatch_requested",
    "dispatch_kicked",
}
CLAIM_COMPLETED_DISPATCH_REASON = "claim_completed_with_pending_queue"


@dataclass(frozen=True)
class Check:
    identifier: str
    status: str
    summary: str
    repair: str = ""
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"invalid check status: {self.status}")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_project_path(root: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (root / candidate)


def resolve_home_path(home: Path, value: str) -> Path:
    if value.startswith(".codex/"):
        return home / value
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (home / candidate)


def parse_simple_toml(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*=\s*(.+)$", line)
        if match is None:
            continue
        key, raw_value = match.groups()
        try:
            result[key] = ast.literal_eval(raw_value)
        except (SyntaxError, ValueError):
            result[key] = raw_value
    return result


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
    candidates = sorted(
        (home / ".codex").glob("state_*.sqlite"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def completion_dispatch_progress(
    state: Any,
    *,
    current: datetime,
    grace_seconds: int,
    required_event_id: str | None,
) -> dict[str, Any]:
    """Classify one durable post-completion delivery request."""

    delivery = state if isinstance(state, dict) else {}
    status = str(delivery.get("status", ""))
    reason = str(delivery.get("reason", ""))
    raw_event_ids = delivery.get("event_ids")
    event_ids = (
        {
            str(event_id)
            for event_id in raw_event_ids
            if str(event_id)
        }
        if isinstance(raw_event_ids, list)
        else set()
    )
    requested_at = parse_timestamp(delivery.get("requested_at"))
    age_seconds = (
        (current - requested_at).total_seconds()
        if requested_at is not None
        else None
    )
    covers_required = (
        required_event_id is None or required_event_id in event_ids
    )
    active = (
        grace_seconds > 0
        and status in ACTIVE_EVENT_DISPATCH_STATUSES
        and reason == CLAIM_COMPLETED_DISPATCH_REASON
        and bool(event_ids)
        and covers_required
        and age_seconds is not None
        and 0 <= age_seconds <= grace_seconds
    )
    return {
        "active": active,
        "status": status,
        "reason": reason,
        "event_ids": sorted(event_ids),
        "covers_required": covers_required,
        "age_seconds": age_seconds,
    }


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


def relay_progress_check(
    *,
    pending_count: int,
    dispatch_state: dict[str, Any],
    owner: Any,
    event_dispatch_state: Any = None,
    required_event_id: str | None = None,
    max_wait_seconds: int,
    now: datetime | None = None,
) -> Check:
    if max_wait_seconds <= 0:
        raise ValueError("relay progress max wait must be positive")
    current = now or datetime.now(timezone.utc)
    dispatch_status = str(dispatch_state.get("status", "missing"))
    details: dict[str, Any] = {
        "dispatch_status": dispatch_status,
        "max_wait_seconds": max_wait_seconds,
        "pending_count": pending_count,
    }
    work_kind = str(dispatch_state.get("work_kind", ""))
    if work_kind:
        details["work_kind"] = work_kind
    if pending_count <= 0:
        return Check(
            "runtime.relay_progress",
            "pass",
            "Relay не имеет ожидающей очереди.",
            "Проверь wake queue и dispatcher state.",
            details,
        )
    if isinstance(owner, dict):
        details["owner_event_ids"] = owner.get("event_ids", [])
        return Check(
            "runtime.relay_progress",
            "pass",
            "Ожидающая очередь уже принадлежит Browser owner.",
            "Проверь owner lease и session janitor.",
            details,
        )
    if work_kind == "repair" and dispatch_status in ACTIVE_REPAIR_STATUSES:
        return Check(
            "runtime.relay_progress",
            "pass",
            "Relay выполняет активный repair handoff.",
            "Возраст X очереди контролируется отдельным SLO.",
            details,
        )
    if dispatch_status == "repair_waiting":
        return Check(
            "runtime.relay_progress",
            "pass",
            "Relay намеренно уступил очередь активному repair owner.",
            "Проверь supervisor lease и runtime.queue_latency.",
            details,
        )
    if dispatch_status == "deferred_resources":
        return Check(
            "runtime.relay_progress",
            "pass",
            "Relay намеренно отложен resource guard.",
            "Проверь возраст очереди и освободи только безопасные ресурсы.",
            details,
        )
    completion_delivery = completion_dispatch_progress(
        event_dispatch_state,
        current=current,
        grace_seconds=max_wait_seconds,
        required_event_id=required_event_id,
    )
    if completion_delivery["status"]:
        details.update(
            {
                "event_dispatch_status": completion_delivery["status"],
                "event_dispatch_reason": completion_delivery["reason"],
                "event_dispatch_covers_oldest": completion_delivery[
                    "covers_required"
                ],
                "event_dispatch_age_seconds": (
                    round(completion_delivery["age_seconds"], 1)
                    if completion_delivery["age_seconds"] is not None
                    else None
                ),
            }
        )
    if completion_delivery["active"]:
        return Check(
            "runtime.relay_progress",
            "pass",
            "Следующий X-handoff уже запрошен после завершения claim.",
            "Дождись self-owned heartbeat в пределах grace; затем снова "
            "проверь очередь.",
            details,
        )
    if dispatch_status not in ACTIVE_X_DELIVERY_STATUSES:
        return Check(
            "runtime.relay_progress",
            "fail",
            "Ожидающая очередь не получила owner claim.",
            "Проверь dispatcher, self-owned heartbeat x-relay и reservation.",
            details,
        )
    waiting_since = parse_timestamp(
        dispatch_state.get("waiting_since")
        or dispatch_state.get("checked_at")
    )
    if waiting_since is None:
        details["waiting_since"] = dispatch_state.get("waiting_since")
        return Check(
            "runtime.relay_progress",
            "fail",
            "Dispatcher не записал начало ожидания owner heartbeat.",
            "Перезапусти штатный dispatcher и проверь waiting_since.",
            details,
        )
    age_seconds = (current - waiting_since).total_seconds()
    details["waiting_since"] = waiting_since.isoformat().replace(
        "+00:00",
        "Z",
    )
    details["age_seconds"] = round(age_seconds, 1)
    healthy = 0 <= age_seconds <= max_wait_seconds
    return Check(
        "runtime.relay_progress",
        "pass" if healthy else "fail",
        (
            f"Heartbeat ожидает claim {round(age_seconds, 1)}s."
            if healthy
            else f"Heartbeat не создал claim за {round(age_seconds, 1)}s."
        ),
        (
            "Дождись ближайшего минутного heartbeat."
            if healthy
            else "Проверь self-owned heartbeat x-relay и reservation."
        ),
        details,
    )


def queue_latency_check(
    *,
    events: list[Any],
    owner: Any,
    supervisor_state: Any = None,
    dispatch_state: Any = None,
    event_dispatch_state: Any = None,
    delivery_grace_seconds: int = 0,
    max_age_seconds: int,
    now: datetime | None = None,
) -> Check:
    """Measure queue age with one bounded grace for a scheduled X delivery."""

    if max_age_seconds <= 0:
        raise ValueError("queue latency max age must be positive")
    if delivery_grace_seconds < 0:
        raise ValueError("X delivery grace must not be negative")
    if not events:
        return Check(
            "runtime.queue_latency",
            "pass",
            "Ожидающая очередь пуста.",
            "Проверь wake queue и poll health.",
            {
                "pending_count": 0,
                "max_age_seconds": max_age_seconds,
            },
        )

    observed: list[tuple[str, datetime]] = []
    invalid_event_ids: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            invalid_event_ids.append("<invalid>")
            continue
        event_id = str(event.get("id") or event.get("event_id") or "")
        first_seen = parse_timestamp(event.get("first_seen_at"))
        if not event_id or first_seen is None:
            invalid_event_ids.append(event_id or "<missing>")
            continue
        observed.append((event_id, first_seen))
    if invalid_event_ids:
        return Check(
            "runtime.queue_latency",
            "fail",
            "Возраст части ожидающей очереди нельзя измерить.",
            "Восстанови first_seen_at из live SQLite, не удаляя события.",
            {
                "invalid_event_ids": invalid_event_ids,
                "pending_count": len(events),
                "max_age_seconds": max_age_seconds,
            },
        )

    oldest_event_id, oldest_seen = min(observed, key=lambda item: item[1])
    current = now or datetime.now(timezone.utc)
    age_seconds = (current - oldest_seen).total_seconds()
    owner_event_ids = (
        {
            str(event_id)
            for event_id in owner.get("event_ids", [])
            if str(event_id)
        }
        if isinstance(owner, dict)
        else set()
    )
    owned = oldest_event_id in owner_event_ids
    owner_lease_expires = (
        parse_timestamp(owner.get("lease_expires_at"))
        if isinstance(owner, dict)
        else None
    )
    active_owner = bool(owner_event_ids) and (
        owner_lease_expires is None or owner_lease_expires >= current
    )
    supervisor_incident = (
        supervisor_state.get("incident")
        if isinstance(supervisor_state, dict)
        else None
    )
    supervisor_owner = (
        supervisor_incident.get("owner")
        if isinstance(supervisor_incident, dict)
        else None
    )
    supervisor_lease_expires = (
        parse_timestamp(supervisor_owner.get("lease_expires_at"))
        if isinstance(supervisor_owner, dict)
        else None
    )
    active_repair = (
        isinstance(supervisor_incident, dict)
        and str(supervisor_incident.get("status", ""))
        in ACTIVE_REPAIR_STATUSES
        and isinstance(supervisor_owner, dict)
        and bool(str(supervisor_owner.get("claim_token", "")))
        and supervisor_lease_expires is not None
        and supervisor_lease_expires >= current
    )
    delivery = dispatch_state if isinstance(dispatch_state, dict) else {}
    delivery_status = str(delivery.get("status", ""))
    delivery_work_kind = str(delivery.get("work_kind", ""))
    raw_delivery_event_ids = delivery.get("event_ids")
    delivery_event_ids = (
        {
            str(event_id)
            for event_id in raw_delivery_event_ids
            if str(event_id)
        }
        if isinstance(raw_delivery_event_ids, list)
        else set()
    )
    delivery_waiting_since = parse_timestamp(delivery.get("waiting_since"))
    delivery_age_seconds = (
        (current - delivery_waiting_since).total_seconds()
        if delivery_waiting_since is not None
        else None
    )
    active_x_delivery = (
        delivery_grace_seconds > 0
        and delivery_status in ACTIVE_X_DELIVERY_STATUSES
        and delivery_work_kind == "x"
        and oldest_event_id in delivery_event_ids
        and delivery_age_seconds is not None
        and 0 <= delivery_age_seconds <= delivery_grace_seconds
    )
    completion_delivery = completion_dispatch_progress(
        event_dispatch_state,
        current=current,
        grace_seconds=delivery_grace_seconds,
        required_event_id=oldest_event_id,
    )
    active_x_delivery = (
        active_x_delivery or completion_delivery["active"]
    )
    healthy = 0 <= age_seconds <= max_age_seconds
    status = (
        "pass"
        if healthy
        else "warn"
        if active_owner or active_repair or active_x_delivery
        else "fail"
    )
    details = {
        "age_seconds": round(age_seconds, 1),
        "max_age_seconds": max_age_seconds,
        "oldest_event_id": oldest_event_id,
        "oldest_first_seen_at": oldest_seen.isoformat().replace(
            "+00:00",
            "Z",
        ),
        "active_owner": active_owner,
        "active_repair": active_repair,
        "active_x_delivery": active_x_delivery,
        "owned": owned,
        "pending_count": len(events),
    }
    if delivery_status:
        details["delivery_status"] = delivery_status
        details["delivery_work_kind"] = delivery_work_kind
        details["delivery_covers_oldest"] = (
            oldest_event_id in delivery_event_ids
        )
        details["delivery_grace_seconds"] = delivery_grace_seconds
        details["delivery_age_seconds"] = (
            round(delivery_age_seconds, 1)
            if delivery_age_seconds is not None
            else None
        )
    if completion_delivery["status"]:
        details.update(
            {
                "event_dispatch_status": completion_delivery["status"],
                "event_dispatch_reason": completion_delivery["reason"],
                "event_dispatch_covers_oldest": completion_delivery[
                    "covers_required"
                ],
                "event_dispatch_age_seconds": (
                    round(completion_delivery["age_seconds"], 1)
                    if completion_delivery["age_seconds"] is not None
                    else None
                ),
            }
        )
    if completion_delivery["active"]:
        details["delivery_source"] = "event_dispatch"
    elif active_x_delivery:
        details["delivery_source"] = "app_server_dispatch"
    if healthy:
        summary = f"Старейшее событие ожидает {round(age_seconds, 1)}s."
        repair = "Проверь relay progress при росте возраста."
    elif owned:
        summary = (
            "Старейшее событие превысило SLO, но уже принадлежит "
            "активному Browser owner."
        )
        repair = "Проверь renew, durable history и завершение текущего claim."
    elif active_owner:
        summary = (
            "Старейшее ожидающее событие превысило SLO, пока активный "
            "Browser owner обрабатывает предыдущую bounded batch."
        )
        repair = (
            "Проверь продвижение текущего claim и следующий автоматический "
            "handoff."
        )
    elif active_repair:
        summary = (
            "Старейшее ожидающее событие превысило SLO во время "
            "активного repair handoff."
        )
        repair = (
            "Заверши repair handoff, затем проверь следующий X claim."
        )
    elif active_x_delivery:
        summary = (
            "Старейшее ожидающее событие превысило SLO, но свежая "
            "X-доставка уже ожидает owner claim."
        )
        repair = (
            "Дождись owner claim в пределах relay grace; после grace "
            "зависшая очередь снова станет FAIL."
        )
    else:
        summary = "Старейшее событие превысило SLO без активного owner."
        repair = (
            "Проверь dispatcher, x-relay и owner claim, очередь не удаляй."
        )
    return Check(
        "runtime.queue_latency",
        status,
        summary,
        repair,
        details,
    )


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
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, archived, is_pinned, model, reasoning_effort, cwd
            FROM threads
            WHERE id = ?
            """,
            (thread_id,),
        ).fetchone()
    return dict(row) if row is not None else None


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
    if contract.get("schema_version") != 1:
        raise ValueError("unsupported recovery contract schema")
    checks = check_contract(root, home, contract, config_path)
    payload = {
        "schema_version": 1,
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
