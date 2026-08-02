#!/usr/bin/env python3
"""X API polling, conversation-tail budgets, and queue acknowledgements."""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from scripts import (
    watcher_constants,
    watcher_events,
    watcher_http,
    watcher_time,
)


@dataclass(frozen=True)
class Dependencies:
    bearer_token: Callable[[Any], str]
    ingest_response: Callable[..., dict[str, Any]]
    record_failure: Callable[..., dict[str, Any]]
    refresh_wake_file: Callable[[Any, sqlite3.Connection], dict[str, Any]]
    write_health: Callable[..., dict[str, Any]]


def active_conversation_ids(
    config: Any,
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> list[str]:
    current = now or watcher_time.utc_now()
    cutoff = watcher_time.isoformat(
        current - timedelta(hours=config.conversation_tail_watch_hours)
    )
    rows = connection.execute(
        """
        SELECT chain_id, MAX(COALESCE(posted_at, observed_at)) AS latest_alex_at
        FROM conversation_turns
        WHERE actor = 'alex'
          AND COALESCE(posted_at, observed_at) >= ?
        GROUP BY chain_id
        ORDER BY latest_alex_at DESC, chain_id DESC
        LIMIT ?
        """,
        (cutoff, config.conversation_tail_max_conversations),
    ).fetchall()
    return [str(row["chain_id"]) for row in rows]


def conversation_tail_query_chunks(
    conversation_ids: Iterable[str],
    *,
    account_handle: str,
    max_query_length: int = 512,
) -> list[str]:
    suffix = " is:reply"
    handle = account_handle.strip().lstrip("@")
    if handle:
        suffix += f" -from:{handle}"
    chunks: list[str] = []
    current: list[str] = []
    for conversation_id in conversation_ids:
        if not str(conversation_id).isdigit():
            raise ValueError("Conversation tail contains a non-numeric ID")
        clause = f"conversation_id:{conversation_id}"
        candidate = f"({' OR '.join([*current, clause])}){suffix}"
        if current and len(candidate) > max_query_length:
            chunks.append(f"({' OR '.join(current)}){suffix}")
            current = [clause]
        else:
            current.append(clause)
    if current:
        chunks.append(f"({' OR '.join(current)}){suffix}")
    return chunks


def conversation_tail_budget_status(
    config: Any,
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or watcher_time.utc_now()
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    row = connection.execute(
        """
        SELECT
            COALESCE(SUM(returned_count), 0) AS returned_count,
            COALESCE(SUM(request_count), 0) AS request_count
        FROM poll_runs
        WHERE source = ?
          AND status IN ('success', 'partial_budget_exhausted')
          AND completed_at >= ?
          AND completed_at < ?
        """,
        (
            watcher_constants.CONVERSATION_TAIL_SOURCE,
            watcher_time.isoformat(day_start),
            watcher_time.isoformat(day_end),
        ),
    ).fetchone()
    used = int(row["returned_count"] or 0)
    limit = config.conversation_tail_daily_post_read_limit
    remaining = max(0, limit - used)
    return {
        "utc_day": day_start.date().isoformat(),
        "daily_limit": limit,
        "used": used,
        "remaining": remaining,
        "request_count": int(row["request_count"] or 0),
        "allowed": remaining >= 10,
    }


def load_conversation_tail_scan_state(
    connection: sqlite3.Connection,
) -> dict[str, Any] | None:
    raw = watcher_events.get_meta(
        connection,
        watcher_constants.CONVERSATION_TAIL_SCAN_STATE_KEY,
    )
    if raw is None:
        return None
    try:
        state = json.loads(raw)
        queries = state["queries"]
        query_index = int(state["query_index"])
        if (
            not isinstance(queries, list)
            or not queries
            or not all(isinstance(query, str) and query for query in queries)
            or query_index < 0
            or query_index > len(queries)
        ):
            raise ValueError
        watcher_time.parse_time(str(state["scan_started_at"]))
        watcher_time.parse_time(str(state["start_time"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("Invalid durable conversation tail scan state") from error
    return state


def store_conversation_tail_scan_state(
    connection: sqlite3.Connection,
    state: dict[str, Any] | None,
) -> None:
    key = watcher_constants.CONVERSATION_TAIL_SCAN_STATE_KEY
    if state is None:
        watcher_events.delete_meta(connection, key)
        return
    watcher_events.set_meta(
        connection,
        key,
        json.dumps(state, ensure_ascii=False, sort_keys=True),
    )


def poll_conversation_tails(
    config: Any,
    connection: sqlite3.Connection,
    *,
    token: str,
    fetch: Callable[[str, str, int], dict[str, Any]] = watcher_http.request_json,
    now: datetime | None = None,
    dependencies: Dependencies,
) -> dict[str, Any]:
    current = now or watcher_time.utc_now()
    scan_state = load_conversation_tail_scan_state(connection)
    last_success = watcher_time.parse_time(
        watcher_events.get_meta(connection, "conversation_tail_last_success_at")
    )
    last_attempt = watcher_time.parse_time(
        watcher_events.get_meta(
            connection,
            watcher_constants.CONVERSATION_TAIL_LAST_ATTEMPT_KEY,
        )
    ) or last_success
    if last_attempt is not None:
        age = (current - last_attempt).total_seconds()
        if age < config.conversation_tail_poll_interval_seconds:
            return {
                "source": watcher_constants.CONVERSATION_TAIL_SOURCE,
                "status": "not_due",
                "new_count": 0,
                "new_event_ids": [],
            }

    budget = conversation_tail_budget_status(config, connection, now=current)
    if not budget["allowed"]:
        with connection:
            watcher_events.set_meta(
                connection,
                watcher_constants.CONVERSATION_TAIL_LAST_ATTEMPT_KEY,
                watcher_time.isoformat(current),
            )
        return {
            "source": watcher_constants.CONVERSATION_TAIL_SOURCE,
            "status": "budget_exhausted",
            "budget": budget,
            "new_count": 0,
            "new_event_ids": [],
        }

    if scan_state is None:
        conversation_ids = active_conversation_ids(config, connection, now=current)
        if not conversation_ids:
            with connection:
                watcher_events.set_meta(
                    connection,
                    "conversation_tail_last_success_at",
                    watcher_time.isoformat(current),
                )
                watcher_events.set_meta(
                    connection,
                    watcher_constants.CONVERSATION_TAIL_LAST_ATTEMPT_KEY,
                    watcher_time.isoformat(current),
                )
            return {
                "source": watcher_constants.CONVERSATION_TAIL_SOURCE,
                "status": "no_active_conversations",
                "tracked_conversation_count": 0,
                "new_count": 0,
                "new_event_ids": [],
            }
        if last_success is None:
            start_time = current - timedelta(
                hours=config.conversation_tail_initial_lookback_hours
            )
        else:
            start_time = last_success - timedelta(
                seconds=config.conversation_tail_overlap_seconds
            )
        watch_cutoff = current - timedelta(hours=config.conversation_tail_watch_hours)
        start_time = max(start_time, watch_cutoff)
        queries = conversation_tail_query_chunks(
            conversation_ids,
            account_handle=config.keychain_account,
        )
        scan_state = {
            "scan_started_at": watcher_time.isoformat(current),
            "start_time": watcher_time.isoformat(start_time),
            "queries": queries,
            "query_index": 0,
            "pagination_token": None,
            "tracked_conversation_count": len(conversation_ids),
        }

    queries = list(scan_state["queries"])
    query_index = int(scan_state["query_index"])
    pagination_token = scan_state.get("pagination_token")
    run_budget = min(
        int(budget["remaining"]),
        config.conversation_tail_max_post_reads_per_poll,
    )

    all_data: list[dict[str, Any]] = []
    newest_ids: list[str | None] = []
    page_count = 0
    while query_index < len(queries):
        remaining_run_budget = run_budget - len(all_data)
        if remaining_run_budget < 10:
            break
        page_count += 1
        if page_count > config.max_pages_per_poll:
            raise RuntimeError(
                "X conversation tail pagination exceeded configured page limit"
            )
        params: dict[str, str] = {
            "query": queries[query_index],
            "start_time": str(scan_state["start_time"]),
            "max_results": str(min(100, remaining_run_budget)),
            "sort_order": "recency",
            "tweet.fields": (
                "author_id,created_at,conversation_id,in_reply_to_user_id,"
                "referenced_tweets,attachments"
            ),
        }
        if pagination_token:
            params["pagination_token"] = str(pagination_token)
        url = (
            f"{config.api_base}/tweets/search/recent?"
            f"{urllib.parse.urlencode(params)}"
        )
        page = fetch(url, token, config.request_timeout_seconds)
        if not isinstance(page, dict):
            raise RuntimeError(
                "X conversation tail returned a non-object JSON response"
            )
        if page.get("errors"):
            raise RuntimeError(
                "X conversation tail returned errors: "
                + json.dumps(page["errors"], ensure_ascii=False)[:1000]
            )
        page_data = page.get("data", []) or []
        if not isinstance(page_data, list):
            raise RuntimeError("X conversation tail data is not an array")
        if len(page_data) > remaining_run_budget:
            raise RuntimeError("X conversation tail exceeded the local read budget")
        all_data.extend(page_data)
        meta = page.get("meta", {}) or {}
        newest_ids.append(meta.get("newest_id"))
        if meta.get("next_token"):
            next_pagination_token = str(meta["next_token"])
            if pagination_token == next_pagination_token:
                raise RuntimeError(
                    "X conversation tail returned a repeated pagination token"
                )
            pagination_token = next_pagination_token
        else:
            query_index += 1
            pagination_token = None

    merged = {
        "data": all_data,
        "meta": {"newest_id": watcher_events.numeric_max(newest_ids)},
    }
    complete = query_index >= len(queries)
    scan_state["query_index"] = query_index
    scan_state["pagination_token"] = pagination_token
    result = dependencies.ingest_response(
        config,
        connection,
        merged,
        source=watcher_constants.CONVERSATION_TAIL_SOURCE,
        started_at=watcher_time.isoformat(current),
        returned_count=len(all_data),
        request_count=page_count,
        poll_status="success" if complete else "partial_budget_exhausted",
        advance_conversation_tail_cursor=complete,
        conversation_tail_cursor_at=str(scan_state["scan_started_at"]),
    )
    with connection:
        watcher_events.set_meta(
            connection,
            watcher_constants.CONVERSATION_TAIL_LAST_ATTEMPT_KEY,
            watcher_time.isoformat(current),
        )
        store_conversation_tail_scan_state(
            connection,
            None if complete else scan_state,
        )
    budget_after = {
        **budget,
        "used": int(budget["used"]) + len(all_data),
        "remaining": max(0, int(budget["remaining"]) - len(all_data)),
    }
    return {
        **result,
        "status": "success" if complete else "partial_budget_exhausted",
        "tracked_conversation_count": int(scan_state["tracked_conversation_count"]),
        "query_count": len(queries),
        "start_time": str(scan_state["start_time"]),
        "budget": budget_after,
        "scan_complete": complete,
    }


def poll_live(
    config: Any,
    connection: sqlite3.Connection,
    *,
    fetch: Callable[[str, str, int], dict[str, Any]] = watcher_http.request_json,
    dependencies: Dependencies,
) -> dict[str, Any]:
    started_at = watcher_time.isoformat()
    failure_source = "x_api"
    try:
        if (
            not config.user_id
            or config.user_id == "REPLACE_WITH_X_USER_ID"
            or not config.user_id.isdigit()
        ):
            raise RuntimeError("config user_id is not configured as a numeric X user ID")
        token = dependencies.bearer_token(config)
        since_id = watcher_events.get_meta(connection, "since_id")
        all_data: list[dict[str, Any]] = []
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
                    "referenced_tweets,attachments"
                ),
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
            "meta": {"newest_id": watcher_events.numeric_max(newest_ids)},
        }
        mention_result = dependencies.ingest_response(
            config,
            connection,
            merged,
            source="x_api",
            started_at=started_at,
            returned_count=len(all_data),
            request_count=page_count,
        )
        if not config.conversation_tail_enabled:
            return mention_result

        failure_source = "x_api_conversation_tail"
        tail_result = poll_conversation_tails(
            config,
            connection,
            token=token,
            fetch=fetch,
            dependencies=dependencies,
        )
        combined_new_ids = sorted(
            set(mention_result["new_event_ids"])
            | set(tail_result["new_event_ids"]),
            key=int,
        )
        combined_self_authored_ids = sorted(
            set(mention_result["self_authored_event_ids"])
            | set(tail_result.get("self_authored_event_ids", [])),
            key=int,
        )
        return {
            **mention_result,
            "observed_count": mention_result["observed_count"]
            + int(tail_result.get("observed_count", 0)),
            "new_count": len(combined_new_ids),
            "new_event_ids": combined_new_ids,
            "self_authored_event_ids": combined_self_authored_ids,
            "pending_count": int(
                tail_result.get("pending_count", mention_result["pending_count"])
            ),
            "health": str(tail_result.get("health", mention_result["health"])),
            "conversation_tail": tail_result,
        }
    except Exception as error:
        dependencies.record_failure(
            config,
            connection,
            error,
            source=failure_source,
            started_at=started_at,
        )
        raise


def acknowledge_events(
    config: Any,
    connection: sqlite3.Connection,
    event_ids: list[str],
    *,
    dependencies: Dependencies,
) -> dict[str, Any]:
    requested = list(dict.fromkeys(event_ids))
    placeholders = ",".join("?" for _ in requested)
    protected_rows = connection.execute(
        f"""
        SELECT event_id, author_id, is_reply, in_reply_to_user_id,
               conversation_id
        FROM events
        WHERE delivery_state = 'queued'
          AND event_id IN ({placeholders})
        """,
        requested,
    ).fetchall()
    protected_ids = sorted(
        (
            str(row["event_id"])
            for row in protected_rows
            if watcher_events.is_eligible_reply(
                connection,
                config,
                author_id=row["author_id"],
                is_reply=bool(row["is_reply"]),
                in_reply_to_user_id=row["in_reply_to_user_id"],
                conversation_id=row["conversation_id"],
            )
        ),
        key=int,
    )
    if protected_ids:
        raise ValueError(
            "Eligible reply events require a durable resolve disposition: "
            + ", ".join(protected_ids)
        )
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
    wake = dependencies.refresh_wake_file(config, connection)
    health = dependencies.write_health(config, connection, last_new_count=0)
    return {
        "acknowledged": acknowledged,
        "not_queued": rejected,
        "pending_count": wake["pending_count"],
        "health": health["status"],
    }
