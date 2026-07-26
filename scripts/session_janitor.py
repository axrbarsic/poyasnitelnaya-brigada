#!/usr/bin/env python3
"""Archive completed X automation threads without creating Codex tasks."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

try:
    from scripts import autopilot_dispatch
except ModuleNotFoundError:
    import autopilot_dispatch  # type: ignore[no-redef]


AUTOMATION_TITLE = "X: автопилот ответов"
ARCHIVABLE_STATUSES = {"idle", "notLoaded", "systemError"}
ACTIVE_STATUS = "active"
HELPER_COMMAND_MARKERS = (
    "/cua_node/bin/node_repl",
    "node ./mcp/server.mjs",
    "uv run --project . --frozen python scripts/mcp_server.py",
)
PROCESS_PATTERN = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+"
    r"([A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d+\s+"
    r"\d{2}:\d{2}:\d{2}\s+\d{4})\s+(.*)$"
)


def send(stream: TextIO, request_id: int, method: str, params: dict[str, Any]) -> None:
    stream.write(
        json.dumps(
            {"id": request_id, "method": method, "params": params},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )
    stream.flush()


def receive(stream: TextIO, request_id: int) -> dict[str, Any]:
    for line in stream:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") != request_id:
            continue
        if "error" in payload:
            raise RuntimeError(
                f"app-server request {request_id} failed: {payload['error']}"
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(
                f"app-server request {request_id} returned no object result"
            )
        return result
    raise RuntimeError(f"app-server closed before response {request_id}")


def owner_snapshot(config_path: Path) -> tuple[Path, dict[str, Any] | None]:
    _, state_path = autopilot_dispatch.load_paths(config_path)
    state = autopilot_dispatch.load_state(state_path)
    owner = state.get("owner")
    return state_path, owner if isinstance(owner, dict) else None


def status_type(thread: dict[str, Any]) -> str | None:
    status = thread.get("status")
    return status.get("type") if isinstance(status, dict) else None


def matches_automation(thread: dict[str, Any]) -> bool:
    return (
        thread.get("name") == AUTOMATION_TITLE
        and str(thread.get("preview", "")).startswith(
            f"Automation: {AUTOMATION_TITLE}\nAutomation ID: x\n"
        )
    )


def owner_age_seconds(
    owner: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> float | None:
    if owner is None:
        return None
    claimed_at = autopilot_dispatch.parse_time(owner.get("claimed_at"))
    if claimed_at is None:
        return None
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - claimed_at).total_seconds())


def owning_automation_thread(
    threads: list[dict[str, Any]],
    owner: dict[str, Any],
) -> dict[str, Any] | None:
    claimed_at = autopilot_dispatch.parse_time(owner.get("claimed_at"))
    if claimed_at is None:
        return None
    claimed_epoch = int(claimed_at.timestamp())
    candidates = [
        thread
        for thread in threads
        if matches_automation(thread)
        and isinstance(thread.get("createdAt"), int)
        and isinstance(thread.get("updatedAt"), int)
        and int(thread["createdAt"]) <= claimed_epoch
        and int(thread["updatedAt"]) >= claimed_epoch
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda thread: int(thread["createdAt"]))


def owning_thread_is_live(
    thread: dict[str, Any] | None,
    *,
    now_epoch: int,
    freshness_seconds: int,
) -> bool:
    if thread is None:
        return False
    if status_type(thread) == ACTIVE_STATUS:
        return True
    updated_at = thread.get("updatedAt")
    return (
        isinstance(updated_at, int)
        and now_epoch - updated_at < freshness_seconds
    )


def recover_owner(
    state_path: Path,
    owner: dict[str, Any],
    *,
    owner_age: float | None,
    apply: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    claim_token = owner.get("claim_token")
    if not isinstance(claim_token, str) or not claim_token:
        return None, None
    candidate = {
        "claim_token": claim_token,
        "event_ids": [
            str(value) for value in owner.get("event_ids", [])
        ],
        "owner_age_seconds": (
            round(owner_age, 1) if owner_age is not None else None
        ),
    }
    if not apply:
        return candidate, None
    released = autopilot_dispatch.release(state_path, claim_token)
    if not released.get("released"):
        return candidate, None
    return candidate, {**candidate, "event_ids": released["released"]}


def parse_processes(output: str) -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = PROCESS_PATTERN.match(line)
        if match is None:
            continue
        started = datetime.strptime(
            match.group(3),
            "%a %b %d %H:%M:%S %Y",
        )
        processes.append(
            {
                "pid": int(match.group(1)),
                "ppid": int(match.group(2)),
                "started_at": int(started.timestamp()),
                "command": match.group(4),
            }
        )
    return processes


def helper_process_candidates(
    processes: list[dict[str, Any]],
    threads: list[dict[str, Any]],
    *,
    now_epoch: int,
    grace_seconds: int,
    protected_ids: set[str],
) -> list[int]:
    task_start_times = {
        int(thread["createdAt"])
        for thread in threads
        if str(thread.get("id", "")) not in protected_ids
        and matches_automation(thread)
        and status_type(thread) != ACTIVE_STATUS
        and isinstance(thread.get("createdAt"), int)
        and isinstance(thread.get("updatedAt"), int)
        and now_epoch - int(thread["updatedAt"]) >= grace_seconds
    }
    selected_roots = {
        int(process["pid"])
        for process in processes
        if any(
            marker in str(process.get("command", ""))
            for marker in HELPER_COMMAND_MARKERS
        )
        and any(
            abs(int(process.get("started_at", 0)) - task_start) <= 2
            for task_start in task_start_times
        )
    }
    selected = set(selected_roots)
    while True:
        descendants = {
            int(process["pid"])
            for process in processes
            if int(process.get("ppid", -1)) in selected
        }
        if descendants.issubset(selected):
            break
        selected.update(descendants)
    return sorted(selected)


def collect_processes() -> list[dict[str, Any]]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,lstart=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_processes(result.stdout)


def reap_helpers(
    pids: list[int],
    *,
    apply: bool,
) -> tuple[list[int], list[int]]:
    if not apply:
        return [], []
    terminated: list[int] = []
    for pid in reversed(pids):
        try:
            os.kill(pid, signal.SIGTERM)
            terminated.append(pid)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue
    if terminated:
        time.sleep(0.25)
    survivors: list[int] = []
    for pid in terminated:
        try:
            os.kill(pid, 0)
            survivors.append(pid)
        except ProcessLookupError:
            continue
        except PermissionError:
            survivors.append(pid)
    return sorted(terminated), sorted(survivors)


def eligible_threads(
    threads: list[dict[str, Any]],
    *,
    now: int,
    minimum_age_seconds: int,
    protected_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    protected = protected_ids or set()
    eligible: list[dict[str, Any]] = []
    for thread in threads:
        updated_at = thread.get("updatedAt")
        if (
            str(thread.get("id", "")) in protected
            or not matches_automation(thread)
            or status_type(thread) not in ARCHIVABLE_STATUSES
            or not isinstance(updated_at, int)
            or now - updated_at < minimum_age_seconds
        ):
            continue
        eligible.append(thread)
    return eligible


def run_janitor(
    config_path: Path,
    *,
    apply: bool,
    minimum_age_seconds: int,
    orphan_owner_seconds: int,
    page_limit: int,
) -> dict[str, Any]:
    config = autopilot_dispatch.read_json(config_path)
    state_path, owner = owner_snapshot(config_path)
    age_seconds = owner_age_seconds(owner)
    cli_path = str(config.get("codex_cli_path", "codex"))
    process = subprocess.Popen(
        [cli_path, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise RuntimeError("failed to open app-server stdio")
    archived: list[str] = []
    candidates: list[str] = []
    matched: list[dict[str, Any]] = []
    all_threads: list[dict[str, Any]] = []
    recovered_owner: dict[str, Any] | None = None
    recovery_candidate: dict[str, Any] | None = None
    helper_candidates: list[int] = []
    helpers_terminated: list[int] = []
    helper_survivors: list[int] = []
    helper_error: str | None = None
    try:
        send(
            process.stdin,
            1,
            "initialize",
            {
                "clientInfo": {
                    "name": "x-session-janitor",
                    "version": "1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        receive(process.stdout, 1)
        cursor: str | None = None
        request_id = 2
        while True:
            send(
                process.stdin,
                request_id,
                "thread/list",
                {
                    "archived": False,
                    "cursor": cursor,
                    "limit": page_limit,
                    "searchTerm": AUTOMATION_TITLE,
                    "sortDirection": "desc",
                    "sortKey": "updated_at",
                    "sourceKinds": ["vscode"],
                    "useStateDbOnly": True,
                },
            )
            result = receive(process.stdout, request_id)
            request_id += 1
            data = result.get("data", [])
            if not isinstance(data, list):
                raise RuntimeError("thread/list data is not an array")
            all_threads.extend(
                item for item in data if isinstance(item, dict)
            )
            matched.extend(
                {
                    "id": str(item.get("id", "")),
                    "name": item.get("name"),
                    "status": status_type(item),
                    "thread_source": item.get("threadSource"),
                    "source": item.get("source"),
                    "preview": str(item.get("preview", ""))[:120],
                    "created_at": item.get("createdAt"),
                    "updated_at": item.get("updatedAt"),
                }
                for item in data
                if isinstance(item, dict)
                and item.get("name") == AUTOMATION_TITLE
            )
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor

        owning_thread = (
            owning_automation_thread(all_threads, owner)
            if owner is not None
            else None
        )
        protected_ids = {
            str(value)
            for value in config.get(
                "session_janitor_protected_thread_ids", []
            )
        }
        if owning_thread is not None:
            protected_ids.add(str(owning_thread.get("id", "")))

        if bool(config.get("session_janitor_reap_helpers", True)):
            try:
                helper_candidates = helper_process_candidates(
                    collect_processes(),
                    all_threads,
                    now_epoch=int(time.time()),
                    grace_seconds=int(
                        config.get(
                            "session_janitor_helper_grace_seconds",
                            120,
                        )
                    ),
                    protected_ids=protected_ids,
                )
                helpers_terminated, helper_survivors = reap_helpers(
                    helper_candidates,
                    apply=apply,
                )
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                helper_error = str(error)

        eligible = eligible_threads(
            all_threads,
            now=int(time.time()),
            minimum_age_seconds=minimum_age_seconds,
            protected_ids=protected_ids,
        )
        for thread in eligible:
            thread_id = str(thread["id"])
            candidates.append(thread_id)
            if not apply:
                continue
            send(
                process.stdin,
                request_id,
                "thread/archive",
                {"threadId": thread_id},
            )
            receive(process.stdout, request_id)
            request_id += 1
            archived.append(thread_id)

        if owner is not None:
            if owning_thread_is_live(
                owning_thread,
                now_epoch=int(time.time()),
                freshness_seconds=orphan_owner_seconds,
            ):
                return {
                    "status": "owner_busy",
                    "owner_age_seconds": (
                        round(age_seconds, 1)
                        if age_seconds is not None
                        else None
                    ),
                    "active_thread_ids": [
                        str(owning_thread.get("id", ""))
                    ],
                    "owner_thread_status": status_type(owning_thread),
                    "owner_thread_updated_at": owning_thread.get(
                        "updatedAt"
                    ),
                    "helper_candidates": helper_candidates,
                    "helpers_terminated": helpers_terminated,
                    "helper_survivors": helper_survivors,
                    "helper_error": helper_error,
                    "archived": archived,
                    "candidates": candidates,
                }
            if age_seconds is None or age_seconds < orphan_owner_seconds:
                return {
                    "status": "owner_busy",
                    "owner_age_seconds": (
                        round(age_seconds, 1)
                        if age_seconds is not None
                        else None
                    ),
                    "active_thread_ids": [],
                    "owner_thread_status": (
                        status_type(owning_thread)
                        if owning_thread is not None
                        else None
                    ),
                    "owner_thread_updated_at": (
                        owning_thread.get("updatedAt")
                        if owning_thread is not None
                        else None
                    ),
                    "helper_candidates": helper_candidates,
                    "helpers_terminated": helpers_terminated,
                    "helper_survivors": helper_survivors,
                    "helper_error": helper_error,
                    "archived": archived,
                    "candidates": candidates,
                }
            recovery_candidate, recovered_owner = recover_owner(
                state_path,
                owner,
                owner_age=age_seconds,
                apply=apply,
            )

        return {
            "status": "completed",
            "apply": apply,
            "helper_candidates": helper_candidates,
            "helpers_terminated": helpers_terminated,
            "helper_survivors": helper_survivors,
            "helper_error": helper_error,
            "recovery_candidate": recovery_candidate,
            "recovered_owner": recovered_owner,
            "matched": matched,
            "candidates": candidates,
            "archived": archived,
        }
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--minimum-age-seconds", type=int, default=900)
    parser.add_argument("--orphan-owner-seconds", type=int)
    parser.add_argument("--page-limit", type=int, default=100)
    arguments = parser.parse_args()
    if arguments.minimum_age_seconds < 60:
        parser.error("--minimum-age-seconds must be at least 60")
    if not 1 <= arguments.page_limit <= 1000:
        parser.error("--page-limit must be between 1 and 1000")

    config = autopilot_dispatch.read_json(arguments.config)
    orphan_owner_seconds = (
        arguments.orphan_owner_seconds
        if arguments.orphan_owner_seconds is not None
        else int(config.get("session_janitor_orphan_owner_seconds", 300))
    )
    if orphan_owner_seconds < 60:
        parser.error("--orphan-owner-seconds must be at least 60")
    lock_path = autopilot_dispatch.resolve_path(
        arguments.config,
        str(config.get("session_janitor_lock_file", "var/session-janitor.lock")),
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(lock_path.open("a+", encoding="utf-8")) as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                json.dumps(
                    {"status": "already_running", "archived": []},
                    sort_keys=True,
                )
            )
            return 0
        result = run_janitor(
            arguments.config.resolve(),
            apply=arguments.apply,
            minimum_age_seconds=arguments.minimum_age_seconds,
            orphan_owner_seconds=orphan_owner_seconds,
            page_limit=arguments.page_limit,
        )
    result["checked_at"] = autopilot_dispatch.isoformat()
    state_path = autopilot_dispatch.resolve_path(
        arguments.config,
        str(
            config.get(
                "session_janitor_state_file",
                "var/session-janitor-health.json",
            )
        ),
    )
    autopilot_dispatch.atomic_write_json(state_path, result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
