#!/usr/bin/env python3
"""Token-free X mention polling, durable queueing, and health monitoring."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from scripts import (
    browser_handoff_sync,
    event_dispatch,
    keychain_bundle,
    watcher_audit_lifecycle,
    watcher_audit_state,
    watcher_auth,
    watcher_cli,
    watcher_config,
    watcher_constants,
    watcher_database,
    watcher_events,
    watcher_history,
    watcher_health,
    watcher_http,
    watcher_io,
    watcher_memory,
    watcher_polling,
    watcher_resolution,
    watcher_time,
    watcher_validation,
)


SCHEMA_VERSION = watcher_constants.SCHEMA_VERSION
TOKEN_ENV_NAMES = watcher_constants.TOKEN_ENV_NAMES
CONVERSATION_TAIL_SOURCE = watcher_constants.CONVERSATION_TAIL_SOURCE
CONVERSATION_TAIL_SCAN_STATE_KEY = (
    watcher_constants.CONVERSATION_TAIL_SCAN_STATE_KEY
)
CONVERSATION_TAIL_LAST_ATTEMPT_KEY = (
    watcher_constants.CONVERSATION_TAIL_LAST_ATTEMPT_KEY
)
INITIAL_AUDIT_EXPIRY_PROVENANCE = (
    watcher_constants.INITIAL_AUDIT_EXPIRY_PROVENANCE
)
TERMINAL_BLOCKER_CODES = watcher_constants.TERMINAL_BLOCKER_CODES


utc_now = watcher_time.utc_now
isoformat = watcher_time.isoformat
parse_time = watcher_time.parse_time


atomic_write_json = watcher_io.atomic_write_json
read_json = watcher_io.read_json
Config = watcher_config.Config
load_config = watcher_config.load_config


is_eligible_reply = watcher_events.is_eligible_reply
event_mentions_configured_account = (
    watcher_events.event_mentions_configured_account
)


def connect_database(path: Path) -> sqlite3.Connection:
    return watcher_database.connect_database(path)


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


get_meta = watcher_events.get_meta
set_meta = watcher_events.set_meta
delete_meta = watcher_events.delete_meta
numeric_max = watcher_events.numeric_max
_reclassify_self_authored_events = (
    watcher_events._reclassify_self_authored_events
)


def _event_dependencies() -> watcher_events.Dependencies:
    return watcher_events.Dependencies(
        write_health=write_health,
        notify_macos=notify_macos,
    )


def reconcile_self_authored_events(
    config: Config,
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    return watcher_events.reconcile_self_authored_events(
        config,
        connection,
        dependencies=_event_dependencies(),
    )


queued_events = watcher_events.queued_events

def commenter_history_for_event(
    connection: sqlite3.Connection,
    event_id: str,
    *,
    limit: int,
) -> dict[str, Any]:
    return watcher_memory.commenter_history_for_event(
        connection,
        event_id,
        limit=limit,
    )


AUTHOR_DOSSIER_CONTRACT = watcher_memory.AUTHOR_DOSSIER_CONTRACT


def author_dossier_for_event(
    connection: sqlite3.Connection,
    event_id: str,
    *,
    limit: int,
    query_text: str | None = None,
    include_recent: bool = True,
    excluded_status_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    return watcher_memory.author_dossier_for_event(
        connection,
        event_id,
        limit=limit,
        query_text=query_text,
        include_recent=include_recent,
        excluded_status_ids=excluded_status_ids,
    )


refresh_wake_file = watcher_events.refresh_wake_file


def ingest_response(
    config: Config,
    connection: sqlite3.Connection,
    response: dict[str, Any],
    *,
    source: str,
    started_at: str | None = None,
    returned_count: int | None = None,
    request_count: int = 1,
    poll_status: str = "success",
    advance_conversation_tail_cursor: bool = True,
    conversation_tail_cursor_at: str | None = None,
) -> dict[str, Any]:
    return watcher_events.ingest_response(
        config,
        connection,
        response,
        source=source,
        started_at=started_at,
        returned_count=returned_count,
        request_count=request_count,
        poll_status=poll_status,
        advance_conversation_tail_cursor=advance_conversation_tail_cursor,
        conversation_tail_cursor_at=conversation_tail_cursor_at,
        dependencies=_event_dependencies(),
    )


def baseline_existing_queue(
    config: Config,
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    return watcher_events.baseline_existing_queue(
        config,
        connection,
        dependencies=_event_dependencies(),
    )


_blocked_resolution_contract_status = (
    watcher_audit_state._blocked_resolution_contract_status
)
_expiry_history_snapshot = watcher_audit_state._expiry_history_snapshot


def _audit_state_dependencies() -> watcher_audit_state.Dependencies:
    return watcher_audit_state.Dependencies(get_meta=get_meta)


def initial_audit_status(connection: sqlite3.Connection) -> dict[str, Any]:
    return watcher_audit_state.initial_audit_status(
        connection,
        dependencies=_audit_state_dependencies(),
    )


def initial_audit_next(
    connection: sqlite3.Connection,
    *,
    conversation_limit: int = 1,
) -> dict[str, Any]:
    return watcher_audit_state.initial_audit_next(
        connection,
        conversation_limit=conversation_limit,
        dependencies=_audit_state_dependencies(),
    )


def _audit_lifecycle_dependencies() -> watcher_audit_lifecycle.Dependencies:
    return watcher_audit_lifecycle.Dependencies(
        get_meta=get_meta,
        set_meta=set_meta,
        delete_meta=delete_meta,
        evaluate_health=evaluate_health,
        refresh_wake_file=refresh_wake_file,
        write_health=write_health,
        is_eligible_reply=is_eligible_reply,
        queued_events=queued_events,
    )


def expire_initial_audit_events(
    config: Config,
    connection: sqlite3.Connection,
    *,
    response_window_hours: float,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    return watcher_audit_lifecycle.expire_initial_audit_events(
        config,
        connection,
        response_window_hours=response_window_hours,
        now=now,
        dry_run=dry_run,
        dependencies=_audit_lifecycle_dependencies(),
    )


def start_initial_audit(
    config: Config,
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    return watcher_audit_lifecycle.start_initial_audit(
        config,
        connection,
        dependencies=_audit_lifecycle_dependencies(),
    )


def requeue_unanswered_skips(
    config: Config,
    connection: sqlite3.Connection,
    *,
    response_window_hours: float,
    now: datetime,
    dry_run: bool,
) -> dict[str, Any]:
    return watcher_audit_lifecycle.requeue_unanswered_skips(
        config,
        connection,
        response_window_hours=response_window_hours,
        now=now,
        dry_run=dry_run,
        dependencies=_audit_lifecycle_dependencies(),
    )


def requeue_recovered_pro_model_blockers(
    config: Config,
    connection: sqlite3.Connection,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    return watcher_audit_lifecycle.requeue_recovered_pro_model_blockers(
        config,
        connection,
        dry_run=dry_run,
        dependencies=_audit_lifecycle_dependencies(),
    )


def complete_initial_audit(
    config: Config,
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    return watcher_audit_lifecycle.complete_initial_audit(
        config,
        connection,
        dependencies=_audit_lifecycle_dependencies(),
    )


_require_matching_published_alex_turn = (
    watcher_resolution._require_matching_published_alex_turn
)
_matching_alex_reply_turns = watcher_resolution._matching_alex_reply_turns
_resolution_row_payload = watcher_resolution._resolution_row_payload
_enforce_mandatory_response_resolution = (
    watcher_resolution._enforce_mandatory_response_resolution
)


def _resolution_dependencies() -> watcher_resolution.Dependencies:
    return watcher_resolution.Dependencies(
        refresh_wake_file=refresh_wake_file,
        write_health=write_health,
    )


def resolve_event(
    config: Config,
    connection: sqlite3.Connection,
    event_id: str,
    *,
    disposition: str,
    reason: str,
    reply_url: str | None,
    blocker_code: str | None = None,
    stance: str | None = None,
    stance_detail: str | None = None,
    confidence: str | None = None,
    media_meaning: str | None = None,
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    return watcher_resolution.resolve_event(
        config,
        connection,
        event_id,
        disposition=disposition,
        reason=reason,
        reply_url=reply_url,
        blocker_code=blocker_code,
        stance=stance,
        stance_detail=stance_detail,
        confidence=confidence,
        media_meaning=media_meaning,
        evidence=evidence,
        dependencies=_resolution_dependencies(),
    )


def revise_event_resolution(
    config: Config,
    connection: sqlite3.Connection,
    event_id: str,
    *,
    disposition: str,
    reason: str,
    reply_url: str | None,
    revision_reason: str,
    blocker_code: str | None = None,
    stance: str | None = None,
    stance_detail: str | None = None,
    confidence: str | None = None,
    media_meaning: str | None = None,
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    return watcher_resolution.revise_event_resolution(
        config,
        connection,
        event_id,
        disposition=disposition,
        reason=reason,
        reply_url=reply_url,
        revision_reason=revision_reason,
        blocker_code=blocker_code,
        stance=stance,
        stance_detail=stance_detail,
        confidence=confidence,
        media_meaning=media_meaning,
        evidence=evidence,
        dependencies=_resolution_dependencies(),
    )


def export_audit_resolutions(
    connection: sqlite3.Connection,
    output: Path,
) -> dict[str, Any]:
    return watcher_resolution.export_audit_resolutions(
        connection,
        output,
        initial_audit_status=initial_audit_status,
    )


_required_text = watcher_validation.required_text
_required_exact_text = watcher_validation.required_exact_text
_optional_text = watcher_validation.optional_text
_validate_status_id = watcher_validation.validate_status_id
_assert_existing_values = watcher_validation.assert_existing_values
_history_records = watcher_history._history_records
import_history_snapshot = watcher_history.import_history_snapshot
import_chatgpt_conversation_migration = (
    watcher_history.import_chatgpt_conversation_migration
)
import_history_file = watcher_history.import_history_file
history_chain_for_status = watcher_history.history_chain_for_status
export_history = watcher_history.export_history
history_status = watcher_history.history_status


def sync_browser_handoffs(
    config: Config,
    connection: sqlite3.Connection,
    *,
    history_path: Path,
    ledger_path: Path,
) -> dict[str, Any]:
    dependencies = browser_handoff_sync.Dependencies(
        import_history_file=import_history_file,
        history_records=_history_records,
        validate_status_id=_validate_status_id,
        required_text=_required_text,
        optional_text=_optional_text,
        is_eligible_reply=is_eligible_reply,
        event_mentions_configured_account=event_mentions_configured_account,
        require_matching_published_alex_turn=(
            _require_matching_published_alex_turn
        ),
        matching_alex_reply_turns=_matching_alex_reply_turns,
        enforce_mandatory_response_resolution=(
            _enforce_mandatory_response_resolution
        ),
        revise_event_resolution=revise_event_resolution,
        resolve_event=resolve_event,
        initial_audit_status=initial_audit_status,
    )
    return browser_handoff_sync.sync_browser_handoffs(
        config,
        connection,
        history_path=history_path,
        ledger_path=ledger_path,
        dependencies=dependencies,
    )




def memory_audit(
    config: Config,
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    return watcher_memory.memory_audit(
        config,
        connection,
        get_meta=get_meta,
        initial_audit_status=initial_audit_status,
    )


record_failure = watcher_health.record_failure
safe_error_message = watcher_health.safe_error_message
write_health = watcher_health.write_health


def _auth_dependencies() -> watcher_auth.Dependencies:
    return watcher_auth.Dependencies(
        run=subprocess.run,
        verify_keychain_bundle=keychain_bundle.verify_bundle,
        platform=sys.platform,
    )


def bearer_token_with_source(config: Config) -> tuple[str, str]:
    return watcher_auth.bearer_token_with_source(
        config,
        dependencies=_auth_dependencies(),
    )


def bearer_token(config: Config) -> str:
    return watcher_auth.bearer_token(
        config,
        dependencies=_auth_dependencies(),
    )


def preflight(config: Config) -> dict[str, Any]:
    return watcher_auth.preflight(
        config,
        dependencies=_auth_dependencies(),
    )


def notify_macos(config: Config, title: str, body: str) -> None:
    watcher_auth.notify_macos(
        config,
        title,
        body,
        dependencies=_auth_dependencies(),
    )


request_json = watcher_http.request_json


def _polling_dependencies() -> watcher_polling.Dependencies:
    return watcher_polling.Dependencies(
        bearer_token=bearer_token,
        ingest_response=ingest_response,
        record_failure=record_failure,
        refresh_wake_file=refresh_wake_file,
        write_health=write_health,
    )


active_conversation_ids = watcher_polling.active_conversation_ids
conversation_tail_query_chunks = watcher_polling.conversation_tail_query_chunks
conversation_tail_budget_status = watcher_polling.conversation_tail_budget_status
_load_conversation_tail_scan_state = (
    watcher_polling.load_conversation_tail_scan_state
)
_store_conversation_tail_scan_state = (
    watcher_polling.store_conversation_tail_scan_state
)


def poll_conversation_tails(
    config: Config,
    connection: sqlite3.Connection,
    *,
    token: str,
    fetch: Callable[[str, str, int], dict[str, Any]] = request_json,
    now: datetime | None = None,
) -> dict[str, Any]:
    return watcher_polling.poll_conversation_tails(
        config,
        connection,
        token=token,
        fetch=fetch,
        now=now,
        dependencies=_polling_dependencies(),
    )


def poll_live(
    config: Config,
    connection: sqlite3.Connection,
    *,
    fetch: Callable[[str, str, int], dict[str, Any]] = request_json,
) -> dict[str, Any]:
    return watcher_polling.poll_live(
        config,
        connection,
        fetch=fetch,
        dependencies=_polling_dependencies(),
    )


def acknowledge_events(
    config: Config,
    connection: sqlite3.Connection,
    event_ids: list[str],
) -> dict[str, Any]:
    return watcher_polling.acknowledge_events(
        config,
        connection,
        event_ids,
        dependencies=_polling_dependencies(),
    )
evaluate_health = watcher_health.evaluate_health


def run_watchdog(config: Config, *, loop: bool) -> int:
    return watcher_health.run_watchdog(
        config,
        loop=loop,
        dependencies=watcher_health.WatchdogDependencies(
            notify_macos=notify_macos,
            sleep=time.sleep,
        ),
    )


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    return watcher_cli.build_parser(
        description=__doc__ or "",
        terminal_blocker_codes=TERMINAL_BLOCKER_CODES,
    )


def _blocked_result(error: Exception, *, status: str = "blocked") -> int:
    print_json({"status": status, "message": str(error)})
    return 2


def _handle_poll(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
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
    try:
        dispatch_result = event_dispatch.trigger_from_poll_result(
            config.source_path,
            result,
            live_poll=args.fixture is None,
        )
    except Exception as error:
        dispatch_result = {
            "status": "fallback_scheduled",
            "triggered": False,
            "event_ids": list(result.get("new_event_ids", [])),
            "error_class": type(error).__name__,
        }
    if dispatch_result["status"] != "not_needed":
        result["event_dispatch"] = dispatch_result
    if not args.quiet:
        print_json(result)
    return 0


def _handle_watch(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    del args
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


def _handle_status(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    queue = refresh_wake_file(config, connection)
    queue_output = (
        queue
        if args.full
        else {
            "schema_version": queue["schema_version"],
            "updated_at": queue["updated_at"],
            "pending_count": queue["pending_count"],
            "newest_event": queue["events"][0] if queue["events"] else None,
        }
    )
    print_json(
        {
            "health": evaluate_health(config),
            "queue": queue_output,
            "x_api_budget": {
                "conversation_tail": conversation_tail_budget_status(
                    config,
                    connection,
                )
            },
        }
    )
    return 0


def _handle_baseline(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    del args, config, connection
    print_json(
        {
            "status": "blocked",
            "message": (
                "Legacy baseline is disabled. Use initial-audit-start "
                "and resolve every queued direct reply."
            ),
        }
    )
    return 2


def _handle_initial_audit_next(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    del config
    try:
        print_json(
            initial_audit_next(
                connection,
                conversation_limit=args.conversations,
            )
        )
    except ValueError as error:
        return _blocked_result(error)
    return 0


def _timezone_aware_argument(value: str, *, label: str) -> datetime:
    parsed = parse_time(value)
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be a timezone-aware ISO-8601 value")
    return parsed


def _handle_initial_audit_expire(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    try:
        expiry_as_of = _timezone_aware_argument(
            args.as_of,
            label="Expiry --as-of",
        )
        result = expire_initial_audit_events(
            config,
            connection,
            response_window_hours=args.hours,
            now=expiry_as_of,
            dry_run=args.dry_run,
        )
        print_json(result)
    except ValueError as error:
        return _blocked_result(error)
    return 2 if result["blocked_candidates"] else 0


def _handle_mandatory_response_requeue(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    try:
        requeue_as_of = _timezone_aware_argument(
            args.as_of,
            label="Policy requeue --as-of",
        )
        print_json(
            requeue_unanswered_skips(
                config,
                connection,
                response_window_hours=args.hours,
                now=requeue_as_of,
                dry_run=args.dry_run,
            )
        )
    except ValueError as error:
        return _blocked_result(error)
    return 0


def _handle_pro_recovery_requeue(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    try:
        print_json(
            requeue_recovered_pro_model_blockers(
                config,
                connection,
                dry_run=args.dry_run,
            )
        )
    except ValueError as error:
        return _blocked_result(error)
    return 0


def _handle_browser_handoff_sync(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    try:
        print_json(
            sync_browser_handoffs(
                config,
                connection,
                history_path=args.history_file,
                ledger_path=args.ledger_file,
            )
        )
    except (KeyError, ValueError) as error:
        return _blocked_result(error)
    return 0


def _handle_history_show(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    del config
    try:
        print_json(history_chain_for_status(connection, args.status_id))
    except KeyError as error:
        return _blocked_result(error, status="not_found")
    return 0


def _handle_memory_audit(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    result = memory_audit(config, connection)
    print_json(result)
    if not result["current_memory_ok"]:
        return 2
    if args.require_archive and not result["final_complete"]:
        return 2
    return 0


def _handle_commenter_history(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    del config
    try:
        print_json(
            commenter_history_for_event(
                connection,
                args.event_id,
                limit=args.limit,
            )
        )
    except (KeyError, ValueError) as error:
        return _blocked_result(error, status="not_found")
    return 0


def _handle_author_dossier(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    del config
    try:
        print_json(
            author_dossier_for_event(
                connection,
                args.event_id,
                limit=args.limit,
                query_text=args.query,
            )
        )
    except (KeyError, ValueError) as error:
        return _blocked_result(error, status="not_found")
    return 0


def _handle_ack(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    try:
        print_json(acknowledge_events(config, connection, args.event_ids))
    except ValueError as error:
        return _blocked_result(error)
    return 0


def _resolution_arguments(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "disposition": args.disposition,
        "reason": args.reason,
        "reply_url": args.reply_url,
        "blocker_code": args.blocker_code,
        "stance": args.stance,
        "stance_detail": args.stance_detail,
        "confidence": args.confidence,
        "media_meaning": args.media_meaning,
        "evidence": args.evidence,
    }


def _handle_resolve(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    try:
        print_json(
            resolve_event(
                config,
                connection,
                args.event_id,
                **_resolution_arguments(args),
            )
        )
    except (KeyError, ValueError) as error:
        return _blocked_result(error)
    return 0


def _handle_revise_resolution(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    try:
        print_json(
            revise_event_resolution(
                config,
                connection,
                args.event_id,
                revision_reason=args.revision_reason,
                **_resolution_arguments(args),
            )
        )
    except (KeyError, ValueError) as error:
        return _blocked_result(error)
    return 0


def _simple_command_handlers() -> dict[str, Callable[..., Any]]:
    return {
        "self-authored-reconcile": reconcile_self_authored_events,
        "initial-audit-start": start_initial_audit,
        "initial-audit-complete": complete_initial_audit,
        "initial-audit-export": export_audit_resolutions,
        "history-import": import_history_file,
        "history-export": export_history,
        "history-status": history_status,
    }


def _handle_simple_command(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int | None:
    handler = _simple_command_handlers().get(args.command)
    if handler is None:
        return None
    try:
        if args.command in {"self-authored-reconcile", "initial-audit-start", "initial-audit-complete"}:
            result = handler(config, connection)
        elif args.command == "initial-audit-export":
            result = handler(connection, args.output)
        elif args.command == "history-import":
            result = handler(connection, args.file)
        elif args.command == "history-export":
            result = handler(connection, args.output)
        else:
            result = handler(connection)
        print_json(result)
    except ValueError as error:
        return _blocked_result(error)
    return 0


def _database_command_handlers() -> dict[str, Callable[..., int]]:
    return {
        "poll": _handle_poll,
        "watch": _handle_watch,
        "status": _handle_status,
        "baseline": _handle_baseline,
        "initial-audit-next": _handle_initial_audit_next,
        "initial-audit-expire": _handle_initial_audit_expire,
        "mandatory-response-requeue": _handle_mandatory_response_requeue,
        "pro-model-recovery-requeue": _handle_pro_recovery_requeue,
        "browser-handoff-sync": _handle_browser_handoff_sync,
        "history-show": _handle_history_show,
        "memory-audit": _handle_memory_audit,
        "commenter-history": _handle_commenter_history,
        "author-dossier": _handle_author_dossier,
        "ack": _handle_ack,
        "resolve": _handle_resolve,
        "revise-resolution": _handle_revise_resolution,
        "watchdog": lambda args, config, connection: run_watchdog(
            config,
            loop=args.loop,
        ),
        "initial-audit-status": lambda args, config, connection: (
            print_json(initial_audit_status(connection)) or 0
        ),
    }


def _run_database_command(
    args: argparse.Namespace,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    simple_result = _handle_simple_command(args, config, connection)
    if simple_result is not None:
        return simple_result
    handler = _database_command_handlers().get(args.command)
    if handler is None:
        raise AssertionError(f"Unhandled command: {args.command}")
    return handler(args, config, connection)


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)

    if args.command == "preflight":
        result = preflight(config)
        print_json(result)
        return 0 if result["ready_for_live_poll"] else 2

    connection = connect_database(config.database)
    try:
        return _run_database_command(args, config, connection)
    except KeyboardInterrupt:
        return 130
    finally:
        connection.close()

if __name__ == "__main__":
    raise SystemExit(main())
