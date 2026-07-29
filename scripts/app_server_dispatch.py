#!/usr/bin/env python3
"""Wake Codex Desktop for the pinned Browser owner only when work is ready."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import selectors
import signal
import subprocess
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    from scripts import (
        autopilot_bridge,
        autopilot_dispatch,
        autopilot_supervisor,
        resource_guard,
    )
except ModuleNotFoundError:
    import autopilot_bridge  # type: ignore[no-redef]
    import autopilot_dispatch  # type: ignore[no-redef]
    import autopilot_supervisor  # type: ignore[no-redef]
    import resource_guard  # type: ignore[no-redef]


OWNER_PROMPT = """Служебное пробуждение X автопилота. Работай только в {root}.
Сразу выполни `python3 scripts/autopilot_bridge.py --config config.json
--lease-seconds {lease_seconds} claim` и разбери JSON. Если dispatch=false,
заверши без Browser. Если dispatch=true, прочитай AGENTS.md,
.codex/config.toml, полный skill x-twitter-operator и только требуемые им
references. Затем выполни `python3 scripts/autopilot_bridge.py --config
config.json started --claim-token CLAIM_TOKEN` и дословно исполни prompt из
claim как единственный Browser owner на Sol High. После durable history и
durable resolve всех заявленных событий выполни `python3
scripts/autopilot_bridge.py --config config.json completed --claim-token
CLAIM_TOKEN`. При ошибке до durable resolve выполни `python3
scripts/autopilot_bridge.py --config config.json failed --claim-token
CLAIM_TOKEN --error WORK_ERROR`. Всегда закрой только task-owned
Browser-вкладки, не самоархивируйся и не запускай дополнительный poll."""

RELAY_PROMPT = """Ты только событийный ретранслятор X автопилота.
Не используй Browser, shell или файлы. Сначала через `tool_search` найди прямой
Codex app tool `codex_app.send_message_to_thread`, затем ровно один раз вызови
его для threadId `{owner_thread_id}`, model `gpt-5.6-sol`, thinking `high` и
передай ему следующий prompt дословно:

<owner_prompt>
{owner_prompt}
</owner_prompt>

