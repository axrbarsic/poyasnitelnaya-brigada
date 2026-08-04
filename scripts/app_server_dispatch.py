#!/usr/bin/env python3
"""Wake Codex Desktop for the Browser owner only when durable work is ready."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    from scripts import (
        app_server_desktop,
        autopilot_bridge,
        autopilot_dispatch,
        autopilot_supervisor,
        resource_guard,
    )
except ModuleNotFoundError:
    import app_server_desktop  # type: ignore[no-redef]
    import autopilot_bridge  # type: ignore[no-redef]
    import autopilot_dispatch  # type: ignore[no-redef]
    import autopilot_supervisor  # type: ignore[no-redef]
    import resource_guard  # type: ignore[no-redef]


class AppServerError(RuntimeError):
    """Raised when Codex Desktop ownership cannot be established safely."""


def desktop_processes(
    executable: str = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
) -> list[int]:
    """Return exact ChatGPT Desktop process ids without using a model."""

    completed = subprocess.run(
        ["/bin/ps", "-axo", "pid=,command="],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    result: list[int] = []
    for raw_line in completed.stdout.splitlines():
        parts = raw_line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        command = parts[1]
        if command != executable and not command.startswith(executable + " "):
            continue
        try:
            result.append(int(parts[0]))
        except ValueError:
            continue
    return result


def launch_desktop(
    *,
    executable: Path,
    cwd: Path,
    process_executable: str,
    timeout_seconds: int,
) -> list[int]:
    """Launch the supported Desktop workspace and wait for its main process."""

    if timeout_seconds <= 0:
        raise ValueError("desktop launch timeout must be positive")
    before = set(desktop_processes(process_executable))
    if before:
        raise AppServerError(
            "Codex Desktop appeared before the supervisor launch"
        )
    completed = subprocess.run(
        [str(executable), "app", str(cwd)],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        processes = desktop_processes(process_executable)
        if processes:
            new_processes = sorted(set(processes) - before)
            if len(new_processes) != 1 or len(processes) != 1:
                raise AppServerError(
                    "Codex Desktop launch ownership is ambiguous"
                )
            return new_processes
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise AppServerError(
                "Codex Desktop launch failed"
                + (f": {detail}" if detail else "")
            )
        time.sleep(0.5)
    raise AppServerError("Codex Desktop did not become ready before timeout")


def notify_desktop_failure(message: str) -> bool:
    """Send a local macOS notification after a verified launch failure."""

    script = (
        'display notification "'
        + message.replace("\\", "\\\\").replace('"', '\\"')
        + '" with title "Пояснительная бригада"'
    )
    completed = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.returncode == 0


def stop_managed_desktop(
    pid: int,
    *,
    process_executable: str,
    timeout_seconds: int = 15,
) -> bool:
    """Gracefully stop only the exact Desktop process launched by supervisor."""

    if pid not in desktop_processes(process_executable):
        return True
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if pid not in desktop_processes(process_executable):
            return True
        time.sleep(0.5)
    return False


@contextmanager
def exclusive_process_lock(path: Path) -> Iterator[bool]:
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


def resolve_path(config_path: Path, value: str) -> Path:
    return autopilot_dispatch.resolve_path(config_path, value)


def dispatch_state_path(
    config_path: Path,
    config: dict[str, Any],
) -> Path:
    return resolve_path(
        config_path,
        str(
            config.get(
                "app_server_dispatch_state_file",
                "var/app-server-dispatch.json",
            )
        ),
    )


def write_state(
    config_path: Path,
    config: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    result = {
        "version": 1,
        "checked_at": autopilot_dispatch.isoformat(),
        **payload,
    }
    autopilot_dispatch.atomic_write_json(
        dispatch_state_path(config_path, config),
        result,
    )


def _desktop_dependencies() -> app_server_desktop.Dependencies:
    return app_server_desktop.Dependencies(
        supervisor_gate=autopilot_supervisor.gate,
        bridge_gate=autopilot_bridge.gate,
        dispatch_state_path=dispatch_state_path,
        read_json=autopilot_dispatch.read_json,
        load_paths=autopilot_dispatch.load_paths,
        queue_status=autopilot_dispatch.status,
        resource_check=resource_guard.check,
        isoformat=autopilot_dispatch.isoformat,
        parse_time=autopilot_dispatch.parse_time,
        utc_now=autopilot_dispatch.utc_now,
        stop_managed_desktop=stop_managed_desktop,
        resolve_path=resolve_path,
        desktop_processes=desktop_processes,
        launch_desktop=launch_desktop,
        notify_desktop_failure=notify_desktop_failure,
        write_state=write_state,
        recoverable_launch_errors=(
            OSError,
            subprocess.SubprocessError,
            AppServerError,
        ),
    )


def desktop_supervisor_dispatch(
    config_path: Path,
    config: dict[str, Any],
    *,
    lease_seconds: int,
) -> dict[str, Any]:
    return app_server_desktop.dispatch(
        config_path,
        config,
        lease_seconds=lease_seconds,
        dependencies=_desktop_dependencies(),
    )


def dispatch(
    config_path: Path,
    *,
    lease_seconds: int,
) -> dict[str, Any]:
    config_path = config_path.expanduser().resolve()
    config = autopilot_dispatch.read_json(config_path)
    relay_mode = str(config.get("desktop_relay_mode", "")).strip()
    if relay_mode != "in_app_heartbeat":
        raise ValueError(
            "desktop_relay_mode must be exactly in_app_heartbeat"
        )
    return desktop_supervisor_dispatch(
        config_path,
        config,
        lease_seconds=lease_seconds,
    )


def run_once(
    config_path: Path,
    *,
    lease_seconds: int,
) -> dict[str, Any]:
    config_path = config_path.expanduser().resolve()
    config = autopilot_dispatch.read_json(config_path)
    lock_path = resolve_path(
        config_path,
        str(
            config.get(
                "app_server_dispatch_lock_file",
                "var/app-server-dispatch.lock",
            )
        ),
    )
    with exclusive_process_lock(lock_path) as acquired:
        if not acquired:
            result = {
                "status": "dispatcher_busy",
                "dispatched": False,
                "event_ids": [],
                "pending_count": 0,
            }
            write_state(config_path, config, result)
            return result
        return dispatch(config_path, lease_seconds=lease_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lease-seconds", type=int, default=1800)
    arguments = parser.parse_args()
    try:
        result = run_once(
            arguments.config,
            lease_seconds=arguments.lease_seconds,
        )
    except (AppServerError, OSError, ValueError) as error:
        print(
            json.dumps(
                {"status": "failed", "error": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
