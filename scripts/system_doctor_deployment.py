#!/usr/bin/env python3
"""Codex task, automation, and LaunchAgent deployment checks."""

from __future__ import annotations

import hashlib
import plistlib
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from scripts import launchagent_runtime
    from scripts.system_doctor_types import Dependencies
except ModuleNotFoundError:
    import launchagent_runtime  # type: ignore[no-redef]
    from system_doctor_types import Dependencies  # type: ignore[no-redef]


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


def thread_checks(
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
        if expected.get("unique_unarchived_in_cwd"):
            title = str(expected.get("title", "")).strip()
            cwd = Path(str(expected.get("cwd", ""))).expanduser().resolve()
            try:
                matching = deps.matching_threads(
                    database,
                    cwd=cwd,
                    title=title,
                    archived=False,
                )
            except (sqlite3.Error, ValueError) as error:
                checks.append(
                    deps.make_check(
                        f"thread.{role}.cardinality",
                        "fail",
                        "Не удалось проверить единственность сессии.",
                        "Проверь read-only Codex state и повтори doctor.",
                        {"error": type(error).__name__},
                    )
                )
            else:
                identifiers = [str(item["id"]) for item in matching]
                unique = identifiers == [thread_id]
                checks.append(
                    deps.make_check(
                        f"thread.{role}.cardinality",
                        "pass" if unique else "fail",
                        (
                            f"Сессия {role} единственная."
                            if unique
                            else f"Для роли {role} найдено лишнее число сессий."
                        ),
                        "Оставь точную текущую сессию и архивируй остальные "
                        "через официальный Codex archive.",
                        None if unique else {
                            "expected": [thread_id],
                            "actual": identifiers,
                        },
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


def automation_checks(
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
        try:
            actual = deps.parse_simple_toml(path)
        except (OSError, ValueError) as error:
            checks.append(
                deps.make_check(
                    f"automation.{name}",
                    "fail",
                    f"Automation {automation_id} нельзя безопасно прочитать.",
                    "Восстанови automation через официальный automation_update.",
                    {"error": f"{type(error).__name__}: {error}"[-500:]},
                )
            )
            continue
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


def _launchagent_runtime_command(arguments: Any) -> list[str] | None:
    if not isinstance(arguments, list) or len(arguments) < 2:
        return None
    interpreter = str(arguments[0])
    entrypoint = str(arguments[1])
    if not Path(interpreter).name.startswith("python"):
        return None
    if not entrypoint.endswith(".py"):
        return None
    return [interpreter, entrypoint, "--help"]


def _launchagent_runtime_failures(
    commands: list[tuple[str, list[str]]],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for label, command in commands:
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as error:
            failures.append(
                {
                    "label": label,
                    "command": command,
                    "error": f"{type(error).__name__}: {error}"[-500:],
                }
            )
            continue
        if completed.returncode == 0:
            continue
        detail = completed.stderr.strip() or completed.stdout.strip()
        failures.append(
            {
                "label": label,
                "command": command,
                "returncode": completed.returncode,
                "output": detail[-1000:],
            }
        )
    return failures


def _launchagent_runtime_check(
    commands: list[tuple[str, list[str]]],
    deps: Dependencies,
    *,
    expected_interpreter: str | None = None,
    minimum_python: str = launchagent_runtime.DEFAULT_MINIMUM_PYTHON,
    minimum_sqlite: str = launchagent_runtime.DEFAULT_MINIMUM_SQLITE,
) -> Any | None:
    if not commands:
        return None
    failures = _launchagent_runtime_failures(commands)
    runtime_details: list[dict[str, Any]] = []
    for interpreter in sorted({command[0] for _, command in commands}):
        if expected_interpreter and interpreter != expected_interpreter:
            failures.append(
                {
                    "interpreter": interpreter,
                    "error": "interpreter differs from config",
                    "expected": expected_interpreter,
                }
            )
        try:
            runtime = launchagent_runtime.require_safe(
                Path(interpreter),
                minimum_python=minimum_python,
                minimum_sqlite=minimum_sqlite,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            failures.append(
                {
                    "interpreter": interpreter,
                    "error": f"{type(error).__name__}: {error}"[-1000:],
                }
            )
        else:
            runtime_details.append(runtime.as_dict())
    return deps.make_check(
        "launchagent.runtime_compatibility",
        "pass" if not failures else "fail",
        (
            "LaunchAgent entrypoints импортируются штатным Python."
            if not failures
            else "LaunchAgent runtime несовместим или небезопасен."
        ),
        "Перерендери plists с единым безопасным Python, затем повтори "
        "адресный --help и SQLite version probe.",
        (
            {"failures": failures}
            if failures
            else {
                "probed_labels": [label for label, _ in commands],
                "runtimes": runtime_details,
            }
        ),
    )


def launchagent_checks(
    home: Path,
    contract: dict[str, Any],
    config: dict[str, Any],
    deps: Dependencies,
) -> list[Any]:
    checks: list[Any] = []
    runtime_commands: list[tuple[str, list[str]]] = []
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
        runtime_command = _launchagent_runtime_command(arguments)
        if installed_ok and runtime_command is not None:
            runtime_commands.append((label, runtime_command))
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
    runtime_contract = contract.get("runtime", {})
    expected_interpreter = str(
        config.get("launchagent_python_executable", "")
    ).strip()
    runtime_check = _launchagent_runtime_check(
        runtime_commands,
        deps,
        expected_interpreter=expected_interpreter or None,
        minimum_python=str(
            runtime_contract.get(
                "minimum_launchagent_python_version",
                launchagent_runtime.DEFAULT_MINIMUM_PYTHON,
            )
        ),
        minimum_sqlite=str(
            runtime_contract.get(
                "minimum_launchagent_sqlite_version",
                launchagent_runtime.DEFAULT_MINIMUM_SQLITE,
            )
        ),
    )
    if runtime_check is not None:
        checks.append(runtime_check)
    return checks