После подтверждения отправки сразу заверши. Не создавай новую сессию, не жди
ответ владельца и не отправляй повторно."""


class AppServerError(RuntimeError):
    """Raised when app-server cannot complete the requested turn."""


class RelayCapabilityUnavailable(AppServerError):
    """Raised when external app-server cannot reach the in-app owner."""


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
    """Yield whether this process owns the dispatcher singleton lock."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class AppServerClient:
    def __init__(
        self,
        *,
        executable: Path,
        cwd: Path,
        timeout_seconds: int,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.stderr_tail: deque[str] = deque(maxlen=40)
        self.pending_messages: deque[dict[str, Any]] = deque()
        self.next_request_id = 1000
        self.process = subprocess.Popen(
            [str(executable), "app-server", "--listen", "stdio://"],
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        if (
            self.process.stdin is None
            or self.process.stdout is None
            or self.process.stderr is None
        ):
            self.close()
            raise AppServerError("app-server pipes are unavailable")
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.selector.register(self.process.stderr, selectors.EVENT_READ)

    def send(self, payload: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise AppServerError("app-server stdin is closed")
        self.process.stdin.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
        self.process.stdin.flush()

    def read_message(self, *, deadline: float) -> dict[str, Any] | None:
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AppServerError(
                    f"app-server exited with {self.process.returncode}: "
                    + " | ".join(self.stderr_tail)
                )
            events = self.selector.select(
                timeout=min(1.0, max(0.0, deadline - time.monotonic()))
            )
            for key, _ in events:
                line = key.fileobj.readline()
                if not line:
                    continue
                if key.fileobj is self.process.stderr:
                    self.stderr_tail.append(line.rstrip())
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as error:
                    raise AppServerError(
                        f"invalid app-server JSON: {line[:200]!r}"
                    ) from error
                if not isinstance(message, dict):
                    raise AppServerError("app-server message is not an object")
                return message
        return None

    def messages(self, *, deadline: float):
        while True:
            message = self.read_message(deadline=deadline)
            if message is None:
                break
            yield message
        raise AppServerError("app-server response timeout")

    def wait_response(
        self,
        request_id: int,
        *,
        deadline: float,
    ) -> dict[str, Any]:
        retained: deque[dict[str, Any]] = deque()
        try:
            while self.pending_messages:
                message = self.pending_messages.popleft()
                if message.get("id") != request_id:
                    retained.append(message)
                    continue
                return self._response_result(message)
            for message in self.messages(deadline=deadline):
                if message.get("id") != request_id:
                    retained.append(message)
                    continue
                return self._response_result(message)
        finally:
            self.pending_messages.extendleft(reversed(retained))
        raise AppServerError("app-server response stream ended")

    @staticmethod
    def _response_result(message: dict[str, Any]) -> dict[str, Any]:
        if "error" in message:
            raise AppServerError(
                "app-server request failed: "
                + json.dumps(message["error"], ensure_ascii=False)
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise AppServerError("app-server response has no result")
        return result

    def wait_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        deadline: float,
    ) -> dict[str, Any]:
        probe_interval_seconds = 5.0
        while True:
            retained: deque[dict[str, Any]] = deque()
            try:
                while self.pending_messages:
                    message = self.pending_messages.popleft()
                    turn = self._matching_completed_turn(
                        message,
                        thread_id=thread_id,
                        turn_id=turn_id,
                    )
                    if turn is not None:
                        return turn
                    retained.append(message)
            finally:
                self.pending_messages.extendleft(reversed(retained))

            if time.monotonic() >= deadline:
                break
            probe_deadline = min(
                deadline,
                time.monotonic() + probe_interval_seconds,
            )
            message = self.read_message(deadline=probe_deadline)
            if message is not None:
                turn = self._matching_completed_turn(
                    message,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )
                if turn is not None:
                    return turn
                self.pending_messages.append(message)
                continue

            persisted = self.read_turn(
                thread_id=thread_id,
                turn_id=turn_id,
                deadline=deadline,
            )
            if persisted is None:
                continue
            if persisted.get("status") != "inProgress":
                return persisted
        raise AppServerError("app-server turn completion timeout")

    def read_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        deadline: float,
    ) -> dict[str, Any] | None:
        request_id = self.next_request_id
        self.next_request_id += 1
        self.send(
            {
                "method": "thread/read",
                "id": request_id,
                "params": {
                    "threadId": thread_id,
                    "includeTurns": True,
                },
            }
        )
        response_deadline = min(deadline, time.monotonic() + 10.0)
        result = self.wait_response(
            request_id,
            deadline=response_deadline,
        )
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise AppServerError("thread/read returned no thread")
        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise AppServerError("thread/read returned no turns")
        for turn in turns:
            if isinstance(turn, dict) and turn.get("id") == turn_id:
                return turn
        return None

    @staticmethod
    def _matching_completed_turn(
        message: dict[str, Any],
        *,
        thread_id: str,
        turn_id: str,
    ) -> dict[str, Any] | None:
        if message.get("method") != "turn/completed":
            return None
        params = message.get("params")
        if not isinstance(params, dict):
            return None
        turn = params.get("turn")
        if (
            params.get("threadId") != thread_id
            or not isinstance(turn, dict)
            or turn.get("id") != turn_id
        ):
            return None
        return turn

    def close(self) -> None:
        process = getattr(self, "process", None)
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)


def relay_handoff_confirmed(turn: dict[str, Any]) -> bool:
    """Return whether the relay completed the exact cross-thread tool call."""

    items = turn.get("items")
    if not isinstance(items, list):
        return False
    accepted_tools = {
        "send_message_to_thread",
        "codex_app.send_message_to_thread",
    }
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("tool", "")).strip() not in accepted_tools:
            continue
        item_type = str(item.get("type", ""))
        status = str(item.get("status", ""))
        if item_type == "dynamicToolCall":
            if status == "completed" and item.get("success") is not False:
                return True
        elif item_type == "mcpToolCall":
            server = str(item.get("server", "")).strip()
            if server == "codex_app" and status == "completed":
                return True
    return False


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


