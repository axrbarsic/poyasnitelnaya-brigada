#!/usr/bin/env python3
"""In-app Desktop dispatch domain with explicit runtime dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class Dependencies:
    supervisor_gate: Callable[..., dict[str, Any]]
    bridge_gate: Callable[..., dict[str, Any]]
    dispatch_state_path: Callable[..., Path]
    read_json: Callable[[Path], dict[str, Any]]
    load_paths: Callable[[Path], tuple[Path, Path]]
    queue_status: Callable[..., dict[str, Any]]
    resource_check: Callable[[Path], dict[str, Any]]
    isoformat: Callable[..., str]
    parse_time: Callable[[str], Any]
    utc_now: Callable[[], Any]
    stop_managed_desktop: Callable[..., bool]
    resolve_path: Callable[[Path, str], Path]
    desktop_processes: Callable[[str], list[int]]
    launch_desktop: Callable[..., list[int]]
    notify_desktop_failure: Callable[[str], bool]
    write_state: Callable[..., None]
    recoverable_launch_errors: tuple[type[BaseException], ...]


def _select_gate(
    config_path: Path,
    *,
    lease_seconds: int,
    dependencies: Dependencies,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    repair_gate = dependencies.supervisor_gate(config_path)
    if repair_gate.get("repair_pending"):
        return (
            repair_gate,
            {
                "status": str(repair_gate.get("status", "repair_pending")),
                "dispatch": bool(repair_gate.get("dispatch")),
                "event_ids": [],
                "pending_count": 0,
            },
            "repair",
        )
    return (
        repair_gate,
        dependencies.bridge_gate(config_path, lease_seconds=lease_seconds),
        "x",
    )


def _previous_state(
    config_path: Path,
    config: dict[str, Any],
    dependencies: Dependencies,
) -> dict[str, Any]:
    path = dependencies.dispatch_state_path(config_path, config)
    return dependencies.read_json(path) if path.exists() else {}


def _managed_desktop_identity(
    config: dict[str, Any],
    previous: dict[str, Any],
) -> tuple[bool, int | None, str]:
    managed_pid = previous.get("managed_desktop_pid")
    managed = (
        previous.get("managed_by_supervisor") is True
        and isinstance(managed_pid, int)
    )
    process_executable = str(
        config.get(
            "codex_desktop_process_path",
            "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
        )
    )
    return managed, managed_pid if managed else None, process_executable


def _idle_transition(
    config_path: Path,
    config: dict[str, Any],
    previous: dict[str, Any],
    gate: dict[str, Any],
    *,
    lease_seconds: int,
    dependencies: Dependencies,
) -> tuple[bool, bool, int | None, str | None]:
    managed, managed_pid, process_executable = _managed_desktop_identity(
        config,
        previous,
    )
    idle_since = previous.get("idle_since")
    if (
        str(gate.get("status", "idle")) != "idle"
        or not managed
        or not bool(config.get("desktop_auto_quit_after_work", False))
    ):
        return False, managed, managed_pid, idle_since
    wake_file, owner_state_file = dependencies.load_paths(config_path)
    queue = dependencies.queue_status(
        wake_file,
        owner_state_file,
        lease_seconds=lease_seconds,
    )
    guard = dependencies.resource_check(config_path)
    if queue["owner_busy"] or guard.get("defer"):
        return False, managed, managed_pid, idle_since
    if not idle_since:
        return False, managed, managed_pid, dependencies.isoformat()
    grace = int(config.get("desktop_auto_quit_grace_seconds", 180))
    if grace < 0:
        raise ValueError("desktop auto quit grace must not be negative")
    parsed_idle = dependencies.parse_time(str(idle_since))
    due = (
        parsed_idle is not None
        and (dependencies.utc_now() - parsed_idle).total_seconds() >= grace
    )
    if not due or managed_pid is None:
        return False, managed, managed_pid, idle_since
    closed = dependencies.stop_managed_desktop(
        managed_pid,
        process_executable=process_executable,
    )
    return closed, managed, managed_pid, idle_since


def _idle_result(
    config_path: Path,
    config: dict[str, Any],
    previous: dict[str, Any],
    gate: dict[str, Any],
    *,
    lease_seconds: int,
    event_ids: list[str],
    pending_count: int,
    work_kind: str,
    dependencies: Dependencies,
) -> dict[str, Any]:
    closed, managed, managed_pid, idle_since = _idle_transition(
        config_path,
        config,
        previous,
        gate,
        lease_seconds=lease_seconds,
        dependencies=dependencies,
    )
    result = {
        "status": (
            "desktop_closed_after_work"
            if closed
            else str(gate.get("status", "idle"))
        ),
        "mode": "in_app_heartbeat",
        "dispatched": False,
        "desktop_launched": False,
        "desktop_closed": closed,
        "event_ids": event_ids,
        "pending_count": pending_count,
        "work_kind": work_kind,
    }
    if managed and not closed and managed_pid is not None:
        result["managed_by_supervisor"] = True
        result["managed_desktop_pid"] = managed_pid
    if idle_since and not closed:
        result["idle_since"] = idle_since
    return result


def _runtime_settings(
    config_path: Path,
    config: dict[str, Any],
    dependencies: Dependencies,
) -> tuple[str, Path, Path, str, int]:
    owner_thread_id = str(config.get("browser_owner_thread_id", "")).strip()
    if not owner_thread_id:
        raise ValueError("browser_owner_thread_id is required")
    cwd = dependencies.resolve_path(
        config_path,
        str(config.get("browser_owner_cwd", ".")),
    )
    executable = Path(
        str(
            config.get(
                "codex_cli_path",
                "/Applications/ChatGPT.app/Contents/Resources/codex",
            )
        )
    ).expanduser()
    process_executable = str(
        config.get(
            "codex_desktop_process_path",
            "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
        )
    )
    timeout_seconds = int(config.get("desktop_launch_timeout_seconds", 30))
    return (
        owner_thread_id,
        cwd,
        executable,
        process_executable,
        timeout_seconds,
    )


def _launch_failure_result(
    config: dict[str, Any],
    previous: dict[str, Any],
    *,
    owner_thread_id: str,
    event_ids: list[str],
    pending_count: int,
    work_kind: str,
    error: Exception,
    dependencies: Dependencies,
) -> dict[str, Any]:
    alert_interval = int(
        config.get("desktop_failure_alert_interval_seconds", 1800)
    )
    if alert_interval < 0:
        raise ValueError("desktop failure alert interval must not be negative")
    last_alert_at = dependencies.parse_time(
        str(previous.get("last_alert_at", ""))
    )
    should_alert = (
        last_alert_at is None
        or (dependencies.utc_now() - last_alert_at).total_seconds()
        >= alert_interval
    )
    alert_sent = (
        dependencies.notify_desktop_failure(
            "Не удалось запустить Codex Desktop. "
            "Работа сохранена, автопилот повторит попытку."
        )
        if should_alert
        else False
    )
    result = {
        "status": "desktop_launch_failed",
        "mode": "in_app_heartbeat",
        "dispatched": False,
        "desktop_launched": False,
        "alert_sent": alert_sent,
        "owner_thread_id": owner_thread_id,
        "event_ids": event_ids,
        "pending_count": pending_count,
        "work_kind": work_kind,
        "error": str(error),
    }
    if alert_sent:
        result["last_alert_at"] = dependencies.isoformat()
    elif previous.get("last_alert_at"):
        result["last_alert_at"] = previous["last_alert_at"]
    return result


def _waiting_result(
    previous: dict[str, Any],
    *,
    launched: bool,
    processes: list[int],
    owner_thread_id: str,
    event_ids: list[str],
    pending_count: int,
    work_kind: str,
    dependencies: Dependencies,
) -> dict[str, Any]:
    waiting_statuses = {
        "desktop_launched_waiting_relay",
        "desktop_ready_waiting_relay",
    }
    preserve_waiting_since = (
        previous.get("status") in waiting_statuses
        and previous.get("work_kind") == work_kind
        and previous.get("waiting_since")
    )
    result = {
        "status": (
            "desktop_launched_waiting_relay"
            if launched
            else "desktop_ready_waiting_relay"
        ),
        "mode": "in_app_heartbeat",
        "dispatched": False,
        "desktop_launched": launched,
        "desktop_pids": processes,
        "owner_thread_id": owner_thread_id,
        "event_ids": event_ids,
        "pending_count": pending_count,
        "waiting_since": (
            previous["waiting_since"]
            if preserve_waiting_since
            else dependencies.isoformat()
        ),
        "work_kind": work_kind,
    }
    if launched:
        result["managed_by_supervisor"] = True
        result["managed_desktop_pid"] = processes[0]
    elif (
        previous.get("managed_by_supervisor") is True
        and previous.get("managed_desktop_pid") in processes
    ):
        result["managed_by_supervisor"] = True
        result["managed_desktop_pid"] = previous["managed_desktop_pid"]
    return result


def _attach_repair_incident(
    result: dict[str, Any],
    repair_gate: dict[str, Any],
) -> dict[str, Any]:
    if repair_gate.get("incident_id"):
        result["repair_incident_id"] = repair_gate["incident_id"]
    return result


def dispatch(
    config_path: Path,
    config: dict[str, Any],
    *,
    lease_seconds: int,
    dependencies: Dependencies,
) -> dict[str, Any]:
    """Keep empty cycles model-free and launch Desktop only for real work."""

    repair_gate, gate, work_kind = _select_gate(
        config_path,
        lease_seconds=lease_seconds,
        dependencies=dependencies,
    )
    previous = _previous_state(config_path, config, dependencies)
    event_ids = list(gate.get("event_ids", []))
    pending_count = int(gate.get("pending_count", 0))
    if not gate.get("dispatch"):
        result = _idle_result(
            config_path,
            config,
            previous,
            gate,
            lease_seconds=lease_seconds,
            event_ids=event_ids,
            pending_count=pending_count,
            work_kind=work_kind,
            dependencies=dependencies,
        )
    else:
        result = _dispatch_work(
            config_path,
            config,
            previous,
            repair_gate,
            event_ids=event_ids,
            pending_count=pending_count,
            work_kind=work_kind,
            dependencies=dependencies,
        )
        return result
    result = _attach_repair_incident(result, repair_gate)
    dependencies.write_state(config_path, config, result)
    return result


def _dispatch_work(
    config_path: Path,
    config: dict[str, Any],
    previous: dict[str, Any],
    repair_gate: dict[str, Any],
    *,
    event_ids: list[str],
    pending_count: int,
    work_kind: str,
    dependencies: Dependencies,
) -> dict[str, Any]:
    (
        owner_thread_id,
        cwd,
        executable,
        process_executable,
        timeout_seconds,
    ) = _runtime_settings(config_path, config, dependencies)
    processes = dependencies.desktop_processes(process_executable)
    launched = False
    if not processes:
        try:
            processes = dependencies.launch_desktop(
                executable=executable,
                cwd=cwd,
                process_executable=process_executable,
                timeout_seconds=timeout_seconds,
            )
            launched = True
        except dependencies.recoverable_launch_errors as error:
            result = _launch_failure_result(
                config,
                previous,
                owner_thread_id=owner_thread_id,
                event_ids=event_ids,
                pending_count=pending_count,
                work_kind=work_kind,
                error=error,
                dependencies=dependencies,
            )
            result = _attach_repair_incident(result, repair_gate)
            dependencies.write_state(config_path, config, result)
            return result
    result = _waiting_result(
        previous,
        launched=launched,
        processes=processes,
        owner_thread_id=owner_thread_id,
        event_ids=event_ids,
        pending_count=pending_count,
        work_kind=work_kind,
        dependencies=dependencies,
    )
    result = _attach_repair_incident(result, repair_gate)
    dependencies.write_state(config_path, config, result)
    return result
