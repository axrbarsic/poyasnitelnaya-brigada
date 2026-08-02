#!/usr/bin/env python3
"""Mutating initial-audit lifecycle operations."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from scripts import (
    watcher_audit_state,
    watcher_history,
    watcher_resolution,
    watcher_time,
)


isoformat = watcher_time.isoformat
parse_time = watcher_time.parse_time
utc_now = watcher_time.utc_now


@dataclass(frozen=True)
class Dependencies:
    get_meta: Callable[..., str | None]
    set_meta: Callable[..., None]
    delete_meta: Callable[..., None]
    evaluate_health: Callable[..., dict[str, Any]]
    refresh_wake_file: Callable[..., dict[str, Any]]
    write_health: Callable[..., dict[str, Any]]
    is_eligible_reply: Callable[..., bool]
    queued_events: Callable[..., list[dict[str, Any]]]


def _initial_audit_status(
    connection: sqlite3.Connection,
    dependencies: Dependencies,
) -> dict[str, Any]:
    return watcher_audit_state.initial_audit_status(
        connection,
        dependencies=watcher_audit_state.Dependencies(
            get_meta=dependencies.get_meta,
        ),
    )

def expire_initial_audit_events(
    config: Any,
    connection: sqlite3.Connection,
    *,
    response_window_hours: float,
    now: datetime | None = None,
    dry_run: bool = False,
    dependencies: Dependencies,
) -> dict[str, Any]:
    if connection.in_transaction:
        raise ValueError("Expiry requires a top-level database transaction")
    window_hours = float(response_window_hours)
    if not math.isfinite(window_hours) or window_hours <= 0:
        raise ValueError("Response window hours must be a positive number")
    hours_text = (
        str(int(window_hours))
        if window_hours.is_integer()
        else format(window_hours, ".6g")
    )
    expiry_reason = (
        f"Outside Alex's requested {hours_text}-hour start window; "
        "no Browser review or publication attempted."
    )
    current = now or utc_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Expiry clock must include a timezone")
    current_utc = current.astimezone(timezone.utc)
    cutoff = current_utc - timedelta(
        hours=window_hours
    )
    cutoff_text = isoformat(cutoff)
    resolved_at = isoformat(current_utc)
    counts = {
        "examined": 0,
        "within_window": 0,
        "future_timestamp": 0,
        "invalid_timestamp": 0,
        "invalid_stored_event": 0,
        "expired": 0,
    }
    blocked_candidates: list[dict[str, str]] = []
    expired_sample: list[str] = []
    transaction_started = False
    try:
        connection.execute("BEGIN IMMEDIATE" if not dry_run else "BEGIN")
        transaction_started = True
        configured_user_id = dependencies.get_meta(connection, "configured_user_id")
        if configured_user_id is None:
            raise ValueError("Initial audit has not started")
        if configured_user_id != config.user_id:
            raise ValueError(
                "Initial audit user does not match configured user"
            )
        if dependencies.get_meta(connection, "initial_audit_started_at") is None:
            raise ValueError("Initial audit has not started")
        if dependencies.get_meta(connection, "initial_audit_cycle_started_at") is None:
            raise ValueError(
                "Response cycle has not started; "
                "run initial-audit-start first"
            )
        applied_as_of = dependencies.get_meta(
            connection,
            "initial_audit_cycle_expiry_as_of",
        )
        applied_hours = dependencies.get_meta(
            connection,
            "initial_audit_cycle_expiry_hours",
        )
        if (
            applied_as_of is not None
            and (
                applied_as_of != resolved_at
                or applied_hours != hours_text
            )
        ):
            raise ValueError(
                "This response cycle already has a different fixed cutoff; "
                "run initial-audit-start only when Alex starts a new cycle"
            )
        cursor = connection.execute(
            """
            SELECT e.*
            FROM events e
            WHERE e.is_reply = 1
              AND e.in_reply_to_user_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM event_resolutions r
                  WHERE r.event_id = e.event_id
              )
            ORDER BY e.created_at, CAST(e.event_id AS INTEGER)
            """,
            (config.user_id,),
        )
        while True:
            rows = cursor.fetchmany(100)
            if not rows:
                break
            for event in rows:
                counts["examined"] += 1
                event_id = str(event["event_id"])
                try:
                    created_at = parse_time(event["created_at"])
                except ValueError:
                    created_at = None
                if (
                    created_at is None
                    or created_at.tzinfo is None
                    or created_at.utcoffset() is None
                ):
                    counts["invalid_timestamp"] += 1
                    if len(blocked_candidates) < 20:
                        blocked_candidates.append(
                            {
                                "event_id": event_id,
                                "reason": "invalid_or_naive_created_at",
                            }
                        )
                    continue
                created_at_utc = created_at.astimezone(timezone.utc)
                if created_at_utc > current_utc:
                    counts["future_timestamp"] += 1
                    continue
                if created_at_utc >= cutoff:
                    counts["within_window"] += 1
                    continue
                savepoint = "expire_event"
                connection.execute(f"SAVEPOINT {savepoint}")
                try:
                    snapshot = watcher_audit_state._expiry_history_snapshot(connection, event)
                    watcher_history.import_history_snapshot(
                        connection,
                        snapshot,
                        within_transaction=True,
                    )
                    evidence = json.dumps(
                        [
                            "policy:requested_start_window_no_browser_review",
                            f"response_window_hours:{hours_text}",
                            f"created_at:{isoformat(created_at_utc)}",
                            f"cutoff:{cutoff_text}",
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    connection.execute(
                        """
                        INSERT INTO event_resolutions(
                            event_id, disposition, reason, reply_url, stance,
                            stance_detail, confidence, media_meaning,
                            evidence_json, resolved_at
                        ) VALUES(?, 'skip', ?, NULL, 'ambiguous', ?, 'high',
                                 NULL, ?, ?)
                        """,
                        (
                            event_id,
                            expiry_reason,
                            "outside_requested_start_window_not_classified",
                            evidence,
                            resolved_at,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE events
                        SET delivery_state = 'acknowledged'
                        WHERE event_id = ?
                        """,
                        (event_id,),
                    )
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                except (KeyError, ValueError, json.JSONDecodeError) as error:
                    connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                    counts["invalid_stored_event"] += 1
                    if len(blocked_candidates) < 20:
                        blocked_candidates.append(
                            {
                                "event_id": event_id,
                                "reason": str(error),
                            }
                        )
                    continue
                counts["expired"] += 1
                if len(expired_sample) < 20:
                    expired_sample.append(event_id)
        if dry_run:
            connection.rollback()
        else:
            dependencies.set_meta(
                connection,
                "initial_audit_response_window_hours",
                hours_text,
            )
            dependencies.set_meta(
                connection,
                "initial_audit_last_expire_cutoff",
                cutoff_text,
            )
            dependencies.set_meta(
                connection,
                "initial_audit_last_expire_at",
                resolved_at,
            )
            dependencies.set_meta(
                connection,
                "initial_audit_cycle_expiry_as_of",
                resolved_at,
            )
            dependencies.set_meta(
                connection,
                "initial_audit_cycle_expiry_hours",
                hours_text,
            )
            connection.commit()
        transaction_started = False
    except Exception:
        if transaction_started:
            connection.rollback()
        raise
    status = _initial_audit_status(connection, dependencies)
    health_status = dependencies.evaluate_health(config)["status"]
    if not dry_run:
        dependencies.refresh_wake_file(config, connection)
        health_status = dependencies.write_health(
            config,
            connection,
            last_new_count=0,
        )["status"]
    return {
        "status": "dry_run" if dry_run else "applied",
        "response_window_hours": window_hours,
        "cutoff": cutoff_text,
        **counts,
        "blocked_candidates": blocked_candidates,
        "expired_sample": expired_sample,
        "pending_events": status["pending_events"],
        "history_complete": status["history_complete"],
        "invariant_ok": status["invariant_ok"],
        "health": health_status,
    }