def runtime_signature(executable: Path) -> str:
    stat = executable.stat()
    return f"{executable.resolve()}:{stat.st_mtime_ns}:{stat.st_size}"


def write_state(
    config_path: Path,
    config: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    state_path = dispatch_state_path(config_path, config)
    result = {
        "version": 1,
        "checked_at": autopilot_dispatch.isoformat(),
        **payload,
    }
    autopilot_dispatch.atomic_write_json(state_path, result)


def wait_owner_resolution(
    config_path: Path,
    config: dict[str, Any],
    *,
    event_ids: list[str],
    deadline: float,
) -> None:
    expected = set(event_ids)
    if not expected:
        return
    wake_file = resolve_path(
        config_path,
        str(config.get("wake_file", "var/wake-request.json")),
    )
    dispatch_state_file = resolve_path(
        config_path,
        str(
            config.get(
                "autopilot_dispatch_state_file",
                "var/autopilot-dispatch.json",
            )
        ),
    )
    claim_timeout = int(
        config.get("app_server_owner_claim_timeout_seconds", 180)
    )
    claim_deadline = min(deadline, time.monotonic() + claim_timeout)
    observed_claim = False
    while time.monotonic() < deadline:
        pending = {
            str(event["id"])
            for event in autopilot_dispatch.load_wake_events(wake_file)
        }
        if expected.isdisjoint(pending):
            return
        state = autopilot_dispatch.read_json(dispatch_state_file)
        owner = state.get("owner")
        if isinstance(owner, dict):
            owner_events = {
                str(event_id)
                for event_id in owner.get("event_ids", [])
            }
            if expected.issubset(owner_events):
                observed_claim = True
        elif observed_claim:
            raise AppServerError(
                "Browser owner released the claim without durable resolve"
            )
        elif time.monotonic() >= claim_deadline:
            raise AppServerError(
                "Browser owner did not claim the dispatched events"
            )
        time.sleep(1.0)
    raise AppServerError("Browser owner completion timeout")


def desktop_supervisor_dispatch(
    config_path: Path,
    config: dict[str, Any],
    *,
    lease_seconds: int,
) -> dict[str, Any]:
    """Keep empty cycles model-free and launch Desktop only for real work."""

    repair_gate = autopilot_supervisor.gate(config_path)
    if repair_gate.get("repair_pending"):
        gate = {
            "status": str(repair_gate.get("status", "repair_pending")),
            "dispatch": bool(repair_gate.get("dispatch")),
            "event_ids": [],
            "pending_count": 0,
        }
        work_kind = "repair"
    else:
        gate = autopilot_bridge.gate(
            config_path,
            lease_seconds=lease_seconds,
        )
        work_kind = "x"
    state_path = dispatch_state_path(config_path, config)
    previous = (
        autopilot_dispatch.read_json(state_path)
        if state_path.exists()
        else {}
    )
    event_ids = list(gate.get("event_ids", []))
    pending_count = int(gate.get("pending_count", 0))
    if not gate.get("dispatch"):
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
        closed = False
        idle_since = previous.get("idle_since")
        if (
            str(gate.get("status", "idle")) == "idle"
            and managed
            and bool(config.get("desktop_auto_quit_after_work", False))
        ):
            wake_file, owner_state_file = autopilot_dispatch.load_paths(
                config_path
            )
            queue = autopilot_dispatch.status(
                wake_file,
                owner_state_file,
                lease_seconds=lease_seconds,
            )
            guard = resource_guard.check(config_path)
            if (
                not queue["owner_busy"]
                and not guard.get("defer")
                and not idle_since
            ):
                idle_since = autopilot_dispatch.isoformat()
            elif (
                not queue["owner_busy"]
                and not guard.get("defer")
                and idle_since
            ):
                parsed_idle = autopilot_dispatch.parse_time(str(idle_since))
                grace = int(
                    config.get(
                        "desktop_auto_quit_grace_seconds",
                        180,
                    )
                )
                if grace < 0:
                    raise ValueError(
                        "desktop auto quit grace must not be negative"
                    )
                if (
                    parsed_idle is not None
                    and (
                        autopilot_dispatch.utc_now() - parsed_idle
                    ).total_seconds()
                    >= grace
                ):
                    closed = stop_managed_desktop(
                        managed_pid,
                        process_executable=process_executable,
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
        if repair_gate.get("incident_id"):
            result["repair_incident_id"] = repair_gate["incident_id"]
        if managed and not closed:
            result["managed_by_supervisor"] = True
            result["managed_desktop_pid"] = managed_pid
        if idle_since and not closed:
            result["idle_since"] = idle_since
        write_state(config_path, config, result)
        return result

    owner_thread_id = str(
        config.get("browser_owner_thread_id", "")
    ).strip()
    if not owner_thread_id:
        raise ValueError("browser_owner_thread_id is required")
    cwd = resolve_path(
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
    timeout_seconds = int(
        config.get("desktop_launch_timeout_seconds", 30)
    )
    processes = desktop_processes(process_executable)
    launched = False
    if not processes:
        try:
            processes = launch_desktop(
                executable=executable,
                cwd=cwd,
                process_executable=process_executable,
                timeout_seconds=timeout_seconds,
            )
            launched = True
        except (OSError, subprocess.SubprocessError, AppServerError) as error:
            alert_interval = int(
                config.get(
                    "desktop_failure_alert_interval_seconds",
                    1800,
                )
            )
            if alert_interval < 0:
                raise ValueError(
                    "desktop failure alert interval must not be negative"
                )
            last_alert_at = autopilot_dispatch.parse_time(
                str(previous.get("last_alert_at", ""))
            )
            should_alert = (
                last_alert_at is None
                or (
                    autopilot_dispatch.utc_now() - last_alert_at
                ).total_seconds()
                >= alert_interval
            )
            alert_sent = (
                notify_desktop_failure(
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
            if repair_gate.get("incident_id"):
                result["repair_incident_id"] = repair_gate["incident_id"]
            if alert_sent:
                result["last_alert_at"] = autopilot_dispatch.isoformat()
            elif previous.get("last_alert_at"):
                result["last_alert_at"] = previous["last_alert_at"]
            write_state(config_path, config, result)
            return result

    waiting_statuses = {
        "desktop_launched_waiting_relay",
        "desktop_ready_waiting_relay",
    }
    status = (
        "desktop_launched_waiting_relay"
        if launched
        else "desktop_ready_waiting_relay"
    )
    preserve_waiting_since = (
        previous.get("status") in waiting_statuses
        and previous.get("work_kind") == work_kind
        and previous.get("waiting_since")
    )
    result = {
        "status": status,
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
            else autopilot_dispatch.isoformat()
        ),
        "work_kind": work_kind,
    }
    if repair_gate.get("incident_id"):
        result["repair_incident_id"] = repair_gate["incident_id"]
    if launched:
        result["managed_by_supervisor"] = True
        result["managed_desktop_pid"] = processes[0]
    elif (
        previous.get("managed_by_supervisor") is True
        and previous.get("managed_desktop_pid") in processes
    ):
        result["managed_by_supervisor"] = True
        result["managed_desktop_pid"] = previous["managed_desktop_pid"]
    write_state(config_path, config, result)
    return result


def dispatch(
    config_path: Path,
    *,
    lease_seconds: int,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    config_path = config_path.expanduser().resolve()
    config = autopilot_dispatch.read_json(config_path)
    relay_mode = str(
        config.get("desktop_relay_mode", "external_app_server")
    ).strip()
    if relay_mode == "in_app_heartbeat":
        return desktop_supervisor_dispatch(
            config_path,
            config,
            lease_seconds=lease_seconds,
        )
    if relay_mode != "external_app_server":
        raise ValueError(
            "desktop_relay_mode must be in_app_heartbeat or "
            "external_app_server"
        )
    gate = autopilot_bridge.gate(
        config_path,
        lease_seconds=lease_seconds,
    )
    if not gate.get("dispatch"):
        result = {
            "status": str(gate.get("status", "idle")),
            "dispatched": False,
            "event_ids": list(gate.get("event_ids", [])),
            "pending_count": int(gate.get("pending_count", 0)),
        }
        write_state(config_path, config, result)
        return result

    owner_thread_id = str(
        config.get("browser_owner_thread_id", "")
    ).strip()
    relay_thread_id = str(
        config.get("app_server_relay_thread_id", "")
    ).strip()
    if not owner_thread_id:
        raise ValueError("browser_owner_thread_id is required")
    if not relay_thread_id:
        raise ValueError("app_server_relay_thread_id is required")
    root = config_path.parent.resolve()
    cwd = resolve_path(
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
    relay_runtime_signature = runtime_signature(executable)
    previous_state_path = dispatch_state_path(config_path, config)
    previous_state = (
        autopilot_dispatch.read_json(previous_state_path)
        if previous_state_path.exists()
        else {}
    )
    if (
        previous_state.get("status") == "blocked"
        and previous_state.get("blocker_code")
        == "cross_thread_tool_unavailable"
        and previous_state.get("relay_runtime_signature")
        == relay_runtime_signature
    ):
        result = {
            "status": "relay_blocked",
            "dispatched": False,
            "event_ids": list(gate.get("event_ids", [])),
            "pending_count": int(gate.get("pending_count", 0)),
            "blocker_code": "cross_thread_tool_unavailable",
            "relay_runtime_signature": relay_runtime_signature,
        }
        write_state(config_path, config, result)
        return result
    timeout = int(
        timeout_seconds
        if timeout_seconds is not None
        else config.get("app_server_dispatch_timeout_seconds", 7200)
    )
    if timeout <= 0:
        raise ValueError("app-server dispatch timeout must be positive")

    client = AppServerClient(
        executable=executable,
        cwd=cwd,
        timeout_seconds=timeout,
    )
    deadline = time.monotonic() + timeout
    try:
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
            raise AppServerError("unarchived the wrong relay thread")
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
            raise AppServerError("resumed the wrong relay thread")
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
                            "text": RELAY_PROMPT.format(
                                owner_thread_id=owner_thread_id,
                                owner_prompt=OWNER_PROMPT.format(
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
            raise AppServerError("turn/start returned no turn id")
        turn_id = str(turn["id"])
        completed = client.wait_turn(
            thread_id=relay_thread_id,
            turn_id=turn_id,
            deadline=deadline,
        )
        status = str(completed.get("status", "failed"))
        client.send(
            {
                "method": "thread/archive",
                "id": 4,
                "params": {"threadId": relay_thread_id},
            }
        )
        client.wait_response(4, deadline=deadline)
        if status != "completed":
            raise AppServerError(
                f"relay turn finished with status {status}"
            )
        if not relay_handoff_confirmed(completed):
            raise RelayCapabilityUnavailable(
                "relay completed without a confirmed owner handoff"
            )
        client.close()
        event_ids = list(gate.get("event_ids", []))
        wait_owner_resolution(
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
        write_state(config_path, config, result)
        return result
    except RelayCapabilityUnavailable as error:
        write_state(
            config_path,
            config,
            {
                "status": "blocked",
                "dispatched": True,
                "event_ids": list(gate.get("event_ids", [])),
                "pending_count": int(gate.get("pending_count", 0)),
                "blocker_code": "cross_thread_tool_unavailable",
                "relay_runtime_signature": relay_runtime_signature,
                "error": str(error),
            },
        )
        raise
    except Exception as error:
        write_state(
            config_path,
            config,
            {
                "status": "failed",
                "dispatched": True,
                "event_ids": list(gate.get("event_ids", [])),
                "pending_count": int(gate.get("pending_count", 0)),
                "error": str(error),
            },
        )
        raise
    finally:
        client.close()


def run_once(
    config_path: Path,
    *,
    lease_seconds: int,
    timeout_seconds: int | None = None,
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
        return dispatch(
            config_path,
            lease_seconds=lease_seconds,
            timeout_seconds=timeout_seconds,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lease-seconds", type=int, default=1800)
    parser.add_argument("--timeout-seconds", type=int)
    arguments = parser.parse_args()
    try:
        result = run_once(
            arguments.config,
            lease_seconds=arguments.lease_seconds,
            timeout_seconds=arguments.timeout_seconds,
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
