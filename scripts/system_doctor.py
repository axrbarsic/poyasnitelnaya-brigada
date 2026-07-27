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
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from scripts import personality_policy
except ModuleNotFoundError:
    import personality_policy  # type: ignore[no-redef]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = PROJECT_ROOT / "recovery" / "system-contract.json"
DEFAULT_CONFIG = PROJECT_ROOT / "config.json"
VALID_STATUSES = {"pass", "warn", "fail"}


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
            for field in ("model", "reasoning_effort", "cwd"):
                expected_value = expected.get(field)
                if expected_value is not None and row.get(field) != expected_value:
                    mismatches[field] = {
                        "actual": row.get(field),
                        "expected": expected_value,
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
        mismatches = {
            key: {"actual": actual.get(key), "expected": value}
            for key, value in expected.items()
            if actual.get(key) != value
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
        checks.append(
            Check(
                f"launchagent.{label}",
                "pass" if payload.get("Label") == label else "fail",
                (
                    f"LaunchAgent {label} установлен."
                    if payload.get("Label") == label
                    else f"LaunchAgent {label} повреждён."
                ),
                "Перерендери и переустанови только этот LaunchAgent.",
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
        uri = f"{live_database.resolve().as_uri()}?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                quick = connection.execute("PRAGMA quick_check").fetchone()
                foreign = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
            healthy = quick == ("ok",) and not foreign
        except sqlite3.Error:
            healthy = False
        checks.append(
            Check(
                "runtime.database",
                "pass" if healthy else "fail",
                (
                    "Live SQLite проходит quick_check и foreign_key_check."
                    if healthy
                    else "Live SQLite не прошла проверку целостности."
                ),
                "Останови poll, сделай snapshot повреждённого файла и "
                "восстанови последний валидный snapshot.",
            )
        )

    wake_path = resolve_project_path(root, str(runtime["wake_file"]))
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
            "Пересобери scripts/keychain_helper.swift в var/keychain-helper.",
        )
    )

    health_path = resolve_project_path(root, str(runtime["health_file"]))
    try:
        health = read_json(health_path)
        last_success = parse_timestamp(health.get("last_success_at"))
        age_seconds = (
            (
                datetime.now(timezone.utc) - last_success
            ).total_seconds()
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
    except (OSError, ValueError, TypeError, AttributeError):
        health_ok = False
        age_seconds = float("inf")
        max_age = int(runtime.get("max_poll_age_seconds", 180))
    checks.append(
        Check(
            "runtime.poll_health",
            "pass" if health_ok else "fail",
            (
                f"Watcher poll свежий, age={round(age_seconds, 1)}s."
                if health_ok
                else "Watcher poll не обновляется или находится в ошибке."
            ),
            "Проверь loaded LaunchAgent poll, Keychain token и последний "
            "health error. Не запускай Browser вместо сломанного API poll.",
            {
                "age_seconds": (
                    round(age_seconds, 1)
                    if age_seconds != float("inf")
                    else None
                ),
                "max_age_seconds": max_age,
            },
        )
    )

    skill = contract.get("skill", {})
    source_skill = resolve_project_path(root, str(skill["source"]))
    installed_skill = resolve_home_path(home, str(skill["installed"]))
    if not source_skill.is_dir() or not installed_skill.is_dir():
        skill_ok = False
        details = {
            "source_exists": source_skill.is_dir(),
            "installed_exists": installed_skill.is_dir(),
        }
    else:
        source_digest = tree_digest(source_skill)
        installed_digest = tree_digest(installed_skill)
        skill_ok = source_digest == installed_digest
        details = {
            "source_sha256": source_digest,
            "installed_sha256": installed_digest,
        }
    checks.append(
        Check(
            "deployment.x_skill",
            "pass" if skill_ok else "fail",
            (
                "Установленный X skill совпадает с Git-копией."
                if skill_ok
                else "Установленный X skill расходится с Git-копией."
            ),
            "После проверки скопируй skill-backup/x-twitter-operator в "
            "~/.codex/skills/x-twitter-operator.",
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