def start_initial_audit(
    config: Any,
    connection: sqlite3.Connection,
    *,
    dependencies: Dependencies,
) -> dict[str, Any]:
    cycle_started_at = isoformat()
    started_at = (
        dependencies.get_meta(connection, "initial_audit_started_at")
        or cycle_started_at
    )
    with connection:
        dependencies.set_meta(connection, "configured_user_id", config.user_id)
        dependencies.set_meta(connection, "initial_audit_started_at", started_at)
        dependencies.set_meta(
            connection,
            "initial_audit_cycle_started_at",
            cycle_started_at,
        )
        dependencies.delete_meta(connection, "initial_audit_cycle_expiry_as_of")
        dependencies.delete_meta(connection, "initial_audit_cycle_expiry_hours")
        dependencies.delete_meta(connection, "initial_audit_completed_at")
        cursor = connection.execute(
            """
            UPDATE events
            SET delivery_state = 'queued'
            WHERE is_reply = 1
              AND in_reply_to_user_id = ?
              AND COALESCE(author_id, '') != ?
              AND delivery_state != 'queued'
              AND NOT EXISTS (
                  SELECT 1 FROM event_resolutions r
                  WHERE r.event_id = events.event_id
              )
            """,
            (config.user_id, config.user_id),
        )
    wake = dependencies.refresh_wake_file(config, connection)
    health = dependencies.write_health(config, connection, last_new_count=0)
    return {
        "requeued": int(cursor.rowcount),
        **_initial_audit_status(connection, dependencies),
        "queue": wake["pending_count"],
        "health": health["status"],
    }


