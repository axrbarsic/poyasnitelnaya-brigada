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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

try:
    from scripts import autopilot_dispatch, json_contract, outbound_cycle
except ModuleNotFoundError:
    import autopilot_dispatch  # type: ignore[no-redef]
    import json_contract  # type: ignore[no-redef]
    import outbound_cycle  # type: ignore[no-redef]


AUTOMATION_IDENTITIES = (
    ("x", "X: автопилот ответов"),
    ("x-relay", "X: событийный relay"),
    (
        "x-15",
        "X: локальная Пояснительная бригада, автономный 15 минут",
    ),
    (
        "x-15",
        "X: Пояснительная бригада v2 каждые 10 минут",
    ),
    (
        "x-pro-15",
        "X: локальная Пояснительная бригада каждые 15 минут",
    ),
)
AUTOMATION_TITLE = AUTOMATION_IDENTITIES[0][1]
OUTBOUND_AUTOMATION_ID = "x-15"
ARCHIVABLE_STATUSES = {"idle", "notLoaded", "systemError"}
ACTIVE_STATUS = "active"
HELPER_COMMAND_MARKERS = (
    "/cua_node/bin/node_repl",
    "node ./mcp/server.mjs",
    "uv run --project . --frozen python scripts/mcp_server.py",
    "/scripts/lightpanda_mcp.py",
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
            payload = json_contract.loads(
                line,
                source="Codex app-server response",
            )
        except (json.JSONDecodeError, json_contract.DuplicateKeyError):
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


def matches_automation_identity(
    thread: dict[str, Any],
    automation_id: str,
    title: str,
) -> bool:
    name = thread.get("name")
    preview = str(thread.get("preview", ""))
    return name == title and preview.startswith(
        f"Automation: {title}\nAutomation ID: {automation_id}\n"
    )


def matches_automation(thread: dict[str, Any]) -> bool:
    return any(
        matches_automation_identity(thread, automation_id, title)
        for automation_id, title in AUTOMATION_IDENTITIES
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


def owner_lease_remaining_seconds(
    owner: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> float | None:
    if owner is None:
        return None
    lease_expires_at = autopilot_dispatch.parse_time(
        owner.get("lease_expires_at")
    )
    if lease_expires_at is None:
        return None
    current = now or datetime.now(timezone.utc)
    return (lease_expires_at - current).total_seconds()


def owning_automation_thread(
    threads: list[dict[str, Any]],
    owner: dict[str, Any],
    *,
    identities: tuple[tuple[str, str], ...] = AUTOMATION_IDENTITIES,
) -> dict[str, Any] | None:
    claimed_at = autopilot_dispatch.parse_time(owner.get("claimed_at"))
    if claimed_at is None:
        return None
    claimed_epoch = int(claimed_at.timestamp())
    candidates = [
        thread
        for thread in threads
        if any(
            matches_automation_identity(thread, automation_id, title)
            for automation_id, title in identities
        )
        and isinstance(thread.get("createdAt"), int)
        and isinstance(thread.get("updatedAt"), int)
        and int(thread["createdAt"]) <= claimed_epoch
        and int(thread["updatedAt"]) >= claimed_epoch
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda thread: int(thread["createdAt"]))


def active_outbound_owner(
    config_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    config = autopilot_dispatch.read_json(config_path)
    state_path = autopilot_dispatch.resolve_path(
        config_path,
        str(
            config.get(
                "outbound_cycle_state_file",
                "var/outbound-cycle.json",
            )
        ),
    )
    state = outbound_cycle.load_state(state_path)
    owner = state.get("owner")
    if not isinstance(owner, dict):
        return None
    current = now or datetime.now(timezone.utc)
    return owner if outbound_cycle.owner_is_active(owner, current) else None


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


def owner_recovery_allowed(
    owner: dict[str, Any],
    owning_thread: dict[str, Any] | None,
    *,
    now: datetime,
    orphan_owner_seconds: int,
) -> bool:
    now_epoch = int(now.timestamp())
    if owning_thread_is_live(
        owning_thread,
        now_epoch=now_epoch,
        freshness_seconds=orphan_owner_seconds,
    ):
        return False
    lease_remaining = owner_lease_remaining_seconds(owner, now=now)
    if lease_remaining is not None:
        return lease_remaining <= 0
    age_seconds = owner_age_seconds(owner, now=now)
    return (
        age_seconds is not None
        and age_seconds >= orphan_owner_seconds
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


@dataclass(frozen=True)
class ThreadInventory:
    all_threads: list[dict[str, Any]]
    unarchived_threads: list[dict[str, Any]]
    matched: list[dict[str, Any]]


@dataclass(frozen=True)
class OwnerThreads:
    inbound: dict[str, Any] | None
    outbound: dict[str, Any] | None
    protected_ids: set[str]


@dataclass(frozen=True)
class HelperCleanup:
    candidates: list[int]
    terminated: list[int]
    survivors: list[int]
    error: str | None

    def fields(self) -> dict[str, Any]:
        return {
            "helper_candidates": self.candidates,
            "helpers_terminated": self.terminated,
            "helper_survivors": self.survivors,
            "helper_error": self.error,
        }


@dataclass(frozen=True)
class ArchiveResult:
    candidates: list[str]
    archived: list[str]


class AppServerClient:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        if process.stdin is None or process.stdout is None:
            process.kill()
            raise RuntimeError("failed to open app-server stdio")
        self.process = process
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.request_id = 1

    @classmethod
    def start(cls, cli_path: str) -> AppServerClient:
        return cls(
            subprocess.Popen(
                [cli_path, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        )

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.request_id
        send(self.stdin, request_id, method, params)
        result = receive(self.stdout, request_id)
        self.request_id += 1
        return result

    def initialize(self) -> None:
        self.call(
            "initialize",
            {
                "clientInfo": {
                    "name": "x-session-janitor",
                    "version": "1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )

    def close(self) -> None:
        self.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def _thread_summary(thread: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(thread.get("id", "")),
        "name": thread.get("name"),
        "status": status_type(thread),
        "thread_source": thread.get("threadSource"),
        "source": thread.get("source"),
        "preview": str(thread.get("preview", ""))[:120],
        "created_at": thread.get("createdAt"),
        "updated_at": thread.get("updatedAt"),
    }


def _list_automation_threads(
    client: AppServerClient,
    *,
    page_limit: int,
    include_archived_helpers: bool,
) -> ThreadInventory:
    all_threads: list[dict[str, Any]] = []
    unarchived_threads: list[dict[str, Any]] = []
    matched: list[dict[str, Any]] = []
    seen_thread_ids: set[str] = set()
    archive_filters = (
        (False, True) if include_archived_helpers else (False,)
    )
    for archived in archive_filters:
        for _, title in AUTOMATION_IDENTITIES:
            cursor: str | None = None
            while True:
                result = client.call(
                    "thread/list",
                    {
                        "archived": archived,
                        "cursor": cursor,
                        "limit": page_limit,
                        "searchTerm": title,
                        "sortDirection": "desc",
                        "sortKey": "updated_at",
                        "sourceKinds": ["vscode"],
                        "useStateDbOnly": True,
                    },
                )
                data = result.get("data", [])
                if not isinstance(data, list):
                    raise RuntimeError("thread/list data is not an array")
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    thread_id = str(item.get("id", ""))
                    if not thread_id or thread_id in seen_thread_ids:
                        continue
                    seen_thread_ids.add(thread_id)
                    all_threads.append(item)
                    if not archived:
                        unarchived_threads.append(item)
                    if matches_automation(item):
                        matched.append(_thread_summary(item))
                next_cursor = result.get("nextCursor")
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                cursor = next_cursor
    return ThreadInventory(
        all_threads=all_threads,
        unarchived_threads=unarchived_threads,
        matched=matched,
    )


def _owner_threads(
    config: dict[str, Any],
    all_threads: list[dict[str, Any]],
    owner: dict[str, Any] | None,
    outbound_owner: dict[str, Any] | None,
) -> OwnerThreads:
    inbound_thread = (
        owning_automation_thread(all_threads, owner)
        if owner is not None
        else None
    )
    outbound_identities = tuple(
        identity
        for identity in AUTOMATION_IDENTITIES
        if identity[0] == OUTBOUND_AUTOMATION_ID
    )
    outbound_thread = (
        owning_automation_thread(
            all_threads,
            outbound_owner,
            identities=outbound_identities,
        )
        if outbound_owner is not None
        else None
    )
    protected_ids = {
        str(value)
        for value in config.get("session_janitor_protected_thread_ids", [])
    }
    for thread in (inbound_thread, outbound_thread):
        if thread is not None:
            protected_ids.add(str(thread.get("id", "")))
    return OwnerThreads(
        inbound=inbound_thread,
        outbound=outbound_thread,
        protected_ids=protected_ids,
    )


def _clean_helpers(
    config: dict[str, Any],
    all_threads: list[dict[str, Any]],
    *,
    protected_ids: set[str],
    apply: bool,
) -> HelperCleanup:
    if not bool(config.get("session_janitor_reap_helpers", True)):
        return HelperCleanup([], [], [], None)
    candidates: list[int] = []
    try:
        candidates = helper_process_candidates(
            collect_processes(),
            all_threads,
            now_epoch=int(time.time()),
            grace_seconds=int(
                config.get("session_janitor_helper_grace_seconds", 120)
            ),
            protected_ids=protected_ids,
        )
        terminated, survivors = reap_helpers(candidates, apply=apply)
        return HelperCleanup(candidates, terminated, survivors, None)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return HelperCleanup(candidates, [], [], str(error))


def _archive_eligible_threads(
    client: AppServerClient,
    all_threads: list[dict[str, Any]],
    *,
    protected_ids: set[str],
    minimum_age_seconds: int,
    apply: bool,
) -> ArchiveResult:
    threads = eligible_threads(
        all_threads,
        now=int(time.time()),
        minimum_age_seconds=minimum_age_seconds,
        protected_ids=protected_ids,
    )
    candidates = [str(thread["id"]) for thread in threads]
    archived: list[str] = []
    if apply:
        for thread_id in candidates:
            client.call("thread/archive", {"threadId": thread_id})
            archived.append(thread_id)
    return ArchiveResult(candidates=candidates, archived=archived)


def _owner_busy_result(
    owner: dict[str, Any],
    owning_thread: dict[str, Any] | None,
    *,
    apply: bool,
    age_seconds: float | None,
    lease_remaining_seconds: float | None,
    thread_is_live: bool,
    helpers: HelperCleanup,
    archive: ArchiveResult,
) -> dict[str, Any]:
    return {
        "status": "owner_busy",
        "apply": apply,
        "owner_age_seconds": (
            round(age_seconds, 1) if age_seconds is not None else None
        ),
        "owner_lease_remaining_seconds": (
            round(lease_remaining_seconds, 1)
            if lease_remaining_seconds is not None
            else None
        ),
        "owner_lease_expires_at": owner.get("lease_expires_at"),
        "owner_last_renewed_at": owner.get("last_renewed_at"),
        "active_thread_ids": (
            [str(owning_thread.get("id", ""))]
            if thread_is_live and owning_thread is not None
            else []
        ),
        "owner_thread_status": (
            status_type(owning_thread) if owning_thread is not None else None
        ),
        "owner_thread_updated_at": (
            owning_thread.get("updatedAt")
            if owning_thread is not None
            else None
        ),
        **helpers.fields(),
        "archived": archive.archived,
        "candidates": archive.candidates,
    }


def _recover_or_report_busy_owner(
    state_path: Path,
    owner: dict[str, Any] | None,
    owning_thread: dict[str, Any] | None,
    *,
    age_seconds: float | None,
    lease_remaining_seconds: float | None,
    orphan_owner_seconds: int,
    apply: bool,
    helpers: HelperCleanup,
    archive: ArchiveResult,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    if owner is None:
        return None, None, None
    current_time = datetime.now(timezone.utc)
    thread_is_live = owning_thread_is_live(
        owning_thread,
        now_epoch=int(current_time.timestamp()),
        freshness_seconds=orphan_owner_seconds,
    )
    recovery_allowed = owner_recovery_allowed(
        owner,
        owning_thread,
        now=current_time,
        orphan_owner_seconds=orphan_owner_seconds,
    )
    if thread_is_live or not recovery_allowed:
        return (
            _owner_busy_result(
                owner,
                owning_thread,
                apply=apply,
                age_seconds=age_seconds,
                lease_remaining_seconds=lease_remaining_seconds,
                thread_is_live=thread_is_live,
                helpers=helpers,
                archive=archive,
            ),
            None,
            None,
        )
    candidate, recovered = recover_owner(
        state_path,
        owner,
        owner_age=age_seconds,
        apply=apply,
    )
    return None, candidate, recovered


def _completed_result(
    *,
    apply: bool,
    outbound_owner: dict[str, Any] | None,
    outbound_thread: dict[str, Any] | None,
    helpers: HelperCleanup,
    recovery_candidate: dict[str, Any] | None,
    recovered_owner: dict[str, Any] | None,
    inventory: ThreadInventory,
    archive: ArchiveResult,
) -> dict[str, Any]:
    return {
        "status": "completed",
        "apply": apply,
        "outbound_owner_active": outbound_owner is not None,
        "outbound_owner_expires_at": (
            outbound_owner.get("expires_at")
            if outbound_owner is not None
            else None
        ),
        "outbound_thread_id": (
            str(outbound_thread.get("id", ""))
            if outbound_thread is not None
            else None
        ),
        **helpers.fields(),
        "recovery_candidate": recovery_candidate,
        "recovered_owner": recovered_owner,
        "matched": inventory.matched,
        "candidates": archive.candidates,
        "archived": archive.archived,
    }


def run_janitor(
    config_path: Path,
    *,
    apply: bool,
    minimum_age_seconds: int,
    orphan_owner_seconds: int,
    page_limit: int,
    include_archived_helpers: bool = False,
) -> dict[str, Any]:
    config = autopilot_dispatch.read_json(config_path)
    state_path, owner = owner_snapshot(config_path)
    age_seconds = owner_age_seconds(owner)
    lease_remaining_seconds = owner_lease_remaining_seconds(owner)
    outbound_owner = active_outbound_owner(config_path)
    client = AppServerClient.start(str(config.get("codex_cli_path", "codex")))
    try:
        client.initialize()
        inventory = _list_automation_threads(
            client,
            page_limit=page_limit,
            include_archived_helpers=include_archived_helpers,
        )
        owner_threads = _owner_threads(
            config,
            inventory.unarchived_threads,
            owner,
            outbound_owner,
        )
        helpers = _clean_helpers(
            config,
            inventory.all_threads,
            protected_ids=owner_threads.protected_ids,
            apply=apply,
        )
        archive = (
            ArchiveResult([], [])
            if helpers.error or helpers.survivors
            else _archive_eligible_threads(
                client,
                inventory.unarchived_threads,
                protected_ids=owner_threads.protected_ids,
                minimum_age_seconds=minimum_age_seconds,
                apply=apply,
            )
        )
        busy, recovery_candidate, recovered_owner = (
            _recover_or_report_busy_owner(
                state_path,
                owner,
                owner_threads.inbound,
                age_seconds=age_seconds,
                lease_remaining_seconds=lease_remaining_seconds,
                orphan_owner_seconds=orphan_owner_seconds,
                apply=apply,
                helpers=helpers,
                archive=archive,
            )
        )
        if busy is not None:
            return busy
        return _completed_result(
            apply=apply,
            outbound_owner=outbound_owner,
            outbound_thread=owner_threads.outbound,
            helpers=helpers,
            recovery_candidate=recovery_candidate,
            recovered_owner=recovered_owner,
            inventory=inventory,
            archive=archive,
        )
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--minimum-age-seconds", type=int, default=900)
    parser.add_argument("--orphan-owner-seconds", type=int)
    parser.add_argument("--page-limit", type=int, default=100)
    parser.add_argument("--include-archived-helpers", action="store_true")
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
            include_archived_helpers=arguments.include_archived_helpers,
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
