#!/usr/bin/env python3
"""Composable contract checks for the read-only X autopilot doctor."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from scripts import system_doctor_rotation
except ModuleNotFoundError:
    import system_doctor_rotation  # type: ignore[no-redef]


@dataclass(frozen=True)
class Dependencies:
    make_check: Callable[..., Any]
    read_json: Callable[[Path], Any]
    resolve_project_path: Callable[[Path, str], Path]
    resolve_home_path: Callable[[Path, str], Path]
    state_database: Callable[[Path], Path | None]
    thread_row: Callable[..., dict[str, Any] | None]
    reasoning_effort_meets_minimum: Callable[[Any, Any], bool]
    parse_simple_toml: Callable[[Path], dict[str, Any]]
    tree_digest: Callable[[Path], str]
    git_origin: Callable[[Path], str]
    launchagent_loaded: Callable[[str], tuple[bool, str]]
    database_integrity: Callable[[Path], tuple[bool, dict[str, Any] | None]]
    parse_timestamp: Callable[[Any], datetime | None]
    relay_progress_check: Callable[..., Any]
    oldest_queue_event_id: Callable[[list[Any]], str | None]
    queue_latency_check: Callable[..., Any]
    latest_poll_context: Callable[[Path], dict[str, Any]]
    keychain_bundle: Any
    personality_policy: Any


@dataclass(frozen=True)
class QueueContext:
    database: Path
    events: list[dict[str, Any]]
    pending: int


def _thread_id_from_contract(
    expected: dict[str, Any],
    config: dict[str, Any],
) -> str:
    direct = str(expected.get("id", "")).strip()
    if direct:
        return direct
    source = str(expected.get("id_source", "")).strip()
    prefix = "config."
    if not source.startswith(prefix):
        raise ValueError("thread contract requires id or config id_source")
    value = str(config.get(source[len(prefix) :], "")).strip()
    if not value:
        raise ValueError(f"thread id source is empty: {source}")
    return value


def _project_checks(
    root: Path,
    contract: dict[str, Any],
    deps: Dependencies,
) -> list[Any]:
    expected_root = Path(str(contract["canonical_root"])).expanduser()
    root_matches = root.resolve() == expected_root.resolve()
    checks = [
        deps.make_check(
            "project.root",
            "pass" if root_matches else "fail",
            (
                "Канонический каталог совпадает."
                if root_matches
                else f"Открыт другой каталог: {root.resolve()}"
            ),
            f"Открой проект из {expected_root}.",
        )
    ]
    missing = [
        value
        for value in contract.get("required_files", [])
        if not (root / str(value)).is_file()
    ]
    checks.append(
        deps.make_check(
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
        actual_origin = deps.git_origin(root)
    except (OSError, subprocess.SubprocessError):
        actual_origin = ""
    expected_origin = str(contract.get("git_origin", ""))
    checks.append(
        deps.make_check(
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
    return checks


def _config_checks(
    root: Path,
    contract: dict[str, Any],
    config_path: Path,
    deps: Dependencies,
) -> tuple[list[Any], dict[str, Any] | None]:
    if not config_path.is_file():
        return [
            deps.make_check(
                "config.local",
                "fail",
                "Локальный config.json отсутствует.",
                "Восстанови config.json из защищённой локальной копии, не из Git.",
            )
        ], None
    config = deps.read_json(config_path)
    config_owner = str(config.get("browser_owner_thread_id", "")).strip()
    project_id = str(config.get("codex_project_id", "")).strip()
    checks = [
        deps.make_check(
            "config.owner_thread",
            "pass" if config_owner else "fail",
            (
                "config.json содержит текущий Browser owner."
                if config_owner
                else "config.json не содержит Browser owner."
            ),
            "Восстанови browser_owner_thread_id из живой automation x-relay.",
            {"actual": config_owner},
        )
    ]
    checks.append(
        deps.make_check(
            "config.codex_project",
            "pass" if project_id else "fail",
            (
                "Codex project для ротации owner настроен."
                if project_id
                else "Codex project для ротации owner не настроен."
            ),
            "Запиши projectId канонического x-mention-watcher в config.json.",
        )
    )
    mode = str(config.get("desktop_relay_mode", ""))
    checks.append(
        deps.make_check(
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
    runtime = contract.get("runtime", {})
    expected_label = runtime.get("event_dispatch_launchagent_label")
    if expected_label is not None:
        expected_label = str(expected_label)
        actual_label = str(config.get("event_dispatch_launchagent_label", ""))
        enabled = bool(config.get("event_dispatch_on_new_events", False))
        matches = enabled and actual_label == expected_label
        checks.append(
            deps.make_check(
                "config.event_dispatch",
                "pass" if matches else "fail",
                (
                    "Новые X события немедленно запускают dispatcher."
                    if matches
                    else "Событийный запуск dispatcher не настроен."
                ),
                "Включи event_dispatch_on_new_events и верни точный "
                "event_dispatch_launchagent_label из контракта.",
                {
                    "enabled": enabled,
                    "actual_label": actual_label,
                    "expected_label": expected_label,
                },
            )
        )
    return checks, config


def _thread_checks(
    root: Path,
    home: Path,
    contract: dict[str, Any],
    config: dict[str, Any],
    deps: Dependencies,
) -> list[Any]:
    database = deps.state_database(home)
    if database is None:
        return [
            deps.make_check(
                "codex.state_database",
                "fail",
                "Codex state database не найдена.",
                "Запусти Codex Desktop и повтори doctor.",
            )
        ]
    checks: list[Any] = []
    for role, expected in contract.get("threads", {}).items():
        try:
            thread_id = _thread_id_from_contract(expected, config)
        except ValueError as error:
            checks.append(
                deps.make_check(
                    f"thread.{role}",
                    "fail",
                    f"Не удалось определить сессию {role}.",
                    "Восстанови runtime-указатель роли в config.json.",
                    {"error": str(error)},
                )
            )
            continue
        try:
            row = deps.thread_row(database, thread_id)
        except sqlite3.Error as error:
            checks.append(
                deps.make_check(
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
                deps.make_check(
                    f"thread.{role}",
                    "fail",
                    f"Сессия {role} не найдена.",
                    "Найди сессию по ID из recovery/system-contract.json или "
                    "восстанови роль по методичке.",
                )
            )
            continue
        mismatches: dict[str, Any] = {}
        for field in ("title", "model", "cwd"):
            expected_value = expected.get(field)
            if expected_value is not None and row.get(field) != expected_value:
                mismatches[field] = {
                    "actual": row.get(field),
                    "expected": expected_value,
                }
        minimum_effort = expected.get("minimum_reasoning_effort")
        if minimum_effort is not None:
            if not deps.reasoning_effort_meets_minimum(
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
            deps.make_check(
                f"thread.{role}",
                "pass" if not mismatches else "fail",
                (
                    f"Сессия {role} соответствует роли."
                    if not mismatches
                    else f"Сессия {role} расходится с контрактом."
                ),
                "Восстанови модель, effort, cwd и снимите архивный флаг через "
                "Codex Desktop.",
                mismatches or None,
            )
        )
        if expected.get("pin_recommended"):
            checks.append(
                deps.make_check(
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
    return checks


def _automation_checks(
    root: Path,
    home: Path,
    contract: dict[str, Any],
    config: dict[str, Any],
    deps: Dependencies,
) -> list[Any]:
    checks: list[Any] = []
    for name, expected in contract.get("automations", {}).items():
        automation_id = str(expected["id"])
        path = home / ".codex" / "automations" / automation_id / "automation.toml"
        if not path.is_file():
            checks.append(
                deps.make_check(
                    f"automation.{name}",
                    "fail",
                    f"Automation {automation_id} отсутствует.",
                    "Восстанови automation через официальный automation_update.",
                )
            )
            continue
        actual = deps.parse_simple_toml(path)
        mismatches = {
            key: {"actual": actual.get(key), "expected": value}
            for key, value in expected.items()
            if key not in {"prompt_source", "target_thread_role"}
            and actual.get(key) != value
        }
        target_role = str(expected.get("target_thread_role", "")).strip()
        if target_role:
            role_contract = contract.get("threads", {}).get(target_role)
            try:
                expected_target = _thread_id_from_contract(
                    role_contract if isinstance(role_contract, dict) else {},
                    config,
                )
            except ValueError:
                expected_target = ""
            if actual.get("target_thread_id") != expected_target:
                mismatches["target_thread_id"] = {
                    "actual": actual.get("target_thread_id"),
                    "expected": expected_target,
                    "role": target_role,
                }
        prompt_source = str(expected.get("prompt_source", "")).strip()
        if prompt_source:
            try:
                expected_prompt = deps.resolve_project_path(
                    root,
                    prompt_source,
                ).read_text(encoding="utf-8").rstrip("\n")
            except OSError:
                expected_prompt = None
            if actual.get("prompt") != expected_prompt:
                mismatches["prompt"] = {
                    "actual_sha256": hashlib.sha256(
                        str(actual.get("prompt", "")).encode("utf-8")
                    ).hexdigest(),
                    "expected_sha256": (
                        hashlib.sha256(expected_prompt.encode("utf-8")).hexdigest()
                        if expected_prompt is not None
                        else None
                    ),
                }
        checks.append(
            deps.make_check(
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
    return checks


def _launchagent_checks(
    home: Path,
    contract: dict[str, Any],
    deps: Dependencies,
) -> list[Any]:
    checks: list[Any] = []
    expected_programs = contract.get("launch_agent_programs", {})
    for label in contract.get("launch_agents", []):
        installed = home / "Library" / "LaunchAgents" / f"{label}.plist"
        if not installed.is_file():
            checks.append(
                deps.make_check(
                    f"launchagent.{label}",
                    "fail",
                    f"LaunchAgent {label} не установлен.",
                    "Перерендери macOS plists в staging, проверь их и установи "
                    "штатным launchctl bootstrap.",
                )
            )
            continue
        try:
            payload = plistlib.loads(installed.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            payload = {}
        expected_program = str(expected_programs.get(label, "")).strip()
        arguments = payload.get("ProgramArguments", [])
        program_ok = not expected_program or (
            isinstance(arguments, list)
            and any(
                str(argument).endswith(expected_program)
                for argument in arguments
            )
        )
        installed_ok = payload.get("Label") == label and program_ok
        checks.append(
            deps.make_check(
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
                loaded, detail = deps.launchagent_loaded(label)
            except (OSError, subprocess.SubprocessError):
                loaded, detail = False, ""
            checks.append(
                deps.make_check(
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
    return checks


def _runtime_database_queue_checks(
    root: Path,
    runtime: dict[str, Any],
    deps: Dependencies,
) -> tuple[list[Any], QueueContext]:
    checks: list[Any] = []
    database = deps.resolve_project_path(root, str(runtime["database"]))
    if not database.is_file():
        checks.append(
            deps.make_check(
                "runtime.database",
                "fail",
                "Live SQLite отсутствует.",
                "Восстанови последний валидный snapshot, не создавай пустую "
                "базу поверх потери.",
            )
        )
    else:
        healthy, details = deps.database_integrity(database)
        unavailable = bool(
            details and (details.get("transient") or details.get("retryable"))
        )
        status = "pass" if healthy else "warn" if unavailable else "fail"
        checks.append(
            deps.make_check(
                "runtime.database",
                status,
                (
                    "Live SQLite проходит quick_check и foreign_key_check."
                    if healthy
                    else "Live SQLite временно занята или недоступна, "
                    "повреждение не подтверждено."
                    if unavailable
                    else "Live SQLite не прошла проверку целостности."
                ),
                (
                    "Повтори штатную проверку после завершения активной "
                    "транзакции или восстановления доступа к файлу."
                    if unavailable
                    else "Останови poll, сделай snapshot повреждённого файла и "
                    "восстанови последний валидный snapshot."
                ),
                details,
            )
        )
    wake_path = deps.resolve_project_path(root, str(runtime["wake_file"]))
    events: list[dict[str, Any]] = []
    try:
        wake = deps.read_json(wake_path)
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
        deps.make_check(
            "runtime.queue",
            "pass" if queue_healthy else "fail",
            (
                f"Очередь согласована, pending={pending}."
                if queue_healthy
                else "Wake queue повреждена или несогласована."
            ),
            "Не удаляй очередь. Повтори model-free poll и сравни SQLite с "
            "wake-request.json.",
        )
    )
    return checks, QueueContext(database, events, pending)


def _runtime_dispatch_checks(
    root: Path,
    config: dict[str, Any],
    runtime: dict[str, Any],
    queue: QueueContext,
    deps: Dependencies,
) -> list[Any]:
    checks: list[Any] = []
    owner: Any = None
    owner_active = False
    owner_state_value = str(runtime.get("autopilot_state_file", "")).strip()
    if owner_state_value:
        try:
            owner = deps.read_json(
                deps.resolve_project_path(root, owner_state_value)
            ).get("owner")
            if owner is None:
                owner_ok = True
                owner_details = {"owner": None}
            elif isinstance(owner, dict):
                expires = deps.parse_timestamp(owner.get("lease_expires_at"))
                owner_active = (
                    expires is not None and expires >= datetime.now(timezone.utc)
                )
                owner_ok = owner_active
                owner_details = {
                    "event_ids": owner.get("event_ids", []),
                    "lease_expires_at": owner.get("lease_expires_at"),
                }
            else:
                owner_ok = False
                owner_details = {"owner_type": type(owner).__name__}
        except (OSError, ValueError, TypeError, AttributeError):
            owner_ok = False
            owner_details = {"state": "unreadable"}
        checks.append(
            deps.make_check(
                "runtime.owner_lease",
                "pass" if owner_ok else "fail",
                (
                    "Browser owner lease согласован."
                    if owner_ok
                    else "Browser owner lease протух или повреждён."
                ),
                "Запусти штатный session janitor recovery, не удаляй очередь.",
                owner_details,
            )
        )
    dispatch_state: dict[str, Any] = {}
    dispatch_value = str(runtime.get("dispatch_state_file", "")).strip()
    if dispatch_value:
        path = deps.resolve_project_path(root, dispatch_value)
        max_age = int(runtime.get("max_dispatch_age_seconds", 180))
        try:
            dispatch_state = deps.read_json(path)
            checked = deps.parse_timestamp(dispatch_state.get("checked_at"))
            age = (
                (datetime.now(timezone.utc) - checked).total_seconds()
                if checked is not None
                else float("inf")
            )
            ok = 0 <= age <= max_age
        except (OSError, ValueError, TypeError, AttributeError):
            ok = False
            age = float("inf")
        dispatch_status = "pass" if ok else ("warn" if owner_active else "fail")
        checks.append(
            deps.make_check(
                "runtime.dispatch_health",
                dispatch_status,
                (
                    f"Dispatcher свежий, age={round(age, 1)}s."
                    if ok
                    else (
                        "Dispatcher устарел, но событие уже принадлежит "
                        "активному Browser owner."
                        if owner_active
                        else "Dispatcher не обновляет durable state."
                    )
                ),
                "Проверь loaded dispatch LaunchAgent и выполни его kickstart.",
                {
                    "age_seconds": round(age, 1) if age != float("inf") else None,
                    "max_age_seconds": max_age,
                },
            )
        )
    event_dispatch_state: dict[str, Any] = {}
    event_dispatch_value = str(
        runtime.get("event_dispatch_state_file", "")
    ).strip()
    if event_dispatch_value:
        try:
            event_dispatch_state = deps.read_json(
                deps.resolve_project_path(root, event_dispatch_value)
            )
        except (OSError, ValueError, TypeError, AttributeError):
            event_dispatch_state = {}
    max_relay_wait = int(runtime.get("max_relay_wait_seconds", 0))
    if max_relay_wait > 0:
        checks.append(
            deps.relay_progress_check(
                pending_count=queue.pending,
                dispatch_state=dispatch_state,
                owner=owner,
                event_dispatch_state=event_dispatch_state,
                required_event_id=deps.oldest_queue_event_id(queue.events),
                max_wait_seconds=max_relay_wait,
            )
        )
    max_queue_age = int(runtime.get("max_queue_age_seconds", 0))
    if max_queue_age > 0:
        supervisor_state: dict[str, Any] = {}
        value = str(
            config.get("autopilot_supervisor_state_file")
            or "var/autopilot-supervisor.json"
        ).strip()
        if value:
            try:
                supervisor_state = deps.read_json(
                    deps.resolve_project_path(root, value)
                )
            except (OSError, ValueError, TypeError, AttributeError):
                supervisor_state = {}
        checks.append(
            deps.queue_latency_check(
                events=queue.events,
                owner=owner,
                supervisor_state=supervisor_state,
                dispatch_state=dispatch_state,
                event_dispatch_state=event_dispatch_state,
                delivery_grace_seconds=max_relay_wait,
                max_age_seconds=max_queue_age,
            )
        )
    return checks


def _runtime_keychain_checks(
    root: Path,
    config: dict[str, Any],
    runtime: dict[str, Any],
    deps: Dependencies,
) -> list[Any]:
    helper = deps.resolve_project_path(root, str(runtime["keychain_helper"]))
    helper_ok = helper.is_file() and os.access(helper, os.X_OK)
    checks = [
        deps.make_check(
            "runtime.keychain_helper",
            "pass" if helper_ok else "fail",
            (
                "Keychain helper установлен и исполняем."
                if helper_ok
                else "Keychain helper отсутствует или не исполняем."
            ),
            "Выполни scripts/install_keychain_helper.sh.",
        )
    ]
    service = str(config.get("keychain_service", "")).strip()
    account = str(config.get("keychain_account", "")).strip()
    if not (sys.platform == "darwin" and helper_ok and service and account):
        return checks
    try:
        verification = deps.keychain_bundle.verify_bundle(helper)
        bundle_ok = verification.ok
        bundle_details = {
            "app_path": verification.app_path,
            "bundle_id": verification.bundle_id,
            "application_id": verification.application_id,
            "profile_name": verification.profile_name,
            "profile_expires_at": verification.profile_expires_at,
            "errors": list(verification.errors),
        }
    except Exception as error:
        bundle_ok = False
        bundle_details = {"errors": ["verifier raised " + type(error).__name__]}
        verification = None
    checks.append(
        deps.make_check(
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
    accessible = False
    accessibility_details: dict[str, Any] = {"bundle_verified": bundle_ok}
    if bundle_ok and verification is not None:
        try:
            result = subprocess.run(
                [
                    str(Path(verification.executable_path)),
                    "is-after-first-unlock",
                    service,
                    account,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            accessible = result.returncode == 0
            accessibility_details["returncode"] = result.returncode
        except (OSError, subprocess.SubprocessError) as error:
            accessibility_details["error_class"] = type(error).__name__
    checks.append(
        deps.make_check(
            "runtime.keychain_accessibility",
            "pass" if accessible else "fail",
            (
                "Bearer item имеет AfterFirstUnlock accessibility."
                if accessible
                else "AfterFirstUnlock accessibility не подтверждена."
            ),
            "Установи подписанный helper, разблокируй macOS один раз и "
            "выполни ensure-after-first-unlock.",
            accessibility_details,
        )
    )
    return checks


def _runtime_poll_checks(
    root: Path,
    runtime: dict[str, Any],
    database: Path,
    deps: Dependencies,
) -> list[Any]:
    health_path = deps.resolve_project_path(root, str(runtime["health_file"]))
    try:
        health = deps.read_json(health_path)
        current = datetime.now(timezone.utc)
        last_success = deps.parse_timestamp(health.get("last_success_at"))
        age = (
            (current - last_success).total_seconds()
            if last_success is not None
            else float("inf")
        )
        healthy_statuses = {
            str(value) for value in runtime.get("healthy_statuses", ["healthy"])
        }
        max_age = int(runtime.get("max_poll_age_seconds", 180))
        healthy = (
            str(health.get("status", "")) in healthy_statuses
            and int(health.get("consecutive_failures", 0)) == 0
            and 0 <= age <= max_age
        )
        health_status = str(health.get("status", ""))
        failures = int(health.get("consecutive_failures", 0))
        error_class = health.get("last_error_class")
        error_message = health.get("last_error_message")
        poll_context = deps.latest_poll_context(database)
        tail_success = deps.parse_timestamp(
            poll_context.get("conversation_tail_last_success_at")
        )
        tail_age = (
            (current - tail_success).total_seconds()
            if tail_success is not None
            else float("inf")
        )
        transient = (
            health_status == "degraded"
            and failures == 1
            and 0 <= age <= max_age
            and poll_context.get("latest_source")
            in {"x_api", "x_api_conversation_tail"}
            and poll_context.get("latest_status") == "failure"
            and str(error_class) in {"timeout", "TimeoutError", "URLError"}
            and (
                poll_context.get("latest_source") == "x_api"
                or 0 <= tail_age <= max_age
            )
        )
    except (OSError, ValueError, TypeError, AttributeError):
        healthy = False
        transient = False
        age = float("inf")
        tail_age = float("inf")
        max_age = int(runtime.get("max_poll_age_seconds", 180))
        health_status = "unreadable"
        failures = 0
        error_class = None
        error_message = None
        poll_context = {}
    if healthy:
        status = "pass"
        summary = f"Watcher poll свежий, age={round(age, 1)}s."
    elif transient:
        status = "warn"
        if poll_context.get("latest_source") == "x_api":
            summary = (
                "Основной poll переживает одиночный сетевой timeout, "
                f"последний успех age={round(age, 1)}s."
            )
        else:
            summary = (
                "Основной poll свежий, conversation tail переживает одиночный "
                f"сетевой timeout, tail age={round(tail_age, 1)}s."
            )
    else:
        status = "fail"
        summary = "Watcher poll не обновляется или находится в ошибке."
    return [
        deps.make_check(
            "runtime.poll_health",
            status,
            summary,
            "Проверь loaded LaunchAgent poll, Keychain token и последний "
            "health error. Не запускай Browser вместо сломанного API poll.",
            {
                "age_seconds": round(age, 1) if age != float("inf") else None,
                "max_age_seconds": max_age,
                "health_status": health_status,
                "consecutive_failures": failures,
                "last_error_class": error_class,
                "last_error_message": error_message,
                "latest_source": poll_context.get("latest_source"),
                "latest_status": poll_context.get("latest_status"),
                "conversation_tail_age_seconds": (
                    round(tail_age, 1) if tail_age != float("inf") else None
                ),
            },
        )
    ]


def _skill_checks(
    root: Path,
    home: Path,
    contract: dict[str, Any],
    deps: Dependencies,
) -> list[Any]:
    configured = contract.get("skills")
    if configured is None:
        configured = [
            {
                "id": "x_skill",
                "display_name": "X skill",
                **contract.get("skill", {}),
            }
        ]
    if not isinstance(configured, list) or not configured:
        raise ValueError("Contract skills must be a non-empty array")
    checks: list[Any] = []
    for skill in configured:
        if not isinstance(skill, dict):
            raise ValueError("Each contract skill must be an object")
        skill_id = str(skill.get("id", "")).strip()
        if not skill_id:
            raise ValueError("Each contract skill requires an id")
        display_name = str(skill.get("display_name", skill_id)).strip()
        source_value = str(skill["source"])
        installed_value = str(skill["installed"])
        source = deps.resolve_project_path(root, source_value)
        installed = deps.resolve_home_path(home, installed_value)
        if not source.is_dir() or not installed.is_dir():
            ok = False
            details = {
                "source": source_value,
                "installed": installed_value,
                "source_exists": source.is_dir(),
                "installed_exists": installed.is_dir(),
            }
        else:
            source_digest = deps.tree_digest(source)
            installed_digest = deps.tree_digest(installed)
            ok = source_digest == installed_digest
            details = {
                "source": source_value,
                "installed": installed_value,
                "source_sha256": source_digest,
                "installed_sha256": installed_digest,
            }
        checks.append(
            deps.make_check(
                f"deployment.{skill_id}",
                "pass" if ok else "fail",
                (
                    f"Установленный {display_name} совпадает с Git-копией."
                    if ok
                    else f"Установленный {display_name} расходится с Git-копией."
                ),
                f"После проверки скопируй {source_value} в "
                f"~/{installed_value.lstrip('/')}.",
                details,
            )
        )
    return checks


def _personality_checks(
    root: Path,
    contract: dict[str, Any],
    deps: Dependencies,
) -> list[Any]:
    personality = contract.get("personality", {})
    policy_path = deps.resolve_project_path(
        root,
        str(personality["tracked_policy"]),
    )
    runtime_path = deps.resolve_project_path(
        root,
        str(personality["runtime_overrides"]),
    )
    try:
        deps.personality_policy.load_policy(policy_path)
        deps.personality_policy.load_runtime(runtime_path)
        ok = True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        ok = False
    return [
        deps.make_check(
            "personality.policy",
            "pass" if ok else "fail",
            (
                "Матрица характера и runtime overrides валидны."
                if ok
                else "Матрица характера повреждена."
            ),
            "Верни personality/policy.json из Git. Runtime overrides "
            "восстанавливай только из подтверждённого журнала.",
        )
    ]


def check_contract(
    root: Path,
    home: Path,
    contract: dict[str, Any],
    config_path: Path,
    *,
    dependencies: Dependencies,
) -> list[Any]:
    checks = _project_checks(root, contract, dependencies)
    config_checks, config = _config_checks(
        root,
        contract,
        config_path,
        dependencies,
    )
    checks.extend(config_checks)
    if config is None:
        return checks
    checks.extend(_thread_checks(root, home, contract, config, dependencies))
    checks.extend(_automation_checks(root, home, contract, config, dependencies))
    checks.extend(_launchagent_checks(home, contract, dependencies))
    runtime = contract.get("runtime", {})
    runtime_checks, queue = _runtime_database_queue_checks(
        root,
        runtime,
        dependencies,
    )
    checks.extend(runtime_checks)
    checks.extend(
        _runtime_dispatch_checks(
            root,
            config,
            runtime,
            queue,
            dependencies,
        )
    )
    checks.extend(
        system_doctor_rotation.check_rotation(
            root,
            runtime,
            make_check=dependencies.make_check,
            read_json=dependencies.read_json,
            resolve_path=dependencies.resolve_project_path,
            parse_timestamp=dependencies.parse_timestamp,
        )
    )
    checks.extend(
        _runtime_keychain_checks(
            root,
            config,
            runtime,
            dependencies,
        )
    )
    checks.extend(
        _runtime_poll_checks(root, runtime, queue.database, dependencies)
    )
    checks.extend(_skill_checks(root, home, contract, dependencies))
    checks.extend(_personality_checks(root, contract, dependencies))
    return checks
