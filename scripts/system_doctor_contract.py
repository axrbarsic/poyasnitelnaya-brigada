#!/usr/bin/env python3
"""Composable contract checks for the read-only X autopilot doctor."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts import (
        system_doctor_deployment,
        system_doctor_foundation,
        system_doctor_rotation,
        system_doctor_runtime,
    )
    from scripts.system_doctor_types import Dependencies, QueueContext
except ModuleNotFoundError:
    import system_doctor_deployment  # type: ignore[no-redef]
    import system_doctor_foundation  # type: ignore[no-redef]
    import system_doctor_rotation  # type: ignore[no-redef]
    import system_doctor_runtime  # type: ignore[no-redef]
    from system_doctor_types import Dependencies, QueueContext  # type: ignore[no-redef]


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
        expected_schema_version = runtime.get("database_schema_version")
        expected_application_id = runtime.get("database_application_id")
        healthy, details = deps.database_integrity(
            database,
            expected_schema_version=(
                int(expected_schema_version)
                if expected_schema_version is not None
                else None
            ),
            expected_application_id=(
                int(expected_application_id)
                if expected_application_id is not None
                else None
            ),
        )
        unavailable = bool(
            details and (details.get("transient") or details.get("retryable"))
        )
        schema_mismatch = bool(
            details and details.get("schema_current") is False
        )
        application_mismatch = bool(
            details and details.get("application_matches") is False
        )
        status = "pass" if healthy else "warn" if unavailable else "fail"
        if healthy:
            summary = "Live SQLite проходит quick_check и foreign_key_check."
            repair = "Проверь snapshot и журнал последнего poll."
        elif application_mismatch:
            summary = "Live SQLite принадлежит другому приложению."
            repair = (
                "Останови runtime и проверь путь database. Не мигрируй и не "
                "изменяй этот файл."
            )
        elif schema_mismatch:
            summary = "Версия схемы live SQLite не совпадает с runtime."
            repair = (
                "Сделай snapshot и запусти штатный watcher один раз для "
                "миграции. Более новую схему не откатывай."
            )
        elif unavailable:
            summary = (
                "Live SQLite временно занята или недоступна, повреждение "
                "не подтверждено."
            )
            repair = (
                "Повтори штатную проверку после завершения активной "
                "транзакции или восстановления доступа к файлу."
            )
        else:
            summary = "Live SQLite не прошла проверку целостности."
            repair = (
                "Останови poll, сделай snapshot повреждённого файла и "
                "восстанови последний валидный snapshot."
            )
        checks.append(
            deps.make_check(
                "runtime.database",
                status,
                summary,
                repair,
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


def _runtime_session_janitor_checks(
    root: Path,
    runtime: dict[str, Any],
    deps: Dependencies,
) -> list[Any]:
    relative = runtime.get("session_janitor_state_file")
    max_age = runtime.get("max_session_janitor_age_seconds")
    if relative is None and max_age is None:
        return []
    if relative is None or max_age is None:
        return [
            deps.make_check(
                "runtime.session_janitor",
                "fail",
                "Контракт session janitor неполон.",
                "Задай state file и максимальный возраст одной парой.",
            )
        ]
    state_path = deps.resolve_project_path(root, str(relative))
    try:
        state = deps.read_json(state_path)
    except (OSError, ValueError):
        state = None
    return [
        system_doctor_runtime.session_janitor_health_check(
            state,
            max_age_seconds=int(max_age),
        )
    ]


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
    except (OSError, ValueError, TypeError):
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
    checks = system_doctor_foundation.project_checks(
        root,
        contract,
        dependencies,
    )
    config_checks, config = system_doctor_foundation.config_checks(
        root,
        contract,
        config_path,
        dependencies,
    )
    checks.extend(config_checks)
    if config is None:
        return checks
    checks.extend(
        system_doctor_deployment.thread_checks(
            root,
            home,
            contract,
            config,
            dependencies,
        )
    )
    checks.extend(
        system_doctor_deployment.automation_checks(
            root,
            home,
            contract,
            config,
            dependencies,
        )
    )
    checks.extend(
        system_doctor_deployment.launchagent_checks(
            home,
            contract,
            config,
            dependencies,
        )
    )
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
        _runtime_session_janitor_checks(root, runtime, dependencies)
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
