#!/usr/bin/env python3
"""Canonical project and local configuration checks for system doctor."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

try:
    from scripts import inbound_policy
    from scripts.system_doctor_types import Dependencies
except ModuleNotFoundError:
    import inbound_policy  # type: ignore[no-redef]
    from system_doctor_types import Dependencies  # type: ignore[no-redef]


def project_checks(
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


def inbound_throughput_check(
    config: dict[str, Any],
    runtime: dict[str, Any],
    deps: Dependencies,
) -> Any | None:
    expected_events = runtime.get("inbound_claim_max_events")
    expected_tabs = runtime.get("inbound_max_parallel_read_tabs")
    if expected_events is None and expected_tabs is None:
        return None
    error_message = None
    try:
        actual = inbound_policy.InboundPolicy.from_config(config)
        expected = inbound_policy.InboundPolicy.from_runtime_contract(runtime)
    except ValueError as error:
        actual_events = config.get("autopilot_max_claim_events")
        actual_tabs = config.get("autopilot_max_parallel_read_tabs")
        expected_events_value = expected_events
        expected_tabs_value = expected_tabs
        matches = False
        error_message = str(error)
    else:
        actual_events = actual.claim_events
        actual_tabs = actual.parallel_read_tabs
        expected_events_value = expected.claim_events
        expected_tabs_value = expected.parallel_read_tabs
        matches = actual == expected
    return deps.make_check(
        "config.inbound_throughput",
        "pass" if matches else "fail",
        (
            "Inbound bounded batch и read-only вкладки соответствуют контракту."
            if matches
            else "Inbound throughput откатился от версионного контракта."
        ),
        "Восстанови bounded claim и read-only tab limit из контракта, сохраняя "
        "одну writer-линию.",
        {
            "actual_claim_events": actual_events,
            "expected_claim_events": expected_events_value,
            "actual_read_tabs": actual_tabs,
            "expected_read_tabs": expected_tabs_value,
            "error": error_message,
        },
    )


def config_checks(
    root: Path,
    contract: dict[str, Any],
    config_path: Path,
    deps: Dependencies,
) -> tuple[list[Any], dict[str, Any] | None]:
    del root
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
    if not isinstance(config, dict):
        return [
            deps.make_check(
                "config.local",
                "fail",
                "Локальный config.json не является JSON object.",
                "Восстанови config.json из защищённой локальной копии.",
            )
        ], None
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
    throughput = inbound_throughput_check(config, runtime, deps)
    if throughput is not None:
        checks.append(throughput)
    return checks, config
