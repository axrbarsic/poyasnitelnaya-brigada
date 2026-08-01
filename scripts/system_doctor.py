#!/usr/bin/env python3
"""Read-only recovery doctor for the X autopilot system."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from scripts import keychain_bundle, personality_policy
except ModuleNotFoundError:
    import keychain_bundle  # type: ignore[no-redef]
    import personality_policy  # type: ignore[no-redef]


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
    waiting_statuses = {
        "desktop_launched_waiting_relay",
        "desktop_ready_waiting_relay",
    }
    if dispatch_status == "deferred_resources":
        return Check(
            "runtime.relay_progress",
            "pass",
            "Relay намеренно отложен resource guard.",
            "Проверь возраст очереди и освободи только безопасные ресурсы.",
            details,
        )
    if dispatch_status not in waiting_statuses:
        return Check(
            "runtime.relay_progress",
            "fail",
            "Ожидающая очередь не передана relay.",
            "Проверь статус dispatcher, heartbeat x-relay и direct delivery.",
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
            "Dispatcher не записал начало ожидания relay.",
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
            f"Relay ожидает claim {round(age_seconds, 1)}s."
            if healthy
            else f"Relay не создал claim за {round(age_seconds, 1)}s."
        ),
        (
            "Дождись ближайшего минутного heartbeat."
            if healthy
            else "Проверь heartbeat x-relay, reservation и direct delivery."
        ),
        details,
    )


def queue_latency_check(
    *,
    events: list[Any],
    owner: Any,
    max_age_seconds: int,
    now: datetime | None = None,
) -> Check:
    """Measure queue age independently from dispatcher heartbeat state."""

    if max_age_seconds <= 0:
        raise ValueError("queue latency max age must be positive")
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
    healthy = 0 <= age_seconds <= max_age_seconds
    status = "pass" if healthy else "warn" if owned else "fail"
    details = {
        "age_seconds": round(age_seconds, 1),
        "max_age_seconds": max_age_seconds,
        "oldest_event_id": oldest_event_id,
        "oldest_first_seen_at": oldest_seen.isoformat().replace(
            "+00:00",
            "Z",
        ),
        "owned": owned,
        "pending_count": len(events),
    }
    if healthy:
        summary = f"Старейшее событие ожидает {round(age_seconds, 1)}s."
        repair = "Проверь relay progress при росте возраста."
    elif owned:
        summary = (
            "Старейшее событие превысило SLO, но уже принадлежит "
            "активному Browser owner."
        )
        repair = "Проверь renew, durable history и завершение текущего claim."
    else:
        summary = "Старейшее событие превысило SLO без активного owner."
        repair = "Проверь dispatcher, x-relay и owner routing, очередь не удаляй."
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
    checks: list[Check] = []
    expected_root = Path(str(contract["canonical_root"])).expanduser()
    checks.append(
        Check(
            "project.root",
            "pass" if root.resolve() == expected_root.resolve() else "fail",
            (
                "Канонический каталог совпадает."
                if root.resolve() == expected_root.resolve()
                else f"Открыт другой каталог: {root.resolve()}"
            ),
            f"Открой проект из {expected_root}.",
        )
    )

    missing = [
        value
        for value in contract.get("required_files", [])
        if not (root / str(value)).is_file()
    ]
    checks.append(
        Check(
            "project.files",
            "pass" if not missing else "fail",
            (
                "Все обязательные файлы каркаса на месте."
                if not missing
                else "Отсутствуют обязательные файлы."
            ),
            "Восстанови отсутствующие файлы командой git pull.",
            {"missing": missing} if missing else None,
        )
    )

    try:
        actual_origin = git_origin(root)
    except (OSError, subprocess.SubprocessError):
        actual_origin = ""
    expected_origin = str(contract.get("git_origin", ""))
    checks.append(
        Check(
            "project.git_origin",
            "pass" if actual_origin == expected_origin else "fail",
            (
                "Git origin совпадает с аварийным источником."
                if actual_origin == expected_origin
                else "Git origin не совпадает с контрактом."
            ),
            f"Проверь origin и восстанови его на {expected_origin}.",
            {"actual": actual_origin, "expected": expected_origin},
        )
    )

    if not config_path.is_file():
        checks.append(
            Check(
                "config.local",
                "fail",
                "Локальный config.json отсутствует.",
                "Восстанови config.json из защищённой локальной копии, "
                "не из Git.",
            )
        )
        return checks
    config = read_json(config_path)
    owner_contract = contract["threads"]["browser_owner"]
    config_owner = str(config.get("browser_owner_thread_id", ""))
    checks.append(
        Check(
            "config.owner_thread",
            "pass" if config_owner == owner_contract["id"] else "fail",
            (
                "config.json указывает на канонический Browser owner."
                if config_owner == owner_contract["id"]
                else "config.json указывает на другой Browser owner."
            ),
            "Верни browser_owner_thread_id из recovery/system-contract.json.",
            {"actual": config_owner, "expected": owner_contract["id"]},
        )
    )
    mode = str(config.get("desktop_relay_mode", ""))
    checks.append(
        Check(
            "config.relay_mode",
            "pass" if mode == "in_app_heartbeat" else "fail",
            (
                "Активен официальный in-app heartbeat relay."
                if mode == "in_app_heartbeat"
                else f"Неожиданный relay mode: {mode}"
            ),
            "Установи desktop_relay_mode в in_app_heartbeat.",
        )
    )
    runtime_contract = contract.get("runtime", {})
    expected_event_dispatch_label = runtime_contract.get(
        "event_dispatch_launchagent_label"
    )
    if expected_event_dispatch_label is not None:
        expected_event_dispatch_label = str(expected_event_dispatch_label)
        actual_event_dispatch_label = str(
            config.get("event_dispatch_launchagent_label", "")
        )
        event_dispatch_enabled = bool(
            config.get("event_dispatch_on_new_events", False)
        )
        event_dispatch_matches = (
            event_dispatch_enabled
            and actual_event_dispatch_label == expected_event_dispatch_label
        )
        checks.append(
            Check(
                "config.event_dispatch",
                "pass" if event_dispatch_matches else "fail",
                (
                    "Новые X события немедленно запускают "
                    "dispatcher."
                    if event_dispatch_matches
                    else (
                        "Событийный запуск dispatcher не "
                        "настроен."
                    )
                ),
                (
                    "Включи event_dispatch_on_new_events и верни точный "
                    "event_dispatch_launchagent_label из контракта."
                ),
                {
                    "enabled": event_dispatch_enabled,
                    "actual_label": actual_event_dispatch_label,
                    "expected_label": expected_event_dispatch_label,
                },
            )
        )

    database = state_database(home)
    if database is None:
        checks.append(
            Check(
                "codex.state_database",
                "fail",
                "Codex state database не найдена.",
                "Запусти Codex Desktop и повтори doctor.",
            )
        )
    else:
        for role, expected in contract.get("threads", {}).items():
            try:
                row = thread_row(database, str(expected["id"]))
            except sqlite3.Error as error:
                checks.append(
                    Check(
                        f"thread.{role}",
                        "fail",
                        "Не удалось прочитать состояние сессии.",
                        "Закрой повреждённый Codex state только после snapshot.",
                        {"error": type(error).__name__},
                    )
                )
                continue
            if row is None:
                checks.append(
                    Check(
                        f"thread.{role}",
                        "fail",
                        f"Сессия {role} не найдена.",
                        "Найди сессию по ID из recovery/system-contract.json "
                        "или восстанови роль по методичке.",
                    )
                )
                continue
            mismatches: dict[str, Any] = {}
            for field in ("model", "cwd"):
                expected_value = expected.get(field)
                if expected_value is not None and row.get(field) != expected_value:
                    mismatches[field] = {
                        "actual": row.get(field),
                        "expected": expected_value,
                    }
            minimum_effort = expected.get("minimum_reasoning_effort")
            if minimum_effort is not None:
                if not reasoning_effort_meets_minimum(
                    row.get("reasoning_effort"),
                    minimum_effort,
                ):
                    mismatches["reasoning_effort"] = {
                        "actual": row.get("reasoning_effort"),
                        "minimum": minimum_effort,
                    }
            else:
                expected_effort = expected.get("reasoning_effort")
                if (
                    expected_effort is not None
                    and row.get("reasoning_effort") != expected_effort
                ):
                    mismatches["reasoning_effort"] = {
                        "actual": row.get("reasoning_effort"),
                        "expected": expected_effort,
                    }
            if expected.get("must_be_unarchived") and int(row["archived"]) != 0:
                mismatches["archived"] = {
                    "actual": bool(row["archived"]),
                    "expected": False,
                }
            checks.append(
                Check(
                    f"thread.{role}",
                    "pass" if not mismatches else "fail",
                    (
                        f"Сессия {role} соответствует роли."
                        if not mismatches
                        else f"Сессия {role} расходится с контрактом."
                    ),
                    "Восстанови модель, effort, cwd и снимите архивный флаг "
                    "через Codex Desktop.",
                    mismatches or None,
                )
            )
            if expected.get("pin_recommended"):
                checks.append(
                    Check(
                        f"thread.{role}.pinned",
                        "pass" if int(row["is_pinned"]) == 1 else "warn",
                        (
                            "Главная сессия закреплена."
                            if int(row["is_pinned"]) == 1
                            else "Главная сессия не закреплена."
                        ),
                        "Закрепи каноническую главную сессию в Codex.",
                    )
                )

    automation_contract = contract.get("automations", {})
    for name, expected in automation_contract.items():
        automation_id = str(expected["id"])
        automation_path = (
            home
            / ".codex"
            / "automations"
            / automation_id
            / "automation.toml"
        )
        if not automation_path.is_file():
            checks.append(
                Check(
                    f"automation.{name}",
                    "fail",
                    f"Automation {automation_id} отсутствует.",
                    "Восстанови automation через официальный automation_update.",
                )
            )
            continue
        actual = parse_simple_toml(automation_path)
        metadata_keys = {"prompt_source"}
        mismatches = {
            key: {"actual": actual.get(key), "expected": value}
            for key, value in expected.items()
            if key not in metadata_keys and actual.get(key) != value
        }
        prompt_source = str(expected.get("prompt_source", "")).strip()
        if prompt_source:
            try:
                expected_prompt = resolve_project_path(
                    root, prompt_source
                ).read_text(encoding="utf-8").rstrip("\n")
            except OSError:
                expected_prompt = None
            if actual.get("prompt") != expected_prompt:
                mismatches["prompt"] = {
                    "actual_sha256": hashlib.sha256(
                        str(actual.get("prompt", "")).encode("utf-8")
                    ).hexdigest(),
                    "expected_sha256": (
                        hashlib.sha256(
                            expected_prompt.encode("utf-8")
                        ).hexdigest()
                        if expected_prompt is not None
                        else None
                    ),
                }
        checks.append(
            Check(
                f"automation.{name}",
                "pass" if not mismatches else "fail",
                (
                    f"Automation {automation_id} соответствует контракту."
                    if not mismatches
                    else f"Automation {automation_id} расходится с контрактом."
                ),
                "Обнови automation только через официальный automation_update.",
                mismatches or None,
            )
        )

    expected_programs = contract.get("launch_agent_programs", {})
    for label in contract.get("launch_agents", []):
        installed = home / "Library" / "LaunchAgents" / f"{label}.plist"
        if not installed.is_file():
            checks.append(
                Check(
                    f"launchagent.{label}",
                    "fail",
                    f"LaunchAgent {label} не установлен.",
                    "Перерендери macOS plists в staging, проверь их и "
                    "установи штатным launchctl bootstrap.",
                )
            )
            continue
        try:
            payload = plistlib.loads(installed.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            payload = {}
        expected_program = str(expected_programs.get(label, "")).strip()
        arguments = payload.get("ProgramArguments", [])
        program_ok = (
            not expected_program
            or (
                isinstance(arguments, list)
                and any(
                    str(argument).endswith(expected_program)
                    for argument in arguments
                )
            )
        )
        installed_ok = payload.get("Label") == label and program_ok
        checks.append(
            Check(
                f"launchagent.{label}",
                "pass" if installed_ok else "fail",
                (
                    f"LaunchAgent {label} установлен."
                    if installed_ok
                    else f"LaunchAgent {label} повреждён или устарел."
                ),
                "Перерендери и переустанови только этот LaunchAgent.",
                (
                    {
                        "expected_program": expected_program,
                        "program_arguments": arguments,
                    }
                    if not program_ok
                    else None
                ),
            )
        )
        if sys.platform == "darwin" and home == Path.home().resolve():
            try:
                loaded, detail = launchagent_loaded(label)
            except (OSError, subprocess.SubprocessError):
                loaded, detail = False, ""
            checks.append(
                Check(
                    f"launchagent.{label}.loaded",
                    "pass" if loaded else "fail",
                    (
                        f"LaunchAgent {label} загружен в launchd."
                        if loaded
                        else f"LaunchAgent {label} не загружен."
                    ),
                    "Выполни bootstrap проверенного plist, затем kickstart.",
                    {"launchctl": detail} if not loaded and detail else None,
                )
            )

    runtime = contract.get("runtime", {})
    live_database = resolve_project_path(root, str(runtime["database"]))
    if not live_database.is_file():
        checks.append(
            Check(
                "runtime.database",
                "fail",
                "Live SQLite отсутствует.",
                "Восстанови последний валидный snapshot, не создавай пустую "
                "базу поверх потери.",
            )
        )
    else:
        healthy, integrity_details = database_integrity(live_database)
        database_unavailable = bool(
            integrity_details
            and (
                integrity_details.get("transient")
                or integrity_details.get("retryable")
            )
        )
        database_status = (
            "pass"
            if healthy
            else "warn"
            if database_unavailable
            else "fail"
        )
        checks.append(
            Check(
                "runtime.database",
                database_status,
                (
                    "Live SQLite проходит quick_check и foreign_key_check."
                    if healthy
                    else (
                        "Live SQLite временно занята или недоступна, "
                        "повреждение не подтверждено."
                    )
                    if database_unavailable
                    else "Live SQLite не прошла проверку целостности."
                ),
                (
                    "Повтори штатную проверку после завершения активной "
                    "транзакции или восстановления доступа к файлу."
                    if database_unavailable
                    else "Останови poll, сделай snapshot повреждённого файла "
                    "и восстанови последний валидный snapshot."
                ),
                integrity_details,
            )
        )

    wake_path = resolve_project_path(root, str(runtime["wake_file"]))
    events: list[dict[str, Any]] = []
    try:
        wake = read_json(wake_path)
        events = wake.get("events", [])
        pending = int(wake.get("pending_count", -1))
        event_ids = [
            str(event.get("id") or event.get("event_id"))
            for event in events
            if isinstance(event, dict)
        ]
        queue_healthy = (
            isinstance(events, list)
            and pending == len(events)
            and len(event_ids) == len(set(event_ids))
        )
    except (OSError, ValueError, TypeError, AttributeError):
        queue_healthy = False
        pending = -1
    checks.append(
        Check(
            "runtime.queue",
            "pass" if queue_healthy else "fail",
            (
                f"Очередь согласована, pending={pending}."
                if queue_healthy
                else "Wake queue повреждена или несогласована."
            ),
            "Не удаляй очередь. Повтори model-free poll и сравни SQLite "
            "с wake-request.json.",
        )
    )

    dispatch_state: dict[str, Any] = {}
    dispatch_value = str(runtime.get("dispatch_state_file", "")).strip()
    if dispatch_value:
        dispatch_path = resolve_project_path(root, dispatch_value)
        dispatch_max_age = int(
            runtime.get("max_dispatch_age_seconds", 180)
        )
        try:
            dispatch_state = read_json(dispatch_path)
            dispatch_checked = parse_timestamp(
                dispatch_state.get("checked_at")
            )
            dispatch_age = (
                (datetime.now(timezone.utc) - dispatch_checked).total_seconds()
                if dispatch_checked is not None
                else float("inf")
            )
            dispatch_ok = 0 <= dispatch_age <= dispatch_max_age
        except (OSError, ValueError, TypeError, AttributeError):
            dispatch_ok = False
            dispatch_age = float("inf")
        checks.append(
            Check(
                "runtime.dispatch_health",
                "pass" if dispatch_ok else "fail",
                (
                    f"Dispatcher свежий, age={round(dispatch_age, 1)}s."
                    if dispatch_ok
                    else "Dispatcher не обновляет durable state."
                ),
                "Проверь loaded dispatch LaunchAgent и выполни его kickstart.",
                {
                    "age_seconds": (
                        round(dispatch_age, 1)
                        if dispatch_age != float("inf")
                        else None
                    ),
                    "max_age_seconds": dispatch_max_age,
                },
            )
        )

    owner: Any = None
    owner_state_value = str(
        runtime.get("autopilot_state_file", "")
    ).strip()
    if owner_state_value:
        owner_state_path = resolve_project_path(root, owner_state_value)
        try:
            owner_state = read_json(owner_state_path)
            owner = owner_state.get("owner")
            if owner is None:
                owner_lease_ok = True
                owner_details = {"owner": None}
            elif isinstance(owner, dict):
                lease_expires = parse_timestamp(
                    owner.get("lease_expires_at")
                )
                owner_lease_ok = (
                    lease_expires is not None
                    and lease_expires >= datetime.now(timezone.utc)
                )
                owner_details = {
                    "event_ids": owner.get("event_ids", []),
                    "lease_expires_at": owner.get("lease_expires_at"),
                }
            else:
                owner_lease_ok = False
                owner_details = {"owner_type": type(owner).__name__}
        except (OSError, ValueError, TypeError, AttributeError):
            owner_lease_ok = False
            owner_details = {"state": "unreadable"}
        checks.append(
            Check(
                "runtime.owner_lease",
                "pass" if owner_lease_ok else "fail",
                (
                    "Browser owner lease согласован."
                    if owner_lease_ok
                    else "Browser owner lease протух или повреждён."
                ),
                "Запусти штатный session janitor recovery, не удаляй очередь.",
                owner_details,
            )
        )
    max_relay_wait = int(runtime.get("max_relay_wait_seconds", 0))
    if max_relay_wait > 0:
        checks.append(
            relay_progress_check(
                pending_count=pending,
                dispatch_state=dispatch_state,
                owner=owner,
                max_wait_seconds=max_relay_wait,
            )
        )
    max_queue_age = int(runtime.get("max_queue_age_seconds", 0))
    if max_queue_age > 0 and isinstance(events, list):
        checks.append(
            queue_latency_check(
                events=events,
                owner=owner,
                max_age_seconds=max_queue_age,
            )
        )

    helper = resolve_project_path(root, str(runtime["keychain_helper"]))
    helper_ok = helper.is_file() and os.access(helper, os.X_OK)
    checks.append(
        Check(
            "runtime.keychain_helper",
            "pass" if helper_ok else "fail",
            (
                "Keychain helper установлен и исполняем."
                if helper_ok
                else "Keychain helper отсутствует или не исполняем."
            ),
            "Выполни scripts/install_keychain_helper.sh.",
        )
    )
    keychain_service = str(config.get("keychain_service", "")).strip()
    keychain_account = str(config.get("keychain_account", "")).strip()
    if (
        sys.platform == "darwin"
        and helper_ok
        and keychain_service
        and keychain_account
    ):
        try:
            bundle_verification = keychain_bundle.verify_bundle(helper)
            bundle_ok = bundle_verification.ok
            bundle_details = {
                "app_path": bundle_verification.app_path,
                "bundle_id": bundle_verification.bundle_id,
                "application_id": bundle_verification.application_id,
                "profile_name": bundle_verification.profile_name,
                "profile_expires_at": (
                    bundle_verification.profile_expires_at
                ),
                "errors": list(bundle_verification.errors),
            }
        except Exception as error:
            bundle_ok = False
            bundle_details = {
                "errors": [
                    "verifier raised "
                    + type(error).__name__
                ]
            }
        checks.append(
            Check(
                "runtime.keychain_bundle",
                "pass" if bundle_ok else "fail",
                (
                    "Keychain helper подписан и авторизован профилем."
                    if bundle_ok
                    else "Keychain helper не имеет валидного app-like bundle."
                ),
                "Добавь Apple Account в Xcode, затем выполни "
                "scripts/install_keychain_helper.sh.",
                bundle_details,
            )
        )
        accessibility_ok = False
        accessibility_details: dict[str, Any] = {
            "bundle_verified": bundle_ok,
        }
        if bundle_ok:
            try:
                verified_helper = Path(
                    bundle_verification.executable_path
                )
                accessibility = subprocess.run(
                    [
                        str(verified_helper),
                        "is-after-first-unlock",
                        keychain_service,
                        keychain_account,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                accessibility_ok = accessibility.returncode == 0
                accessibility_details["returncode"] = (
                    accessibility.returncode
                )
            except (OSError, subprocess.SubprocessError) as error:
                accessibility_details["error_class"] = type(error).__name__
        checks.append(
            Check(
                "runtime.keychain_accessibility",
                "pass" if accessibility_ok else "fail",
                (
                    "Bearer item имеет AfterFirstUnlock accessibility."
                    if accessibility_ok
                    else "AfterFirstUnlock accessibility не подтверждена."
                ),
                "Установи подписанный helper, разблокируй macOS один раз и "
                "выполни ensure-after-first-unlock.",
                accessibility_details,
            )
        )

    health_path = resolve_project_path(root, str(runtime["health_file"]))
    try:
        health = read_json(health_path)
        current = datetime.now(timezone.utc)
        last_success = parse_timestamp(health.get("last_success_at"))
        age_seconds = (
            (current - last_success).total_seconds()
            if last_success is not None
            else float("inf")
        )
        healthy_statuses = {
            str(value) for value in runtime.get("healthy_statuses", ["healthy"])
        }
        max_age = int(runtime.get("max_poll_age_seconds", 180))
        health_ok = (
            str(health.get("status", "")) in healthy_statuses
            and int(health.get("consecutive_failures", 0)) == 0
            and 0 <= age_seconds <= max_age
        )
        health_status = str(health.get("status", ""))
        consecutive_failures = int(health.get("consecutive_failures", 0))
        last_error_class = health.get("last_error_class")
        last_error_message = health.get("last_error_message")
        poll_context = latest_poll_context(live_database)
        tail_success_at = parse_timestamp(
            poll_context.get("conversation_tail_last_success_at")
        )
        tail_age_seconds = (
            (current - tail_success_at).total_seconds()
            if tail_success_at is not None
            else float("inf")
        )
        transient_poll_error = (
            health_status == "degraded"
            and consecutive_failures == 1
            and 0 <= age_seconds <= max_age
            and poll_context.get("latest_source")
            in {"x_api", "x_api_conversation_tail"}
            and poll_context.get("latest_status") == "failure"
            and str(last_error_class)
            in {"timeout", "TimeoutError", "URLError"}
            and (
                poll_context.get("latest_source") == "x_api"
                or 0 <= tail_age_seconds <= max_age
            )
        )
    except (OSError, ValueError, TypeError, AttributeError):
        health_ok = False
        transient_poll_error = False
        age_seconds = float("inf")
        tail_age_seconds = float("inf")
        max_age = int(runtime.get("max_poll_age_seconds", 180))
        health_status = "unreadable"
        consecutive_failures = 0
        last_error_class = None
        last_error_message = None
        poll_context = {}
    if health_ok:
        poll_status = "pass"
        poll_summary = f"Watcher poll свежий, age={round(age_seconds, 1)}s."
    elif transient_poll_error:
        poll_status = "warn"
        if poll_context.get("latest_source") == "x_api":
            poll_summary = (
                "Основной poll переживает одиночный сетевой timeout, "
                f"последний успех age={round(age_seconds, 1)}s."
            )
        else:
            poll_summary = (
                "Основной poll свежий, conversation tail переживает одиночный "
                f"сетевой timeout, tail age={round(tail_age_seconds, 1)}s."
            )
    else:
        poll_status = "fail"
        poll_summary = "Watcher poll не обновляется или находится в ошибке."
    checks.append(
        Check(
            "runtime.poll_health",
            poll_status,
            poll_summary,
            "Проверь loaded LaunchAgent poll, Keychain token и последний "
            "health error. Не запускай Browser вместо сломанного API poll.",
            {
                "age_seconds": (
                    round(age_seconds, 1)
                    if age_seconds != float("inf")
                    else None
                ),
                "max_age_seconds": max_age,
                "health_status": health_status,
                "consecutive_failures": consecutive_failures,
                "last_error_class": last_error_class,
                "last_error_message": last_error_message,
                "latest_source": poll_context.get("latest_source"),
                "latest_status": poll_context.get("latest_status"),
                "conversation_tail_age_seconds": (
                    round(tail_age_seconds, 1)
                    if tail_age_seconds != float("inf")
                    else None
                ),
            },
        )
    )

    configured_skills = contract.get("skills")
    if configured_skills is None:
        legacy_skill = contract.get("skill", {})
        configured_skills = [
            {
                "id": "x_skill",
                "display_name": "X skill",
                **legacy_skill,
            }
        ]
    if not isinstance(configured_skills, list) or not configured_skills:
        raise ValueError("Contract skills must be a non-empty array")
    for skill in configured_skills:
        if not isinstance(skill, dict):
            raise ValueError("Each contract skill must be an object")
        skill_id = str(skill.get("id", "")).strip()
        if not skill_id:
            raise ValueError("Each contract skill requires an id")
        display_name = str(
            skill.get("display_name", skill_id)
        ).strip()
        source_value = str(skill["source"])
        installed_value = str(skill["installed"])
        source_skill = resolve_project_path(root, source_value)
        installed_skill = resolve_home_path(home, installed_value)
        if not source_skill.is_dir() or not installed_skill.is_dir():
            skill_ok = False
            details = {
                "source": source_value,
                "installed": installed_value,
                "source_exists": source_skill.is_dir(),
                "installed_exists": installed_skill.is_dir(),
            }
        else:
            source_digest = tree_digest(source_skill)
            installed_digest = tree_digest(installed_skill)
            skill_ok = source_digest == installed_digest
            details = {
                "source": source_value,
                "installed": installed_value,
                "source_sha256": source_digest,
                "installed_sha256": installed_digest,
            }
        checks.append(
            Check(
                f"deployment.{skill_id}",
                "pass" if skill_ok else "fail",
                (
                    f"Установленный {display_name} совпадает с Git-копией."
                    if skill_ok
                    else (
                        f"Установленный {display_name} расходится "
                        "с Git-копией."
                    )
                ),
                f"После проверки скопируй {source_value} в "
                f"~/{installed_value.lstrip('/')}.",
                details,
            )
        )

    personality = contract.get("personality", {})
    policy_path = resolve_project_path(
        root, str(personality["tracked_policy"])
    )
    runtime_path = resolve_project_path(
        root, str(personality["runtime_overrides"])
    )
    try:
        personality_policy.load_policy(policy_path)
        personality_policy.load_runtime(runtime_path)
        personality_ok = True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        personality_ok = False
    checks.append(
        Check(
            "personality.policy",
            "pass" if personality_ok else "fail",
            (
                "Матрица характера и runtime overrides валидны."
                if personality_ok
                else "Матрица характера повреждена."
            ),
            "Верни personality/policy.json из Git. Runtime overrides "
            "восстанавливай только из подтверждённого журнала.",
        )
    )
    return checks


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
