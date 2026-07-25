#!/usr/bin/env python3
"""Token-free X mention polling, durable queueing, and health monitoring."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 1
TOKEN_ENV_NAMES = ("X_BEARER_TOKEN", "X_API_BEARER_TOKEN", "TWITTER_BEARER_TOKEN")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Config:
    source_path: Path
    user_id: str
    database: Path
    lock_file: Path
    health_file: Path
    wake_file: Path
    alert_file: Path
    poll_interval_seconds: int
    stale_after_seconds: int
    failure_threshold: int
    silence_review_seconds: int
    watchdog_interval_seconds: int
    request_timeout_seconds: int
    max_pages_per_poll: int
    api_base: str
    keychain_helper: Path
    keychain_service: str
    keychain_account: str
    queue_direct_replies_only: bool
    notifications_enabled: bool


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def load_config(path: Path) -> Config:
    raw = read_json(path)
    base = path.resolve().parent
    config = Config(
        source_path=path.resolve(),
        user_id=str(raw.get("user_id", "")).strip(),
        database=_resolve(base, raw.get("database", "var/watcher.sqlite3")),
        lock_file=_resolve(base, raw.get("lock_file", "var/poll.lock")),
        health_file=_resolve(base, raw.get("health_file", "var/health.json")),
        wake_file=_resolve(base, raw.get("wake_file", "var/wake-request.json")),
        alert_file=_resolve(base, raw.get("alert_file", "var/watchdog-alert.json")),
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 300)),
        stale_after_seconds=int(raw.get("stale_after_seconds", 900)),
        failure_threshold=int(raw.get("failure_threshold", 2)),
        silence_review_seconds=int(raw.get("silence_review_seconds", 21600)),
        watchdog_interval_seconds=int(raw.get("watchdog_interval_seconds", 60)),
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 30)),
        max_pages_per_poll=int(raw.get("max_pages_per_poll", 20)),
        api_base=str(raw.get("api_base", "https://api.x.com/2")).rstrip("/"),
        keychain_helper=_resolve(base, raw.get("keychain_helper", "var/keychain-helper")),
        keychain_service=str(raw.get("keychain_service", "")).strip(),
        keychain_account=str(raw.get("keychain_account", "")).strip(),
        queue_direct_replies_only=bool(raw.get("queue_direct_replies_only", True)),
        notifications_enabled=bool(raw.get("notifications_enabled", True)),
    )
    positive_values = {
        "poll_interval_seconds": config.poll_interval_seconds,
        "stale_after_seconds": config.stale_after_seconds,
        "failure_threshold": config.failure_threshold,
        "silence_review_seconds": config.silence_review_seconds,
        "watchdog_interval_seconds": config.watchdog_interval_seconds,
        "request_timeout_seconds": config.request_timeout_seconds,
        "max_pages_per_poll": config.max_pages_per_poll,
    }
    invalid = [name for name, value in positive_values.items() if value <= 0]
    if invalid:
        raise ValueError("Config values must be positive: " + ", ".join(invalid))
    return config


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            author_id TEXT,
            username TEXT,
            created_at TEXT,
            conversation_id TEXT,
            in_reply_to_user_id TEXT,
            is_reply INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'queued'
        );

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS poll_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            new_count INTEGER NOT NULL,
            error_class TEXT,
            error_message TEXT
        );
        """
    )
    connection.commit()
    return connection


class AlreadyRunningError(RuntimeError):
    """Raised when another poller process owns the lock."""


