#!/usr/bin/env python3
"""External app-server relay domain with explicit runtime dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class Dependencies:
    resolve_path: Callable[[Path, str], Path]
    runtime_signature: Callable[[Path], str]
    dispatch_state_path: Callable[..., Path]
    read_json: Callable[[Path], dict[str, Any]]
    write_state: Callable[..., None]
    client_factory: Callable[..., Any]
    relay_handoff_confirmed: Callable[[dict[str, Any]], bool]
    wait_owner_resolution: Callable[..., None]
    monotonic: Callable[[], float]
    app_server_error: type[Exception]
    relay_capability_error: type[Exception]
    relay_prompt: str
    owner_prompt: str


def idle_result(
    config_path: Path,
    config: dict[str, Any],
    gate: dict[str, Any],
    dependencies: Dependencies,
) -> dict[str, Any]:
    result = {
        "status": str(gate.get("status", "idle")),
        "dispatched": False,
        "event_ids": list(gate.get("event_ids", [])),
        "pending_count": int(gate.get("pending_count", 0)),
    }
    dependencies.write_state(config_path, config, result)
    return result


def _thread_ids(config: dict[str, Any]) -> tuple[str, str]:
    owner_thread_id = str(config.get("browser_owner_thread_id", "")).strip()
    relay_thread_id = str(config.get("app_server_relay_thread_id", "")).strip()
    if not owner_thread_id:
        raise ValueError("browser_owner_thread_id is required")
    if not relay_thread_id:
        raise ValueError("app_server_relay_thread_id is required")
    return owner_thread_id, relay_thread_id


def _runtime_settings(
    config_path: Path,
    config: dict[str, Any],
    *,
    timeout_seconds: int | None,
    dependencies: Dependencies,
) -> tuple[Path, Path, str, dict[str, Any], int]:
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
    signature = dependencies.runtime_signature(executable)
    state_path = dependencies.dispatch_state_path(config_path, config)
    previous = dependencies.read_json(state_path) if state_path.exists() else {}
    timeout = int(
        timeout_seconds
        if timeout_seconds is not None
        else config.get("app_server_dispatch_timeout_seconds", 7200)
    )
    if timeout <= 0:
        raise ValueError("app-server dispatch timeout must be positive")
    return cwd, executable, signature, previous, timeout


def _blocked_runtime_result(
    config_path: Path,
    config: dict[str, Any],
    gate: dict[str, Any],
    *,
    signature: str,
    previous: dict[str, Any],
    dependencies: Dependencies,
) -> dict[str, Any] | None:
    blocked = (
        previous.get("status") == "blocked"
        and previous.get("blocker_code") == "cross_thread_tool_unavailable"
        and previous.get("relay_runtime_signature") == signature
    )
    if not blocked:
        return None
    result = {
        "status": "relay_blocked",
        "dispatched": False,
        "event_ids": list(gate.get("event_ids", [])),
        "pending_count": int(gate.get("pending_count", 0)),
        "blocker_code": "cross_thread_tool_unavailable",
        "relay_runtime_signature": signature,
    }
    dependencies.write_state(config_path, config, result)
    return result


def _initialize_client(
    client: Any,
    *,
    relay_thread_id: str,
    cwd: Path,
    deadline: float,
    dependencies: Dependencies,
) -> None:
    client.send(
        {
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {
                    "name": "poyasnitelnaya-brigada-autopilot",
                    "title": "Poyasnitelnaya Brigada Autopilot",
                    "version": "1.0.0",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": [
                        "item/agentMessage/delta",
                        "item/reasoning/summaryTextDelta",
                    ],
                },
            },
        }
    )
    client.wait_response(0, deadline=deadline)
    client.send({"method": "initialized", "params": {}})
    client.send(
        {
            "method": "thread/unarchive",
            "id": 1,
            "params": {"threadId": relay_thread_id},
        }
    )
    unarchived = client.wait_response(1, deadline=deadline)
    unarchived_thread = unarchived.get("thread")
    if (
        not isinstance(unarchived_thread, dict)
        or unarchived_thread.get("id") != relay_thread_id
    ):
        raise dependencies.app_server_error("unarchived the wrong relay thread")
    client.send(
        {
            "method": "thread/resume",
            "id": 2,
            "params": {
                "threadId": relay_thread_id,
                "cwd": str(cwd),
                "model": "gpt-5.6-luna",
                "approvalPolicy": "never",
            },
        }
    )
    resumed = client.wait_response(2, deadline=deadline)
    resumed_thread = resumed.get("thread")
    if (
        not isinstance(resumed_thread, dict)
        or resumed_thread.get("id") != relay_thread_id
    ):
        raise dependencies.app_server_error("resumed the wrong relay thread")


def _start_turn(
    client: Any,
    *,
    relay_thread_id: str,
    owner_thread_id: str,
    cwd: Path,
    root: Path,
    lease_seconds: int,
    deadline: float,
    dependencies: Dependencies,
) -> tuple[str, dict[str, Any]]:
    client.send(
        {
            "method": "turn/start",
            "id": 3,
            "params": {
                "threadId": relay_thread_id,
                "cwd": str(cwd),
                "model": "gpt-5.6-luna",
                "effort": "low",
                "approvalPolicy": "never",
                "input": [
                    {
                        "type": "text",
                        "text": dependencies.relay_prompt.format(
                            owner_thread_id=owner_thread_id,
                            owner_prompt=dependencies.owner_prompt.format(
                                root=root,
                                lease_seconds=lease_seconds,
                            ),
                        ),
                    }
                ],
            },
        }
    )
    started = client.wait_response(3, deadline=deadline)
    turn = started.get("turn")
    if not isinstance(turn, dict) or not turn.get("id"):
        raise dependencies.app_server_error("turn/start returned no turn id")
    turn_id = str(turn["id"])
    completed = client.wait_turn(
        thread_id=relay_thread_id,
        turn_id=turn_id,
        deadline=deadline,
    )
    return turn_id, completed


def _archive_thread(
    client: Any,
    *,
    relay_thread_id: str,
    deadline: float,
) -> None:
    client.send(
        {
            "method": "thread/archive",
            "id": 4,
            "params": {"threadId": relay_thread_id},
        }
    )
    client.wait_response(4, deadline=deadline)


def _run_protocol(
    client: Any,
    *,
    relay_thread_id: str,
    owner_thread_id: str,
    cwd: Path,
    root: Path,
    lease_seconds: int,
    deadline: float,
    dependencies: Dependencies,
) -> str:
    _initialize_client(
        client,
        relay_thread_id=relay_thread_id,
        cwd=cwd,
        deadline=deadline,
        dependencies=dependencies,
    )
    turn_id, completed = _start_turn(
        client,
        relay_thread_id=relay_thread_id,
        owner_thread_id=owner_thread_id,
        cwd=cwd,
        root=root,
        lease_seconds=lease_seconds,
        deadline=deadline,
        dependencies=dependencies,
    )
    _archive_thread(
        client,
        relay_thread_id=relay_thread_id,
        deadline=deadline,
    )
    status = str(completed.get("status", "failed"))
    if status != "completed":
        raise dependencies.app_server_error(
            f"relay turn finished with status {status}"
        )
    if not dependencies.relay_handoff_confirmed(completed):
        raise dependencies.relay_capability_error(
            "relay completed without a confirmed owner handoff"
        )
    return turn_id


def _write_failure(
    config_path: Path,
    config: dict[str, Any],
    gate: dict[str, Any],
    error: Exception,
    *,
    signature: str,
    dependencies: Dependencies,
) -> None:
    unavailable = isinstance(error, dependencies.relay_capability_error)
    payload = {
        "status": "blocked" if unavailable else "failed",
        "dispatched": True,
        "event_ids": list(gate.get("event_ids", [])),
        "pending_count": int(gate.get("pending_count", 0)),
        "error": str(error),
    }
    if unavailable:
        payload["blocker_code"] = "cross_thread_tool_unavailable"
        payload["relay_runtime_signature"] = signature
    dependencies.write_state(config_path, config, payload)


def dispatch(
    config_path: Path,
    config: dict[str, Any],
    gate: dict[str, Any],
    *,
    lease_seconds: int,
    timeout_seconds: int | None,
    dependencies: Dependencies,
) -> dict[str, Any]:
    owner_thread_id, relay_thread_id = _thread_ids(config)
    root = config_path.parent.resolve()
    cwd, executable, signature, previous, timeout = _runtime_settings(
        config_path,
        config,
        timeout_seconds=timeout_seconds,
        dependencies=dependencies,
    )
    blocked = _blocked_runtime_result(
        config_path,
        config,
        gate,
        signature=signature,
        previous=previous,
        dependencies=dependencies,
    )
    if blocked is not None:
        return blocked
    client = dependencies.client_factory(
        executable=executable,
        cwd=cwd,
        timeout_seconds=timeout,
    )
    deadline = dependencies.monotonic() + timeout
    try:
        turn_id = _run_protocol(
            client,
            relay_thread_id=relay_thread_id,
            owner_thread_id=owner_thread_id,
            cwd=cwd,
            root=root,
            lease_seconds=lease_seconds,
            deadline=deadline,
            dependencies=dependencies,
        )
        client.close()
        event_ids = list(gate.get("event_ids", []))
        dependencies.wait_owner_resolution(
            config_path,
            config,
            event_ids=event_ids,
            deadline=deadline,
        )
        result = {
            "status": "completed",
            "dispatched": True,
            "relay_thread_id": relay_thread_id,
            "owner_thread_id": owner_thread_id,
            "turn_id": turn_id,
            "event_ids": event_ids,
            "pending_count": int(gate.get("pending_count", 0)),
        }
        dependencies.write_state(config_path, config, result)
        return result
    except Exception as error:
        _write_failure(
            config_path,
            config,
            gate,
            error,
            signature=signature,
            dependencies=dependencies,
        )
        raise
    finally:
        client.close()
