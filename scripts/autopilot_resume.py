#!/usr/bin/env python3
"""Wake one existing Codex Browser-owner session for queued X events."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

try:
    from scripts import autopilot_dispatch
except ModuleNotFoundError:
    import autopilot_dispatch  # type: ignore[no-redef]


Runner = Callable[..., subprocess.CompletedProcess[str]]

WAKE_CONTRACT = """СТОЯЧЕЕ РАЗРЕШЕНИЕ X-АВТОПИЛОТА.
Ты являешься единственным Browser owner. Обработай перечисленные прямые ответы
как Sol High. Используй skill x-twitter-operator и встроенный Browser.

Для каждого события открой точный URL, восстанови полную ветку и историю,
проверь media и позицию автора, выполни актуальный фактчек первичными
источниками, двойную проверку дубля и выбери short, Pro или skip. Short пиши и
финально проверяй только Sol High. Follow-up к Pro отправляй только
screenshot-only с 0 символов в точную историческую conversation.

Публикуй без дополнительного одобрения только релевантные прямые ответы в
рамках ранее разрешенного X workflow. Не ставь лайки, не делай репосты,
подписки, личные сообщения, новые исходные посты и удаления. После каждой
публикации проверь точный URL, сохрани полную историю в watcher и durable
resolve. На iMac 8 GB держи одну вкладку X, одну ChatGPT и максимум один
активный Pro. После обработки сделай один свежий poll и закончи, если очередь
пуста. Не создавай автоматики, задачи или Browser helpers. В финале укажи
event ID, disposition и verified reply URL.
"""


def resolve_path(config_path: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return (config_path.resolve().parent / candidate).resolve()


def load_runtime(config_path: Path) -> dict[str, Any]:
    config = autopilot_dispatch.read_json(config_path)
    thread_id = str(config.get("browser_owner_thread_id", "")).strip()
    try:
        uuid.UUID(thread_id)
    except ValueError as error:
        raise ValueError("browser_owner_thread_id must be a UUID") from error

    codex_cli = resolve_path(
        config_path,
        str(config.get("codex_cli_path", "~/.local/bin/codex")),
    )
    browser_owner_cwd = resolve_path(
        config_path,
        str(config.get("browser_owner_cwd", ".")),
    )
    health_file = resolve_path(
        config_path,
        str(config.get("autopilot_health_file", "var/autopilot-health.json")),
    )
    last_message_file = resolve_path(
        config_path,
        str(
            config.get(
                "autopilot_last_message_file",
                "var/autopilot-last-message.txt",
            )
        ),
    )
    process_lock = resolve_path(
        config_path,
        str(config.get("autopilot_process_lock", "var/autopilot-resume.lock")),
    )
    rotation_after_runs = int(
        config.get("autopilot_owner_rotation_after_runs", 20)
    )
    notifications_enabled = bool(config.get("notifications_enabled", True))

    if not codex_cli.is_file() or not os.access(codex_cli, os.X_OK):
        raise ValueError(f"codex_cli_path is not executable: {codex_cli}")
    if not browser_owner_cwd.is_dir():
        raise ValueError(
            f"browser_owner_cwd is not a directory: {browser_owner_cwd}"
        )
    if rotation_after_runs <= 0:
        raise ValueError("autopilot_owner_rotation_after_runs must be positive")

    return {
        "thread_id": thread_id,
        "codex_cli": codex_cli,
        "browser_owner_cwd": browser_owner_cwd,
        "health_file": health_file,
        "last_message_file": last_message_file,
        "process_lock": process_lock,
        "rotation_after_runs": rotation_after_runs,
        "notifications_enabled": notifications_enabled,
    }


@contextmanager
def single_process(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def build_prompt(
    events: Sequence[dict[str, Any]],
    *,
    config_path: Path,
    browser_owner_cwd: Path,
) -> str:
    payload = json.dumps(
        {"events": list(events)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        f"{WAKE_CONTRACT}\n\n"
        f"WATCHER_CONFIG={config_path.resolve()}\n"
        f"BROWSER_OWNER_WORKSPACE={browser_owner_cwd.resolve()}\n"
        f"EVENTS_JSON:\n{payload}\n"
    )


def build_command(runtime: dict[str, Any]) -> list[str]:
    return [
        str(runtime["codex_cli"]),
        "exec",
        "resume",
        "--ephemeral",
        str(runtime["thread_id"]),
        "-m",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="high"',
        "--skip-git-repo-check",
        "-o",
        str(runtime["last_message_file"]),
        "-",
    ]


def write_health(
    path: Path,
    *,
    status: str,
    event_ids: Sequence[str],
    claim_token: str | None = None,
    exit_code: int | None = None,
    error: str | None = None,
    completed_runs: int = 0,
    rotation_after_runs: int = 20,
    owner_thread_id: str,
) -> None:
    payload: dict[str, Any] = {
        "version": 1,
        "updated_at": autopilot_dispatch.isoformat(),
        "status": status,
        "event_ids": list(event_ids),
        "owner_thread_id": owner_thread_id,
        "completed_runs": completed_runs,
        "rotation_after_runs": rotation_after_runs,
        "rotation_recommended": completed_runs >= rotation_after_runs,
    }
    if claim_token is not None:
        payload["claim_token"] = claim_token
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if error:
        payload["error"] = error[:1000]
    autopilot_dispatch.atomic_write_json(path, payload)


def completed_run_count(path: Path, owner_thread_id: str) -> int:
    if not path.exists():
        return 0
    try:
        payload = autopilot_dispatch.read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return 0
    if payload.get("owner_thread_id") != owner_thread_id:
        return 0
    value = payload.get("completed_runs", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def health_status(path: Path, owner_thread_id: str) -> str | None:
    if not path.exists():
        return None
    try:
        payload = autopilot_dispatch.read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if payload.get("owner_thread_id") != owner_thread_id:
        return None
    value = payload.get("status")
    return str(value) if value else None


def notify_macos(enabled: bool, title: str, body: str) -> None:
    if not enabled or sys.platform != "darwin":
        return
    try:
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                "on run argv",
                "-e",
                "display notification (item 2 of argv) "
                "with title (item 1 of argv)",
                "-e",
                "end run",
                title,
                body,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def run_once(
    config_path: Path,
    *,
    lease_seconds: int,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    runtime = load_runtime(config_path)
    wake_file, state_file = autopilot_dispatch.load_paths(config_path)
    with single_process(runtime["process_lock"]) as acquired:
        if not acquired:
            return {"status": "busy", "dispatch": False}
        completed_runs = completed_run_count(
            runtime["health_file"],
            runtime["thread_id"],
        )
        previous_status = health_status(
            runtime["health_file"],
            runtime["thread_id"],
        )

        claim = autopilot_dispatch.claim(
            wake_file,
            state_file,
            lease_seconds=lease_seconds,
        )
        if not claim["dispatch"]:
            pending_events = autopilot_dispatch.load_wake_events(wake_file)
            pending_ids = [str(event["id"]) for event in pending_events]
            status = "leased_waiting" if pending_ids else "idle"
            write_health(
                runtime["health_file"],
                status=status,
                event_ids=pending_ids,
                owner_thread_id=runtime["thread_id"],
                completed_runs=completed_runs,
                rotation_after_runs=runtime["rotation_after_runs"],
            )
            return {"status": status, **claim}

        events = claim["events"]
        event_ids = [str(event["id"]) for event in events]
        claim_token = str(claim["claim_token"])
        runtime["last_message_file"].parent.mkdir(parents=True, exist_ok=True)
        write_health(
            runtime["health_file"],
            status="running",
            event_ids=event_ids,
            claim_token=claim_token,
            owner_thread_id=runtime["thread_id"],
            completed_runs=completed_runs,
            rotation_after_runs=runtime["rotation_after_runs"],
        )

        stderr_text = ""
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
            try:
                completed = runner(
                    build_command(runtime),
                    input=build_prompt(
                        events,
                        config_path=config_path,
                        browser_owner_cwd=runtime["browser_owner_cwd"],
                    ),
                    text=True,
                    cwd=runtime["browser_owner_cwd"],
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    check=False,
                )
            except OSError as error:
                autopilot_dispatch.release(state_file, claim_token)
                write_health(
                    runtime["health_file"],
                    status="failed",
                    event_ids=event_ids,
                    claim_token=claim_token,
                    error=str(error),
                    owner_thread_id=runtime["thread_id"],
                    completed_runs=completed_runs,
                    rotation_after_runs=runtime["rotation_after_runs"],
                )
                if previous_status != "failed":
                    notify_macos(
                        runtime["notifications_enabled"],
                        "Требуется проверка X автопилота",
                        "Codex Browser-owner не запустился.",
                    )
                raise
            if isinstance(completed.stderr, str):
                stderr_text = completed.stderr
            else:
                stderr_file.flush()
                end = stderr_file.tell()
                stderr_file.seek(max(0, end - 4000))
                stderr_text = stderr_file.read()

        if completed.returncode != 0:
            autopilot_dispatch.release(state_file, claim_token)
            write_health(
                runtime["health_file"],
                status="failed",
                event_ids=event_ids,
                claim_token=claim_token,
                exit_code=completed.returncode,
                error=stderr_text or "Codex resume failed",
                owner_thread_id=runtime["thread_id"],
                completed_runs=completed_runs,
                rotation_after_runs=runtime["rotation_after_runs"],
            )
            if previous_status != "failed":
                notify_macos(
                    runtime["notifications_enabled"],
                    "Требуется проверка X автопилота",
                    "Codex Browser-owner завершился с ошибкой.",
                )
            return {
                "status": "failed",
                "dispatch": True,
                "event_ids": event_ids,
                "exit_code": completed.returncode,
            }

        pending_after = {
            str(event["id"])
            for event in autopilot_dispatch.load_wake_events(wake_file)
        }
        unresolved_ids = [
            event_id for event_id in event_ids if event_id in pending_after
        ]
        completed_status = (
            "completed_unresolved" if unresolved_ids else "completed"
        )
        write_health(
            runtime["health_file"],
            status=completed_status,
            event_ids=unresolved_ids or event_ids,
            claim_token=claim_token,
            exit_code=0,
            error=(
                "Browser owner exited without resolving leased events"
                if unresolved_ids
                else None
            ),
            owner_thread_id=runtime["thread_id"],
            completed_runs=completed_runs + 1,
            rotation_after_runs=runtime["rotation_after_runs"],
        )
        if completed_status == "completed_unresolved":
            if previous_status != "completed_unresolved":
                notify_macos(
                    runtime["notifications_enabled"],
                    "Требуется проверка X автопилота",
                    "Browser-owner завершился, но событие осталось в очереди.",
                )
        elif previous_status in {"failed", "completed_unresolved"}:
            notify_macos(
                runtime["notifications_enabled"],
                "X автопилот восстановлен",
                "Новый Browser-owner run завершился успешно.",
            )
        return {
            "status": completed_status,
            "dispatch": True,
            "event_ids": event_ids,
            "unresolved_event_ids": unresolved_ids,
            "exit_code": 0,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lease-seconds", type=int, default=1800)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = run_once(
            arguments.config,
            lease_seconds=arguments.lease_seconds,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