@contextlib.contextmanager
def exclusive_process_lock(path: Path) -> Iterable[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AlreadyRunningError("Another poller process is already running") from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def get_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """
        INSERT INTO meta(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def delete_meta(connection: sqlite3.Connection, key: str) -> None:
    connection.execute("DELETE FROM meta WHERE key = ?", (key,))


def numeric_max(values: Iterable[str | None]) -> str | None:
    candidates = [value for value in values if value and value.isdigit()]
    return max(candidates, key=int) if candidates else None


def queued_events(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT event_id, author_id, username, created_at, conversation_id,
               in_reply_to_user_id, is_reply, first_seen_at
        FROM events
        WHERE delivery_state = 'queued'
        ORDER BY CAST(event_id AS INTEGER)
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        username = row["username"]
        event_id = str(row["event_id"])
        event_url = (
            f"https://x.com/{username}/status/{event_id}"
            if username
            else f"https://x.com/i/web/status/{event_id}"
        )
        result.append(
            {
                "event_id": event_id,
                "event_url": event_url,
                "author_id": row["author_id"],
                "username": username,
                "created_at": row["created_at"],
                "conversation_id": row["conversation_id"],
                "in_reply_to_user_id": row["in_reply_to_user_id"],
                "is_reply": bool(row["is_reply"]),
                "first_seen_at": row["first_seen_at"],
            }
        )
    return result


def refresh_wake_file(config: Config, connection: sqlite3.Connection) -> dict[str, Any]:
    events = queued_events(connection)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": isoformat(),
        "pending_count": len(events),
        "events": events,
    }
    atomic_write_json(config.wake_file, payload)
    return payload


def ingest_response(
    config: Config,
    connection: sqlite3.Connection,
    response: dict[str, Any],
    *,
    source: str,
    started_at: str | None = None,
) -> dict[str, Any]:
    started = started_at or isoformat()
    observed_at = isoformat()
    users = {
        str(user.get("id")): str(user.get("username"))
        for user in response.get("includes", {}).get("users", [])
        if user.get("id") and user.get("username")
    }
    new_ids: list[str] = []
    observed_ids: list[str] = []
    seen_ids: list[str] = []
    live_bootstrap = (
        source == "x_api"
        and get_meta(connection, "first_success_at") is None
        and get_meta(connection, "since_id") is None
    )

    with connection:
        for event in response.get("data", []) or []:
            event_id = str(event.get("id", "")).strip()
            if not event_id:
                continue
            seen_ids.append(event_id)
            referenced = event.get("referenced_tweets") or []
            is_reply = any(item.get("type") == "replied_to" for item in referenced)
            author_id = str(event.get("author_id", "")).strip() or None
            in_reply_to_user_id = (
                str(event.get("in_reply_to_user_id", "")).strip() or None
            )
            direct_reply = is_reply and in_reply_to_user_id == config.user_id
            if live_bootstrap:
                delivery_state = "baseline"
            elif config.queue_direct_replies_only and not direct_reply:
                delivery_state = "ignored"
            else:
                delivery_state = "queued"
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO events(
                    event_id, author_id, username, created_at, conversation_id,
                    in_reply_to_user_id, is_reply, payload_json, first_seen_at,
                    delivery_state
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    author_id,
                    users.get(author_id or ""),
                    event.get("created_at"),
                    event.get("conversation_id"),
                    in_reply_to_user_id,
                    1 if is_reply else 0,
                    json.dumps(event, ensure_ascii=False, sort_keys=True),
                    observed_at,
                    delivery_state,
                ),
            )
            if cursor.rowcount == 1:
                observed_ids.append(event_id)
                if delivery_state == "queued":
                    new_ids.append(event_id)

        previous_cursor = get_meta(connection, "since_id")
        newest_id = numeric_max(
            [
                previous_cursor,
                response.get("meta", {}).get("newest_id"),
                *seen_ids,
            ]
        )
        if newest_id:
            set_meta(connection, "since_id", newest_id)
        if get_meta(connection, "first_success_at") is None:
            set_meta(connection, "first_success_at", observed_at)
        set_meta(connection, "last_success_at", observed_at)
        set_meta(connection, "consecutive_failures", "0")
        delete_meta(connection, "last_error_class")
        delete_meta(connection, "last_error_message")
        if new_ids:
            set_meta(connection, "last_new_event_at", observed_at)
        connection.execute(
            """
            INSERT INTO poll_runs(
                started_at, completed_at, source, status, new_count
            ) VALUES(?, ?, ?, 'success', ?)
            """,
            (started, observed_at, source, len(new_ids)),
        )

    health = write_health(config, connection, last_new_count=len(new_ids))
    wake = refresh_wake_file(config, connection)
    if new_ids:
        notify_macos(
            config,
            "Новые ответы в X",
            f"В очереди {wake['pending_count']}, новых {len(new_ids)}.",
        )
    return {
        "source": source,
        "bootstrap": live_bootstrap,
        "observed_count": len(observed_ids),
        "new_count": len(new_ids),
        "new_event_ids": sorted(new_ids, key=int),
        "since_id": get_meta(connection, "since_id"),
        "pending_count": wake["pending_count"],
        "health": health["status"],
    }


def baseline_existing_queue(
    config: Config,
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    with connection:
        cursor = connection.execute(
            "UPDATE events SET delivery_state = 'baseline' "
            "WHERE delivery_state = 'queued'"
        )
    wake = refresh_wake_file(config, connection)
    health = write_health(config, connection, last_new_count=0)
    return {
        "baselined_count": int(cursor.rowcount),
        "pending_count": wake["pending_count"],
        "since_id": get_meta(connection, "since_id"),
        "health": health["status"],
    }


def record_failure(
    config: Config,
    connection: sqlite3.Connection,
    error: Exception,
    *,
    source: str,
    started_at: str,
) -> dict[str, Any]:
    completed_at = isoformat()
    previous_failures = int(get_meta(connection, "consecutive_failures") or "0")
    failures = previous_failures + 1
    safe_message = safe_error_message(error)
    with connection:
        set_meta(connection, "consecutive_failures", str(failures))
        set_meta(connection, "last_error_class", type(error).__name__)
        set_meta(connection, "last_error_message", safe_message)
        connection.execute(
            """
            INSERT INTO poll_runs(
                started_at, completed_at, source, status, new_count,
                error_class, error_message
            ) VALUES(?, ?, ?, 'failure', 0, ?, ?)
            """,
            (started_at, completed_at, source, type(error).__name__, safe_message),
        )
    return write_health(config, connection, last_new_count=0)


def safe_error_message(error: Exception) -> str:
    message = str(error)
    message = re.sub(r"(?i)bearer\s+[^\s\"']+", "Bearer [REDACTED]", message)
    for name in TOKEN_ENV_NAMES:
        token = os.environ.get(name)
        if token:
            message = message.replace(token, "[REDACTED]")
    return message[:500]


def write_health(
    config: Config,
    connection: sqlite3.Connection,
    *,
    last_new_count: int,
) -> dict[str, Any]:
    failures = int(get_meta(connection, "consecutive_failures") or "0")
    status = "healthy" if failures == 0 else "degraded"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "updated_at": isoformat(),
        "first_success_at": get_meta(connection, "first_success_at"),
        "last_success_at": get_meta(connection, "last_success_at"),
        "last_new_event_at": get_meta(connection, "last_new_event_at"),
        "last_new_count": last_new_count,
        "last_seen_id": get_meta(connection, "since_id"),
        "consecutive_failures": failures,
        "last_error_class": get_meta(connection, "last_error_class"),
        "last_error_message": get_meta(connection, "last_error_message"),
        "queued_events": len(queued_events(connection)),
        "poll_interval_seconds": config.poll_interval_seconds,
    }
    atomic_write_json(config.health_file, payload)
    return payload


def bearer_token_with_source(config: Config) -> tuple[str, str]:
    for name in TOKEN_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            return value, f"environment:{name}"
    if sys.platform == "darwin" and config.keychain_service and config.keychain_account:
        try:
            if config.keychain_helper.is_file() and os.access(
                config.keychain_helper, os.X_OK
            ):
                command = [
                    str(config.keychain_helper),
                    "get",
                    config.keychain_service,
                    config.keychain_account,
                ]
                source = "keychain_helper"
            else:
                command = [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    config.keychain_service,
                    "-a",
                    config.keychain_account,
                    "-w",
                ]
                source = "keychain"
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("Unable to read X API bearer token from Keychain") from error
        value = result.stdout.strip()
        if value:
            return value, source
    raise RuntimeError(
        "Missing X API bearer token. Set an environment variable or configured Keychain item."
    )


def bearer_token(config: Config) -> str:
    return bearer_token_with_source(config)[0]


def preflight(config: Config) -> dict[str, Any]:
    user_id_valid = bool(config.user_id and config.user_id.isdigit())
    try:
        _, source = bearer_token_with_source(config)
        token_available = True
    except RuntimeError:
        source = None
        token_available = False
    return {
        "config": str(config.source_path),
        "user_id_configured": user_id_valid,
        "token_available": token_available,
        "token_source": source,
        "database_parent_writable": os.access(config.database.parent, os.W_OK)
        if config.database.parent.exists()
        else os.access(config.database.parent.parent, os.W_OK),
        "ready_for_live_poll": user_id_valid and token_available,
    }


def notify_macos(config: Config, title: str, body: str) -> None:
    if not config.notifications_enabled or sys.platform != "darwin":
        return
    try:
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                "on run argv",
                "-e",
                "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e",
                "end run",
                title,
                body,
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def request_json(url: str, token: str, timeout_seconds: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "axrbarsic-x-mention-watcher/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read(1000).decode("utf-8", errors="replace")
        detail = ""
        try:
            payload = json.loads(body)
            detail = str(payload.get("title") or payload.get("detail") or "")
        except (TypeError, ValueError):
            detail = ""
        suffix = f" ({detail[:200]})" if detail else ""
        raise RuntimeError(f"X API HTTP {error.code}{suffix}") from error


def poll_live(
    config: Config,
    connection: sqlite3.Connection,
    *,
    fetch: Callable[[str, str, int], dict[str, Any]] = request_json,
) -> dict[str, Any]:
    started_at = isoformat()
    try:
        if (
            not config.user_id
            or config.user_id == "REPLACE_WITH_X_USER_ID"
            or not config.user_id.isdigit()
        ):
            raise RuntimeError("config user_id is not configured as a numeric X user ID")
        token = bearer_token(config)
        since_id = get_meta(connection, "since_id")
        all_data: list[dict[str, Any]] = []
        all_users: dict[str, dict[str, Any]] = {}
        newest_ids: list[str | None] = [since_id]
        pagination_token: str | None = None
        seen_pagination_tokens: set[str] = set()
        page_count = 0

        while True:
            page_count += 1
            if page_count > config.max_pages_per_poll:
                raise RuntimeError("X API pagination exceeded configured page limit")
            params: dict[str, str] = {
                "max_results": "100",
                "tweet.fields": (
                    "author_id,created_at,conversation_id,in_reply_to_user_id,"
                    "referenced_tweets"
                ),
                "expansions": "author_id",
                "user.fields": "username",
            }
            if since_id:
                params["since_id"] = since_id
            if pagination_token:
                params["pagination_token"] = pagination_token
            url = (
                f"{config.api_base}/users/{urllib.parse.quote(config.user_id)}/mentions?"
                f"{urllib.parse.urlencode(params)}"
            )
            page = fetch(url, token, config.request_timeout_seconds)
            if not isinstance(page, dict):
                raise RuntimeError("X API returned a non-object JSON response")
            if page.get("errors"):
                raise RuntimeError(
                    "X API returned errors: "
                    + json.dumps(page["errors"], ensure_ascii=False)[:1000]
                )
            all_data.extend(page.get("data", []) or [])
            for user in page.get("includes", {}).get("users", []) or []:
                if user.get("id"):
                    all_users[str(user["id"])] = user
            meta = page.get("meta", {}) or {}
            newest_ids.append(meta.get("newest_id"))
            pagination_token = meta.get("next_token")
            if not pagination_token:
                break
            if pagination_token in seen_pagination_tokens:
                raise RuntimeError("X API returned a repeated pagination token")
            seen_pagination_tokens.add(pagination_token)

        merged = {
            "data": all_data,
            "includes": {"users": list(all_users.values())},
            "meta": {"newest_id": numeric_max(newest_ids)},
        }
        return ingest_response(
            config,
            connection,
            merged,
            source="x_api",
            started_at=started_at,
        )
    except Exception as error:
        record_failure(
            config,
            connection,
            error,
            source="x_api",
            started_at=started_at,
        )
        raise


def acknowledge_events(
    config: Config,
    connection: sqlite3.Connection,
    event_ids: list[str],
) -> dict[str, Any]:
    requested = list(dict.fromkeys(event_ids))
    placeholders = ",".join("?" for _ in requested)
    rows = connection.execute(
        f"""
        SELECT event_id
        FROM events
        WHERE delivery_state = 'queued' AND event_id IN ({placeholders})
        """,
        requested,
    ).fetchall()
    acknowledged = sorted((str(row["event_id"]) for row in rows), key=int)
    rejected = sorted(set(requested) - set(acknowledged), key=int)
    with connection:
        connection.executemany(
            "UPDATE events SET delivery_state = 'acknowledged' WHERE event_id = ?",
            [(event_id,) for event_id in acknowledged],
        )
    wake = refresh_wake_file(config, connection)
    health = write_health(config, connection, last_new_count=0)
    return {
        "acknowledged": acknowledged,
        "not_queued": rejected,
        "pending_count": wake["pending_count"],
        "health": health["status"],
    }


def evaluate_health(config: Config, now: datetime | None = None) -> dict[str, Any]:
    current = now or utc_now()
    if not config.health_file.exists():
        return {
            "status": "never_started",
            "healthy": False,
            "reason": "health_file_missing",
        }
    health = read_json(config.health_file)
    first_success = parse_time(health.get("first_success_at"))
    last_success = parse_time(health.get("last_success_at"))
    last_new = parse_time(health.get("last_new_event_at"))
    failures = int(health.get("consecutive_failures") or 0)
    last_error_class = health.get("last_error_class")
    last_error_message = health.get("last_error_message")

    if str(last_error_message or "").startswith("X API HTTP 402"):
        status = "billing_blocked"
        reason = "x_api_credits_depleted"
        healthy = False
    elif last_success is None:
        status = "never_succeeded"
        reason = "last_success_missing"
        healthy = False
    else:
        success_age = (current - last_success).total_seconds()
        if success_age > config.stale_after_seconds:
            status = "stale"
            reason = "last_success_too_old"
            healthy = False
        elif failures >= config.failure_threshold:
            status = "failing"
            reason = "failure_threshold_reached"
            healthy = False
        elif (
            (last_new or first_success) is not None
            and (current - (last_new or first_success)).total_seconds()
            > config.silence_review_seconds
        ):
            status = "healthy_silence_review"
            reason = "no_new_events_review_due"
            healthy = True
        else:
            status = "healthy"
            reason = "polling_current"
            healthy = True

    return {
        "status": status,
        "healthy": healthy,
        "reason": reason,
        "checked_at": isoformat(current),
        "first_success_at": health.get("first_success_at"),
        "last_success_at": health.get("last_success_at"),
        "last_new_event_at": health.get("last_new_event_at"),
        "consecutive_failures": failures,
        "queued_events": int(health.get("queued_events") or 0),
        "last_error_class": last_error_class,
        "last_error_message": last_error_message,
    }


def run_watchdog(config: Config, *, loop: bool) -> int:
    previous_status: str | None = None
    if config.alert_file.exists():
        try:
            previous_status = str(read_json(config.alert_file).get("status") or "") or None
        except (OSError, ValueError):
            previous_status = None
    while True:
        result = evaluate_health(config)
        if result["status"] != previous_status:
            atomic_write_json(config.alert_file, result)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
            if result["status"] == "healthy":
                notify_macos(config, "X watcher восстановлен", "Проверка X снова работает.")
            else:
                notify_macos(
                    config,
                    "Требуется проверка X watcher",
                    f"Состояние: {result['status']}. Причина: {result['reason']}.",
                )
            previous_status = str(result["status"])
        if not loop:
            return 0 if result["healthy"] else 2
        time.sleep(config.watchdog_interval_seconds)


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="Path to watcher JSON config.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    poll = commands.add_parser("poll", help="Poll X API once or ingest a fixture.")
    poll.add_argument("--fixture", type=Path)
    poll.add_argument("--quiet", action="store_true")

    commands.add_parser("watch", help="Poll continuously at the configured interval.")
    commands.add_parser("preflight", help="Check config and token availability safely.")
    commands.add_parser("status", help="Print health and pending queue.")
    commands.add_parser(
        "baseline",
        help="Mark the current queued backlog as the initial observed baseline.",
    )

    acknowledge = commands.add_parser("ack", help="Acknowledge queued event IDs.")
    acknowledge.add_argument("event_ids", nargs="+")

    watchdog = commands.add_parser("watchdog", help="Check watcher health.")
    watchdog.add_argument("--loop", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)

    if args.command == "preflight":
        result = preflight(config)
        print_json(result)
        return 0 if result["ready_for_live_poll"] else 2

    connection = connect_database(config.database)
    try:
        if args.command == "poll":
            try:
                with exclusive_process_lock(config.lock_file):
                    if args.fixture:
                        payload = read_json(args.fixture)
                        result = ingest_response(
                            config,
                            connection,
                            payload,
                            source=f"fixture:{args.fixture.name}",
                        )
                    else:
                        result = poll_live(config, connection)
            except AlreadyRunningError:
                result = {"status": "already_running"}
            except Exception as error:
                print_json(
                    {
                        "status": "poll_failed",
                        "error_class": type(error).__name__,
                    }
                )
                return 1
            if not args.quiet:
                print_json(result)
            return 0

        if args.command == "watch":
            try:
                with exclusive_process_lock(config.lock_file):
                    while True:
                        try:
                            print_json(poll_live(config, connection))
                        except Exception as error:
                            print(
                                json.dumps(
                                    {
                                        "status": "poll_failed",
                                        "error_class": type(error).__name__,
                                    },
                                    sort_keys=True,
                                ),
                                file=sys.stderr,
                                flush=True,
                            )
                        time.sleep(config.poll_interval_seconds)
            except AlreadyRunningError:
                print_json({"status": "already_running"})
                return 0

        if args.command == "status":
            print_json(
                {
                    "health": evaluate_health(config),
                    "queue": refresh_wake_file(config, connection),
                }
            )
            return 0

        if args.command == "baseline":
            print_json(baseline_existing_queue(config, connection))
            return 0

        if args.command == "ack":
            print_json(acknowledge_events(config, connection, args.event_ids))
            return 0

        if args.command == "watchdog":
            return run_watchdog(config, loop=args.loop)

        raise AssertionError(f"Unhandled command: {args.command}")
    except KeyboardInterrupt:
        return 130
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
