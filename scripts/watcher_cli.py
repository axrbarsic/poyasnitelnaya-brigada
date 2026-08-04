#!/usr/bin/env python3
"""Argument parser for the X mention watcher entrypoint."""

from __future__ import annotations

import argparse
from pathlib import Path


def build_parser(
    *,
    description: str,
    terminal_blocker_codes: frozenset[str],
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
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
    status = commands.add_parser(
        "status",
        help="Print compact health and queue status.",
    )
    status.add_argument(
        "--full",
        action="store_true",
        help="Include every queued event in the command output.",
    )
    commands.add_parser(
        "baseline",
        help="Legacy recovery command. Do not use for a normal first audit.",
    )
    commands.add_parser(
        "self-authored-reconcile",
        help=(
            "Remove unresolved posts authored by the configured account from "
            "the inbound reply queue without creating an event resolution."
        ),
    )
    commands.add_parser(
        "initial-audit-start",
        help="Queue every unresolved historical direct reply for first review.",
    )
    commands.add_parser(
        "initial-audit-status",
        help="Show first-review progress and remaining queued events.",
    )
    audit_next = commands.add_parser(
        "initial-audit-next",
        help="Show the next unresolved conversation groups newest-first.",
    )
    audit_next.add_argument(
        "--conversations",
        type=int,
        default=1,
        help="Number of conversation groups to return.",
    )
    audit_expire = commands.add_parser(
        "initial-audit-expire",
        help=(
            "Durably skip unresolved replies older than Alex's one-time "
            "lookback window without Browser review."
        ),
    )
    audit_expire.add_argument(
        "--hours",
        type=float,
        required=True,
        help=(
            "One-time lookback window selected by Alex for this run. "
            "The UTC cutoff is fixed when the command starts."
        ),
    )
    audit_expire.add_argument(
        "--as-of",
        required=True,
        help=(
            "Timezone-aware ISO-8601 cycle start. Reuse the exact same value "
            "for dry-run and apply."
        ),
    )
    audit_expire.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report candidates without changing durable state.",
    )
    response_requeue = commands.add_parser(
        "mandatory-response-requeue",
        help=(
            "Requeue recent content-based skips and previously ignored mention "
            "replies that have no exact Alex child reply."
        ),
    )
    response_requeue.add_argument(
        "--hours",
        type=float,
        required=True,
        help="Recent lookback window to reconcile under mandatory mode.",
    )
    response_requeue.add_argument(
        "--as-of",
        required=True,
        help="Timezone-aware ISO-8601 reconciliation time.",
    )
    response_requeue.add_argument(
        "--dry-run",
        action="store_true",
        help="Report candidates without changing queue state.",
    )
    pro_recovery_requeue = commands.add_parser(
        "pro-model-recovery-requeue",
        help=(
            "Requeue legacy ChatGPT, model, and screenshot blockers now "
            "covered by the local poyasnitelnaya-brigada skill."
        ),
    )
    pro_recovery_requeue.add_argument(
        "--dry-run",
        action="store_true",
        help="Report candidates without changing queue state.",
    )
    deleted_replacement = commands.add_parser(
        "replace-deleted-publication",
        help=(
            "Requeue one exact published event only after the official X API "
            "proves that its recorded Alex reply was deleted."
        ),
    )
    deleted_replacement.add_argument("event_id")
    deleted_replacement.add_argument("--reason", required=True)
    commands.add_parser(
        "initial-audit-complete",
        help="Complete first review only when the queue is empty.",
    )
    audit_export = commands.add_parser(
        "initial-audit-export",
        help="Export canonical initial-audit resolutions as JSONL.",
    )
    audit_export.add_argument("--output", type=Path, required=True)
    history_import = commands.add_parser(
        "history-import",
        help="Import append-only conversation chain snapshots.",
    )
    history_import.add_argument("--file", type=Path, required=True)
    browser_sync = commands.add_parser(
        "browser-handoff-sync",
        help=(
            "Import exact Browser history and durably resolve confirmed "
            "initial-audit dispositions."
        ),
    )
    browser_sync.add_argument("--history-file", type=Path, required=True)
    browser_sync.add_argument("--ledger-file", type=Path, required=True)

    history_show = commands.add_parser(
        "history-show",
        help="Show a complete stored conversation chain for one status ID.",
    )
    history_show.add_argument("status_id")

    history_export = commands.add_parser(
        "history-export",
        help="Export canonical conversation history as JSONL.",
    )
    history_export.add_argument("--output", type=Path, required=True)

    commands.add_parser(
        "history-status",
        help="Show conversation history database counts.",
    )
    memory_check = commands.add_parser(
        "memory-audit",
        help=(
            "Fail closed on database, identity, history, archive, and "
            "candidate-memory inconsistencies."
        ),
    )
    memory_check.add_argument(
        "--require-archive",
        action="store_true",
        help=(
            "Return failure until a valid official X archive import is "
            "present."
        ),
    )
    commenter_history = commands.add_parser(
        "commenter-history",
        help=(
            "Show source-linked public interaction history for the author of "
            "one stored event."
        ),
    )
    commenter_history.add_argument("event_id")
    commenter_history.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum prior interactions to return, newest-first.",
    )
    author_dossier = commands.add_parser(
        "author-dossier",
        help=(
            "Build a source-linked, context-aware navigation dossier for "
            "the author of one stored event."
        ),
    )
    author_dossier.add_argument("event_id")
    author_dossier.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum recent and separately relevant records to return.",
    )
    author_dossier.add_argument(
        "--query",
        help=(
            "Optional current thesis text for context-aware retrieval. "
            "Omit to use the stored event text."
        ),
    )

    acknowledge = commands.add_parser("ack", help="Acknowledge queued event IDs.")
    acknowledge.add_argument("event_ids", nargs="+")

    resolve = commands.add_parser(
        "resolve",
        help=(
            "Durably resolve one queued event as published, skipped, or "
            "contract-blocked."
        ),
    )
    resolve.add_argument("event_id")
    resolve.add_argument(
        "--disposition",
        choices=("published", "skip", "blocked"),
        required=True,
    )
    resolve.add_argument("--reason", required=True)
    resolve.add_argument("--reply-url")
    resolve.add_argument(
        "--blocker-code",
        choices=tuple(sorted(terminal_blocker_codes)),
    )
    resolve.add_argument(
        "--stance",
        choices=("supportive", "opposing", "neutral", "ambiguous"),
    )
    resolve.add_argument("--stance-detail")
    resolve.add_argument(
        "--confidence",
        choices=("high", "medium", "low"),
    )
    resolve.add_argument("--media-meaning")
    resolve.add_argument("--evidence", action="append", default=[])
    revise = commands.add_parser(
        "revise-resolution",
        help=(
            "Auditably revise an existing skip or blocker, or consume an "
            "authorized deleted-publication replacement."
        ),
    )
    revise.add_argument("event_id")
    revise.add_argument(
        "--disposition",
        choices=("published", "skip", "blocked"),
        required=True,
    )
    revise.add_argument("--reason", required=True)
    revise.add_argument("--reply-url")
    revise.add_argument(
        "--blocker-code",
        choices=tuple(sorted(terminal_blocker_codes)),
    )
    revise.add_argument("--revision-reason", required=True)
    revise.add_argument(
        "--stance",
        choices=("supportive", "opposing", "neutral", "ambiguous"),
    )
    revise.add_argument("--stance-detail")
    revise.add_argument(
        "--confidence",
        choices=("high", "medium", "low"),
    )
    revise.add_argument("--media-meaning")
    revise.add_argument("--evidence", action="append", default=[])

    watchdog = commands.add_parser("watchdog", help="Check watcher health.")
    watchdog.add_argument("--loop", action="store_true")
    return parser