def requeue_unanswered_skips(
    config: Any,
    connection: sqlite3.Connection,
    *,
    response_window_hours: float,
    now: datetime,
    dry_run: bool,
    dependencies: Dependencies,
) -> dict[str, Any]:
    if not config.mandatory_response_mode:
        raise ValueError(
            "mandatory_response_mode must be enabled before policy requeue"
        )
    if response_window_hours <= 0 or not math.isfinite(response_window_hours):
        raise ValueError("Response window hours must be a positive number")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Policy requeue --as-of must be timezone-aware")
    current = now.astimezone(timezone.utc)
    cutoff = current - timedelta(hours=response_window_hours)
    candidates: list[sqlite3.Row] = []
    rows = connection.execute(
        """
        SELECT e.*, r.*
        FROM events e
        LEFT JOIN event_resolutions r ON r.event_id = e.event_id
        WHERE e.delivery_state != 'queued'
          AND (
              r.disposition = 'skip'
              OR (
                  r.event_id IS NULL
                  AND e.delivery_state = 'ignored'
                  AND e.is_reply = 1
              )
          )
        ORDER BY e.created_at, CAST(e.event_id AS INTEGER)
        """
    ).fetchall()
    for row in rows:
        if row["author_id"] == config.user_id:
            continue
        created_at = parse_time(row["created_at"])
        if (
            created_at is None
            or created_at.tzinfo is None
            or created_at.utcoffset() is None
        ):
            continue
        created_at_utc = created_at.astimezone(timezone.utc)
        if created_at_utc < cutoff or created_at_utc > current:
            continue
        is_ignored_mention_reply = (
            row["disposition"] is None
            and row["delivery_state"] == "ignored"
            and bool(row["is_reply"])
        )
        if not is_ignored_mention_reply:
            if not dependencies.is_eligible_reply(
                connection,
                config,
                author_id=row["author_id"],
                is_reply=bool(row["is_reply"]),
                in_reply_to_user_id=row["in_reply_to_user_id"],
                conversation_id=row["conversation_id"],
            ):
                continue
        if watcher_resolution._matching_alex_reply_turns(connection, str(row["event_id"])):
            continue
        candidates.append(row)

    requeued_at = isoformat(current)
    if not dry_run and candidates:
        with connection:
            dependencies.delete_meta(connection, "initial_audit_completed_at")
            for row in candidates:
                event_id = str(row["event_id"])
                previous_resolution = (
                    watcher_resolution._resolution_row_payload(row)
                    if row["disposition"] is not None
                    else {
                        "delivery_state": "ignored",
                        "disposition": None,
                    }
                )
                connection.execute(
                    """
                    INSERT INTO response_policy_requeues(
                        event_id, previous_resolution_json, reason, requeued_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        json.dumps(
                            previous_resolution,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        (
                            "mandatory_response_ignored_mention_reconciliation"
                            if row["disposition"] is None
                            else "mandatory_response_policy_migration"
                        ),
                        requeued_at,
                    ),
                )
                connection.execute(
                    """
                    UPDATE events
                    SET delivery_state = 'queued'
                    WHERE event_id = ?
                    """,
                    (event_id,),
                )
    wake = (
        dependencies.refresh_wake_file(config, connection)
        if not dry_run
        else {"pending_count": len(dependencies.queued_events(connection))}
    )
    if not dry_run:
        dependencies.write_health(config, connection, last_new_count=0)
    return {
        "dry_run": dry_run,
        "as_of": isoformat(current),
        "cutoff": isoformat(cutoff),
        "hours": response_window_hours,
        "candidate_count": len(candidates),
        "candidate_event_ids": [
            str(row["event_id"]) for row in candidates
        ],
        "pending_count": wake["pending_count"],
    }


def requeue_recovered_pro_model_blockers(
    config: Any,
    connection: sqlite3.Connection,
    *,
    dry_run: bool,
    dependencies: Dependencies,
) -> dict[str, Any]:
    if not config.mandatory_response_mode:
        raise ValueError(
            "mandatory_response_mode must be enabled before Pro recovery"
        )
    candidates = connection.execute(
        """
        SELECT e.*, r.*
        FROM events e
        JOIN event_resolutions r ON r.event_id = e.event_id
        WHERE r.disposition = 'blocked'
          AND r.blocker_code IN (
              'required_pro_model_unavailable',
              'missing_historical_pro_conversation',
              'target_screenshot_unavailable'
          )
          AND e.delivery_state != 'queued'
          AND COALESCE(e.author_id, '') != ?
          AND NOT EXISTS (
              SELECT 1
              FROM conversation_turns alex
              WHERE alex.parent_status_id = e.event_id
                AND alex.actor = 'alex'
          )
        ORDER BY e.created_at, CAST(e.event_id AS INTEGER)
        """,
        (config.user_id,),
    ).fetchall()
    requeued_at = isoformat()
    if not dry_run and candidates:
        with connection:
            dependencies.delete_meta(connection, "initial_audit_completed_at")
            for row in candidates:
                event_id = str(row["event_id"])
                connection.execute(
                    """
                    INSERT INTO response_policy_requeues(
                        event_id, previous_resolution_json, reason,
                        requeued_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        json.dumps(
                            watcher_resolution._resolution_row_payload(row),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        "local_poyasnitelnaya_brigada_skill_recovery",
                        requeued_at,
                    ),
                )
                connection.execute(
                    """
                    UPDATE events
                    SET delivery_state = 'queued'
                    WHERE event_id = ?
                    """,
                    (event_id,),
                )
    wake = (
        dependencies.refresh_wake_file(config, connection)
        if not dry_run
        else {"pending_count": len(dependencies.queued_events(connection))}
    )
    if not dry_run:
        dependencies.write_health(config, connection, last_new_count=0)
    return {
        "dry_run": dry_run,
        "candidate_count": len(candidates),
        "candidate_event_ids": [
            str(row["event_id"]) for row in candidates
        ],
        "pending_count": wake["pending_count"],
        "reason": "local_poyasnitelnaya_brigada_skill_recovery",
    }


def complete_initial_audit(
    config: Any,
    connection: sqlite3.Connection,
    *,
    dependencies: Dependencies,
) -> dict[str, Any]:
    unresolved = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM events e
            WHERE e.is_reply = 1
              AND e.in_reply_to_user_id = ?
              AND COALESCE(e.author_id, '') != ?
              AND (
                  NOT EXISTS (
                      SELECT 1 FROM event_resolutions r
                      WHERE r.event_id = e.event_id
                  )
                  OR (
                      e.delivery_state = 'queued'
                      AND EXISTS (
                          SELECT 1 FROM event_resolutions r
                          WHERE r.event_id = e.event_id
                            AND r.disposition = 'skip'
                      )
                  )
              )
            """,
            (config.user_id, config.user_id),
        ).fetchone()["count"]
    )
    if unresolved:
        raise ValueError(
            "Initial audit cannot complete with "
            f"{unresolved} unresolved direct events"
        )
    history_missing = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM event_resolutions r
            JOIN events e ON e.event_id = r.event_id
            LEFT JOIN conversation_turns t ON t.status_id = r.event_id
            WHERE e.is_reply = 1
              AND e.in_reply_to_user_id = ?
              AND COALESCE(e.author_id, '') != ?
              AND NOT (
                  e.delivery_state = 'queued'
                  AND r.disposition = 'skip'
              )
              AND t.status_id IS NULL
            """,
            (config.user_id, config.user_id),
        ).fetchone()["count"]
    )
    if history_missing:
        raise ValueError(
            "Initial audit cannot complete with "
            f"{history_missing} resolved events missing exact history turns"
        )
    published_alex_history_missing = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM event_resolutions r
            JOIN events e ON e.event_id = r.event_id
            LEFT JOIN conversation_turns alex
              ON alex.status_id = substr(
                  r.reply_url,
                  length('https://x.com/axrbarsic/status/') + 1
              )
             AND alex.actor = 'alex'
             AND alex.parent_status_id = r.event_id
             AND alex.url = r.reply_url
            WHERE e.is_reply = 1
              AND e.in_reply_to_user_id = ?
              AND COALESCE(e.author_id, '') != ?
              AND r.disposition = 'published'
              AND (
                  r.reply_url NOT LIKE
                      'https://x.com/axrbarsic/status/%'
                  OR alex.status_id IS NULL
              )
            """,
            (config.user_id, config.user_id),
        ).fetchone()["count"]
    )
    if published_alex_history_missing:
        raise ValueError(
            "Initial audit cannot complete with "
            f"{published_alex_history_missing} published replies missing "
            "matching exact Alex history turns"
        )
    _, blocked_missing_code = watcher_audit_state._blocked_resolution_contract_status(
        connection,
        config.user_id,
    )
    if blocked_missing_code:
        raise ValueError(
            "Initial audit cannot complete with "
            f"{blocked_missing_code} blocked resolutions missing a valid "
            "terminal blocker_code"
        )
    completed_at = isoformat()
    with connection:
        dependencies.set_meta(connection, "configured_user_id", config.user_id)
        if dependencies.get_meta(connection, "initial_audit_started_at") is None:
            dependencies.set_meta(connection, "initial_audit_started_at", completed_at)
        dependencies.set_meta(connection, "initial_audit_completed_at", completed_at)
    dependencies.refresh_wake_file(config, connection)
    dependencies.write_health(config, connection, last_new_count=0)
    return _initial_audit_status(connection, dependencies)
