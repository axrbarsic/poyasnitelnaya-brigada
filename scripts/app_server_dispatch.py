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
        app_server_desktop,
        app_server_external,
        autopilot_bridge,
        autopilot_dispatch,
        autopilot_supervisor,
        resource_guard,
    )
except ModuleNotFoundError:
    import app_server_desktop  # type: ignore[no-redef]
    import app_server_external  # type: ignore[no-redef]
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
claim как единственный Browser owner на Sol Max. После durable history и
durable resolve всех заявленных событий выполни `python3
scripts/autopilot_bridge.py --config config.json completed --claim-token
CLAIM_TOKEN`. При ошибке до durable resolve выполни `python3
scripts/autopilot_bridge.py --config config.json failed --claim-token
CLAIM_TOKEN --error WORK_ERROR`. Всегда закрой только task-owned
Browser-вкладки, не самоархивируйся и не запускай дополнительный poll."""

RELAY_PROMPT = """Ты только событийный ретранслятор X автопилота.
Не используй Browser, shell или файлы. Сначала через `tool_search` найди прямой
Codex app tool `codex_app.send_message_to_thread`, затем ровно один раз вызови
его для threadId `{owner_thread_id}`, model `gpt-5.6-sol`, thinking `max` и
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


def _external_dependencies() -> app_server_external.Dependencies:
    return app_server_external.Dependencies(
        resolve_path=resolve_path,
        runtime_signature=runtime_signature,
        dispatch_state_path=dispatch_state_path,
        read_json=autopilot_dispatch.read_json,
        write_state=write_state,
        client_factory=AppServerClient,
        relay_handoff_confirmed=relay_handoff_confirmed,
        wait_owner_resolution=wait_owner_resolution,
        monotonic=time.monotonic,
        app_server_error=AppServerError,
        relay_capability_error=RelayCapabilityUnavailable,
        relay_prompt=RELAY_PROMPT,
        owner_prompt=OWNER_PROMPT,
    )


def _idle_external_dispatch_result(
    config_path: Path,
    config: dict[str, Any],
    gate: dict[str, Any],
) -> dict[str, Any]:
    return app_server_external.idle_result(
        config_path,
        config,
        gate,
        _external_dependencies(),
    )


def _dispatch_external_relay(
    config_path: Path,
    config: dict[str, Any],
    gate: dict[str, Any],
    *,
    lease_seconds: int,
    timeout_seconds: int | None,
) -> dict[str, Any]:
    return app_server_external.dispatch(
        config_path,
        config,
        gate,
        lease_seconds=lease_seconds,
        timeout_seconds=timeout_seconds,
        dependencies=_external_dependencies(),
    )


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
        return _idle_external_dispatch_result(config_path, config, gate)
    return _dispatch_external_relay(
        config_path,
        config,
        gate,
        lease_seconds=lease_seconds,
        timeout_seconds=timeout_seconds,
    )

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
