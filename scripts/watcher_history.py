#!/usr/bin/env python3
"""Append-only conversation history storage for the X watcher."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any

from scripts import (
    watcher_chatgpt,
    watcher_constants,
    watcher_time,
    watcher_validation,
)


SCHEMA_VERSION = watcher_constants.SCHEMA_VERSION
isoformat = watcher_time.isoformat
parse_time = watcher_time.parse_time
_chatgpt_custom_gpt_scope = watcher_chatgpt.custom_gpt_scope

def _history_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Invalid JSONL at line {line_number}"
                ) from error
            if not isinstance(record, dict):
                raise ValueError(
                    f"History record at line {line_number} must be an object"
                )
            records.append(record)
        return records
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return payload
    raise ValueError("History input must be an object, object array, or JSONL")


_required_text = watcher_validation.required_text
_required_exact_text = watcher_validation.required_exact_text
_optional_text = watcher_validation.optional_text
_validate_status_id = watcher_validation.validate_status_id
_assert_existing_values = watcher_validation.assert_existing_values


@dataclass(frozen=True)
class HistoryTurnInput:
    status_id: str
    parent_status_id: str | None
    actor: str
    author: str | None
    url: str
    exact_text: str
    media_json: str
    posted_at: str | None
    provenance: str | None
    source_urls: tuple[str, ...]


@dataclass(frozen=True)
class HistorySnapshotInput:
    chain_id: str
    root_status_id: str
    provenance: str
    chatgpt_url: str | None
    ledger_reference: str | None
    turns: tuple[HistoryTurnInput, ...]


def _snapshot_provenance(
    record: dict[str, Any],
    turns: list[Any],
) -> str:
    provenance = _optional_text(record, "provenance", "reply_type")
    if provenance is None:
        turn_provenance = {
            str(turn.get("provenance")).strip()
            for turn in turns
            if isinstance(turn, dict) and turn.get("provenance")
        }
        provenance = "pro" if "pro" in turn_provenance else "short"
    if provenance not in {"short", "pro", "mixed"}:
        raise ValueError("History provenance must be short, pro, or mixed")
    return provenance


def _history_media_json(turn: dict[str, Any]) -> str:
    media = turn.get("media", [])
    if media is None:
        media = []
    if not isinstance(media, list):
        raise ValueError("History turn media must be an array")
    return json.dumps(media, ensure_ascii=False, sort_keys=True)


def _history_source_urls(turn: dict[str, Any]) -> tuple[str, ...]:
    sources = turn.get("source_urls", turn.get("sources", []))
    if sources is None:
        sources = []
    if not isinstance(sources, list):
        raise ValueError("History turn sources must be an array")
    return tuple(
        source
        for source_url in sources
        if (source := str(source_url).strip())
    )


def _normalize_history_turn(turn: Any) -> HistoryTurnInput:
    if not isinstance(turn, dict):
        raise ValueError("Each history turn must be an object")
    parent_status_id = _optional_text(
        turn,
        "parent_status_id",
        "in_reply_to_status_id",
    )
    if parent_status_id is not None:
        parent_status_id = _validate_status_id(
            parent_status_id,
            "parent_status_id",
        )
    actor = _required_text(turn, "actor")
    actor = {"axrbarsic": "alex", "root": "other"}.get(actor, actor)
    if actor not in {"target", "alex", "user", "other"}:
        raise ValueError("History actor must be target, alex, user, or other")
    return HistoryTurnInput(
        status_id=_validate_status_id(
            _required_text(turn, "status_id", "event_id"),
            "status_id",
        ),
        parent_status_id=parent_status_id,
        actor=actor,
        author=_optional_text(turn, "author", "username"),
        url=_required_text(turn, "url", "status_url", "event_url"),
        exact_text=_required_exact_text(turn, "exact_text", "text"),
        media_json=_history_media_json(turn),
        posted_at=_optional_text(
            turn,
            "posted_at",
            "created_at",
            "timestamp",
            "timestamp_visible",
        ),
        provenance=_optional_text(turn, "provenance", "reply_type"),
        source_urls=_history_source_urls(turn),
    )


def _normalize_history_snapshot(record: dict[str, Any]) -> HistorySnapshotInput:
    turns = record.get("turns")
    if not isinstance(turns, list) or not turns:
        raise ValueError("History record must contain a non-empty turns array")
    return HistorySnapshotInput(
        chain_id=_validate_status_id(
            _required_text(
                record,
                "chain_id",
                "conversation_id",
                "root_id",
                "conversation_root_id",
            ),
            "chain_id",
        ),
        root_status_id=_validate_status_id(
            _required_text(
                record,
                "root_status_id",
                "root_id",
                "conversation_id",
                "conversation_root_id",
            ),
            "root_status_id",
        ),
        provenance=_snapshot_provenance(record, turns),
        chatgpt_url=_optional_text(
            record,
            "chatgpt_conversation_url",
            "conversation_url",
        ),
        ledger_reference=_optional_text(
            record,
            "ledger_reference",
            "ledger_ref",
        ),
        turns=tuple(_normalize_history_turn(turn) for turn in turns),
    )


def _upsert_history_chain(
    connection: sqlite3.Connection,
    snapshot: HistorySnapshotInput,
    observed_at: str,
) -> None:
    existing_chain = connection.execute(
        "SELECT * FROM conversation_chains WHERE chain_id = ?",
        (snapshot.chain_id,),
    ).fetchone()
    if existing_chain is None:
        connection.execute(
            """
            INSERT INTO conversation_chains(
                chain_id, root_status_id, provenance,
                chatgpt_conversation_url, ledger_reference,
                created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.chain_id,
                snapshot.root_status_id,
                snapshot.provenance,
                snapshot.chatgpt_url,
                snapshot.ledger_reference,
                observed_at,
                observed_at,
            ),
        )
        return
    _assert_existing_values(
        existing_chain,
        {
            "root_status_id": snapshot.root_status_id,
            "provenance": snapshot.provenance,
            "chatgpt_conversation_url": snapshot.chatgpt_url,
            "ledger_reference": snapshot.ledger_reference,
        },
        resource=f"chain {snapshot.chain_id}",
    )
    connection.execute(
        """
        UPDATE conversation_chains
        SET chatgpt_conversation_url =
                COALESCE(chatgpt_conversation_url, ?),
            ledger_reference = COALESCE(ledger_reference, ?),
            updated_at = ?
        WHERE chain_id = ?
        """,
        (
            snapshot.chatgpt_url,
            snapshot.ledger_reference,
            observed_at,
            snapshot.chain_id,
        ),
    )


def _turn_expected_values(
    snapshot: HistorySnapshotInput,
    turn: HistoryTurnInput,
) -> dict[str, str | None]:
    return {
        "chain_id": snapshot.chain_id,
        "parent_status_id": turn.parent_status_id,
        "actor": turn.actor,
        "author": turn.author,
        "url": turn.url,
        "exact_text": turn.exact_text,
        "posted_at": turn.posted_at,
        "provenance": turn.provenance,
    }


def _upsert_history_turn(
    connection: sqlite3.Connection,
    snapshot: HistorySnapshotInput,
    turn: HistoryTurnInput,
    observed_at: str,
) -> bool:
    existing_turn = connection.execute(
        "SELECT * FROM conversation_turns WHERE status_id = ?",
        (turn.status_id,),
    ).fetchone()
    if existing_turn is None:
        connection.execute(
            """
            INSERT INTO conversation_turns(
                status_id, chain_id, parent_status_id, actor, author,
                url, exact_text, media_json, posted_at, observed_at,
                provenance
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                turn.status_id,
                snapshot.chain_id,
                turn.parent_status_id,
                turn.actor,
                turn.author,
                turn.url,
                turn.exact_text,
                turn.media_json,
                turn.posted_at,
                observed_at,
                turn.provenance,
            ),
        )
        return True
    _assert_existing_values(
        existing_turn,
        _turn_expected_values(snapshot, turn),
        resource=f"turn {turn.status_id}",
    )
    existing_media_json = existing_turn["media_json"]
    if (
        existing_media_json not in {None, "[]"}
        and existing_media_json != turn.media_json
    ):
        raise ValueError(
            f"Append-only history conflict for turn {turn.status_id}: "
            "media_json"
        )
    connection.execute(
        """
        UPDATE conversation_turns
        SET parent_status_id = COALESCE(parent_status_id, ?),
            author = COALESCE(author, ?),
            posted_at = COALESCE(posted_at, ?),
            provenance = COALESCE(provenance, ?),
            media_json = CASE
                WHEN media_json = '[]' THEN ?
                ELSE media_json
            END
        WHERE status_id = ?
        """,
        (
            turn.parent_status_id,
            turn.author,
            turn.posted_at,
            turn.provenance,
            turn.media_json,
            turn.status_id,
        ),
    )
    return False


def _insert_history_sources(
    connection: sqlite3.Connection,
    turn: HistoryTurnInput,
) -> int:
    inserted = 0
    for source_url in turn.source_urls:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO conversation_sources(status_id, source_url)
            VALUES(?, ?)
            """,
            (turn.status_id, source_url),
        )
        inserted += int(cursor.rowcount)
    return inserted


def _persist_history_snapshot(
    connection: sqlite3.Connection,
    snapshot: HistorySnapshotInput,
    observed_at: str,
) -> tuple[int, int]:
    _upsert_history_chain(connection, snapshot, observed_at)
    inserted_turns = 0
    inserted_sources = 0
    for turn in snapshot.turns:
        inserted_turns += int(
            _upsert_history_turn(connection, snapshot, turn, observed_at)
        )
        inserted_sources += _insert_history_sources(connection, turn)
    return inserted_turns, inserted_sources


def import_history_snapshot(
    connection: sqlite3.Connection,
    record: dict[str, Any],
    *,
    within_transaction: bool = False,
) -> dict[str, Any]:
    snapshot = _normalize_history_snapshot(record)
    observed_at = isoformat()
    transaction = contextlib.nullcontext() if within_transaction else connection
    with transaction:
        inserted_turns, inserted_sources = _persist_history_snapshot(
            connection,
            snapshot,
            observed_at,
        )

    return {
        "chain_id": snapshot.chain_id,
        "inserted_turns": inserted_turns,
        "inserted_sources": inserted_sources,
        "total_turns": len(snapshot.turns),
    }


@dataclass(frozen=True)
class ChatGPTMigrationInput:
    chain_id: str
    source_url: str
    target_url: str
    source_model: str
    target_model: str
    branch_from_status_id: str
    method: str
    reason: str
    evidence_json: str
    verified_at: str


def _migration_urls(record: dict[str, Any]) -> tuple[str, str]:
    source_url = _required_text(
        record,
        "source_chatgpt_conversation_url",
        "source_url",
    )
    target_url = _required_text(
        record,
        "target_chatgpt_conversation_url",
        "target_url",
    )
    source_scope = _chatgpt_custom_gpt_scope(source_url)
    target_scope = _chatgpt_custom_gpt_scope(target_url)
    if source_scope is None or target_scope is None:
        raise ValueError(
            "ChatGPT migration requires exact custom GPT conversation URLs"
        )
    if source_scope != target_scope:
        raise ValueError(
            "ChatGPT migration must preserve the exact custom GPT identity"
        )
    if source_url == target_url:
        raise ValueError("ChatGPT migration target must differ from source")
    return source_url, target_url


def _migration_verified_at(record: dict[str, Any]) -> str:
    verified_at = parse_time(_required_text(record, "verified_at"))
    if (
        verified_at is None
        or verified_at.tzinfo is None
        or verified_at.utcoffset() is None
    ):
        raise ValueError(
            "ChatGPT migration verified_at must be timezone-aware"
        )
    return isoformat(verified_at.astimezone(timezone.utc))


def _migration_evidence_json(record: dict[str, Any]) -> str:
    evidence = record.get("evidence")
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(not str(item).strip() for item in evidence)
    ):
        raise ValueError(
            "ChatGPT migration requires a non-empty evidence array"
        )
    return json.dumps(
        [str(item).strip() for item in evidence],
        ensure_ascii=False,
        sort_keys=True,
    )


def _normalize_chatgpt_migration(
    record: dict[str, Any],
) -> ChatGPTMigrationInput:
    source_url, target_url = _migration_urls(record)
    target_model = _required_text(record, "target_model")
    if target_model != "ChatGPT 5.6 Pro":
        raise ValueError(
            "ChatGPT migration target model must be exactly ChatGPT 5.6 Pro"
        )
    method = _required_text(record, "method")
    if method != "branch_in_new_chat":
        raise ValueError(
            "ChatGPT migration method must be branch_in_new_chat"
        )
    return ChatGPTMigrationInput(
        chain_id=_validate_status_id(
            _required_text(record, "chain_id", "conversation_root_id"),
            "chain_id",
        ),
        source_url=source_url,
        target_url=target_url,
        source_model=_required_text(record, "source_model"),
        target_model=target_model,
        branch_from_status_id=_validate_status_id(
            _required_text(record, "branch_from_status_id"),
            "branch_from_status_id",
        ),
        method=method,
        reason=_required_text(record, "reason", "migration_reason"),
        evidence_json=_migration_evidence_json(record),
        verified_at=_migration_verified_at(record),
    )


def _require_migration_chain(
    connection: sqlite3.Connection,
    migration: ChatGPTMigrationInput,
) -> sqlite3.Row:
    chain = connection.execute(
        "SELECT * FROM conversation_chains WHERE chain_id = ?",
        (migration.chain_id,),
    ).fetchone()
    if chain is None:
        raise ValueError(
            f"ChatGPT migration chain does not exist: {migration.chain_id}"
        )
    if chain["provenance"] not in {"pro", "mixed"}:
        raise ValueError("ChatGPT migration requires a Pro or mixed chain")
    return chain


def _require_migration_branch(
    connection: sqlite3.Connection,
    migration: ChatGPTMigrationInput,
) -> None:
    branch_turn = connection.execute(
        "SELECT * FROM conversation_turns WHERE status_id = ?",
        (migration.branch_from_status_id,),
    ).fetchone()
    if (
        branch_turn is None
        or branch_turn["chain_id"] != migration.chain_id
        or branch_turn["actor"] != "alex"
        or branch_turn["provenance"] != "pro"
    ):
        raise ValueError(
            "ChatGPT migration requires the exact Pro-generated Alex "
            "branch turn"
        )


def _existing_migration(
    connection: sqlite3.Connection,
    migration: ChatGPTMigrationInput,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT *
        FROM chatgpt_conversation_migrations
        WHERE chain_id = ?
          AND source_url = ?
          AND target_url = ?
        """,
        (
            migration.chain_id,
            migration.source_url,
            migration.target_url,
        ),
    ).fetchone()


def _assert_matching_migration(
    existing: sqlite3.Row,
    migration: ChatGPTMigrationInput,
) -> None:
    _assert_existing_values(
        existing,
        {
            "source_model": migration.source_model,
            "target_model": migration.target_model,
            "branch_from_status_id": migration.branch_from_status_id,
            "method": migration.method,
            "reason": migration.reason,
            "evidence_json": migration.evidence_json,
            "verified_at": migration.verified_at,
        },
        resource=f"ChatGPT migration {migration.chain_id}",
    )


def _insert_chatgpt_migration(
    connection: sqlite3.Connection,
    migration: ChatGPTMigrationInput,
) -> None:
    connection.execute(
        """
        INSERT INTO chatgpt_conversation_migrations(
            chain_id, source_url, target_url, source_model,
            target_model, branch_from_status_id, method, reason,
            evidence_json, verified_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            migration.chain_id,
            migration.source_url,
            migration.target_url,
            migration.source_model,
            migration.target_model,
            migration.branch_from_status_id,
            migration.method,
            migration.reason,
            migration.evidence_json,
            migration.verified_at,
        ),
    )


def _persist_chatgpt_migration(
    connection: sqlite3.Connection,
    migration: ChatGPTMigrationInput,
) -> bool:
    chain = _require_migration_chain(connection, migration)
    _require_migration_branch(connection, migration)
    existing = _existing_migration(connection, migration)
    if existing is not None:
        _assert_matching_migration(existing, migration)
        return False
    if chain["chatgpt_conversation_url"] not in {
        migration.source_url,
        migration.target_url,
    }:
        raise ValueError(
            "ChatGPT migration source must equal the active chain URL, "
            "or target must already be active during exact restore"
        )
    _insert_chatgpt_migration(connection, migration)
    if chain["chatgpt_conversation_url"] == migration.source_url:
        connection.execute(
            """
            UPDATE conversation_chains
            SET chatgpt_conversation_url = ?,
                updated_at = ?
            WHERE chain_id = ?
            """,
            (
                migration.target_url,
                migration.verified_at,
                migration.chain_id,
            ),
        )
    return True


def import_chatgpt_conversation_migration(
    connection: sqlite3.Connection,
    record: dict[str, Any],
    *,
    within_transaction: bool = False,
) -> dict[str, Any]:
    migration = _normalize_chatgpt_migration(record)
    transaction = contextlib.nullcontext() if within_transaction else connection
    with transaction:
        inserted = _persist_chatgpt_migration(connection, migration)
    return {
        "chain_id": migration.chain_id,
        "source_url": migration.source_url,
        "target_url": migration.target_url,
        "inserted": inserted,
    }


@dataclass(frozen=True)
class HistoryFileBatch:
    source_record_count: int
    chain_records: tuple[dict[str, Any], ...]
    migration_records: tuple[dict[str, Any], ...]


def _history_provenance_corrections(
    records: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    turn_corrections: dict[str, str] = {}
    chain_corrections: dict[str, str] = {}
    for record in records:
        snapshot_type = record.get("snapshot_type")
        if snapshot_type == "provenance_correction":
            status_id = _optional_text(record, "corrected_status_id")
            provenance = _optional_text(record, "corrected_provenance")
            if status_id and provenance in {"short", "pro"}:
                turn_corrections[status_id] = provenance
            continue
        if snapshot_type != "chain_provenance_correction":
            continue
        chain_id = _validate_status_id(
            _required_text(record, "chain_id", "conversation_root_id"),
            "chain_id",
        )
        provenance = _required_text(record, "corrected_provenance")
        if provenance not in {"short", "pro", "mixed"}:
            raise ValueError(
                "Corrected chain provenance must be short, pro, or mixed"
            )
        _required_text(record, "correction_reason")
        previous = chain_corrections.get(chain_id)
        if previous is not None and previous != provenance:
            raise ValueError(
                f"Conflicting chain provenance corrections for {chain_id}"
            )
        chain_corrections[chain_id] = provenance
    return turn_corrections, chain_corrections


def _flat_history_media(source_record: dict[str, Any]) -> list[Any]:
    media = source_record.get("media_json", [])
    if isinstance(media, str):
        try:
            media = json.loads(media)
        except json.JSONDecodeError as error:
            raise ValueError(
                "Flat history turn media_json must be valid JSON"
            ) from error
    if not isinstance(media, list):
        raise ValueError("Flat history turn media_json must be an array")
    return media


def _existing_chain_provenance(
    connection: sqlite3.Connection,
    chain_id: str,
) -> str | None:
    row = connection.execute(
        """
        SELECT provenance
        FROM conversation_chains
        WHERE chain_id = ?
        """,
        (chain_id,),
    ).fetchone()
    return str(row["provenance"]) if row is not None else None


def _flat_history_record(
    connection: sqlite3.Connection,
    source_record: dict[str, Any],
) -> dict[str, Any]:
    chain_id = _required_text(
        source_record,
        "chain_id",
        "conversation_root_id",
    )
    return {
        "chain_id": chain_id,
        "root_status_id": _required_text(
            source_record,
            "conversation_root_id",
            "chain_id",
        ),
        "chatgpt_conversation_url": source_record.get(
            "chatgpt_conversation_url"
        ),
        "ledger_reference": source_record.get("ledger_reference"),
        "provenance": (
            _optional_text(source_record, "chain_provenance")
            or _existing_chain_provenance(connection, chain_id)
            or "short"
        ),
        "turns": [
            {
                "status_id": _required_text(source_record, "status_id"),
                "parent_status_id": source_record.get("parent_status_id"),
                "actor": _required_text(source_record, "actor"),
                "author": source_record.get("author"),
                "url": _required_text(source_record, "url"),
                "exact_text": _required_exact_text(
                    source_record,
                    "exact_text",
                ),
                "posted_at": source_record.get("posted_at"),
                "provenance": source_record.get("provenance"),
                "media": _flat_history_media(source_record),
                "source_urls": source_record.get(
                    "sources",
                    source_record.get("source_urls", []),
                ),
            }
        ],
    }


def _history_chain_record(
    connection: sqlite3.Connection,
    source_record: dict[str, Any],
) -> dict[str, Any] | None:
    if isinstance(source_record.get("turns"), list):
        return json.loads(json.dumps(source_record))
    if source_record.get("record_type") in {
        "initial_audit_event_turn",
        "initial_audit_alex_turn",
    }:
        return _flat_history_record(connection, source_record)
    return None


def _apply_history_corrections(
    record: dict[str, Any],
    turn_corrections: dict[str, str],
    chain_corrections: dict[str, str],
) -> None:
    for turn in record["turns"]:
        if not isinstance(turn, dict):
            continue
        status_id = str(turn.get("status_id") or "")
        if status_id in turn_corrections:
            turn["provenance"] = turn_corrections[status_id]
    chain_id = str(record.get("chain_id") or "")
    if chain_id in chain_corrections:
        record["provenance"] = chain_corrections[chain_id]


def _classify_history_records(
    connection: sqlite3.Connection,
    records: list[dict[str, Any]],
) -> HistoryFileBatch:
    turn_corrections, chain_corrections = (
        _history_provenance_corrections(records)
    )
    chain_records: list[dict[str, Any]] = []
    migration_records: list[dict[str, Any]] = []
    for source_record in records:
        if source_record.get("snapshot_type") == (
            "chatgpt_conversation_migration"
        ):
            migration_records.append(source_record)
            continue
        record = _history_chain_record(connection, source_record)
        if record is None:
            continue
        _apply_history_corrections(
            record,
            turn_corrections,
            chain_corrections,
        )
        chain_records.append(record)
    return HistoryFileBatch(
        source_record_count=len(records),
        chain_records=tuple(chain_records),
        migration_records=tuple(migration_records),
    )


def _import_history_batch(
    connection: sqlite3.Connection,
    batch: HistoryFileBatch,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with connection:
        results = [
            import_history_snapshot(
                connection,
                record,
                within_transaction=True,
            )
            for record in batch.chain_records
        ]
        migration_results = [
            import_chatgpt_conversation_migration(
                connection,
                record,
                within_transaction=True,
            )
            for record in batch.migration_records
        ]
    return results, migration_results


def import_history_file(
    connection: sqlite3.Connection,
    path: Path,
) -> dict[str, Any]:
    records = _history_records(path)
    batch = _classify_history_records(connection, records)
    results, migration_results = _import_history_batch(connection, batch)
    return {
        "records": len(results),
        "migration_records": len(migration_results),
        "inserted_migrations": sum(
            int(item["inserted"]) for item in migration_results
        ),
        "skipped_metadata_records": (
            batch.source_record_count
            - len(batch.chain_records)
            - len(batch.migration_records)
        ),
        "inserted_turns": sum(item["inserted_turns"] for item in results),
        "inserted_sources": sum(item["inserted_sources"] for item in results),
        "chains": [item["chain_id"] for item in results],
    }

def history_chain_for_status(
    connection: sqlite3.Connection,
    status_id: str,
) -> dict[str, Any]:
    status = _validate_status_id(status_id, "status_id")
    turn_match = connection.execute(
        "SELECT chain_id FROM conversation_turns WHERE status_id = ?",
        (status,),
    ).fetchone()
    if turn_match is not None:
        chain = connection.execute(
            "SELECT * FROM conversation_chains WHERE chain_id = ?",
            (turn_match["chain_id"],),
        ).fetchone()
    else:
        chain = connection.execute(
            "SELECT * FROM conversation_chains WHERE chain_id = ?",
            (status,),
        ).fetchone()
    if chain is None:
        root_matches = connection.execute(
            """
            SELECT *
            FROM conversation_chains
            WHERE root_status_id = ?
            ORDER BY chain_id
            """,
            (status,),
        ).fetchall()
        if len(root_matches) > 1:
            raise ValueError(
                f"Ambiguous conversation history for root status {status}"
            )
        chain = root_matches[0] if root_matches else None
    if chain is None:
        raise KeyError(f"No conversation history for status {status}")
    turns = connection.execute(
        """
        SELECT *
        FROM conversation_turns
        WHERE chain_id = ?
        ORDER BY COALESCE(posted_at, observed_at), CAST(status_id AS INTEGER)
        """,
        (chain["chain_id"],),
    ).fetchall()
    result_turns: list[dict[str, Any]] = []
    for turn in turns:
        sources = connection.execute(
            """
            SELECT source_url
            FROM conversation_sources
            WHERE status_id = ?
            ORDER BY source_url
            """,
            (turn["status_id"],),
        ).fetchall()
        result_turns.append(
            {
                "status_id": turn["status_id"],
                "parent_status_id": turn["parent_status_id"],
                "actor": turn["actor"],
                "author": turn["author"],
                "url": turn["url"],
                "exact_text": turn["exact_text"],
                "media": json.loads(turn["media_json"] or "[]"),
                "posted_at": turn["posted_at"],
                "observed_at": turn["observed_at"],
                "provenance": turn["provenance"],
                "source_urls": [row["source_url"] for row in sources],
            }
        )
    migration_rows = connection.execute(
        """
        SELECT *
        FROM chatgpt_conversation_migrations
        WHERE chain_id = ?
        ORDER BY migration_id
        """,
        (chain["chain_id"],),
    ).fetchall()
    migrations = [
        {
            "snapshot_type": "chatgpt_conversation_migration",
            "chain_id": row["chain_id"],
            "source_chatgpt_conversation_url": row["source_url"],
            "target_chatgpt_conversation_url": row["target_url"],
            "source_model": row["source_model"],
            "target_model": row["target_model"],
            "branch_from_status_id": row["branch_from_status_id"],
            "method": row["method"],
            "reason": row["reason"],
            "evidence": json.loads(row["evidence_json"]),
            "verified_at": row["verified_at"],
        }
        for row in migration_rows
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "chain_id": chain["chain_id"],
        "root_status_id": chain["root_status_id"],
        "provenance": chain["provenance"],
        "chatgpt_conversation_url": chain["chatgpt_conversation_url"],
        "ledger_reference": chain["ledger_reference"],
        "turns": result_turns,
        "chatgpt_conversation_migrations": migrations,
    }


def export_history(
    connection: sqlite3.Connection,
    output: Path,
) -> dict[str, Any]:
    chain_ids = [
        str(row["chain_id"])
        for row in connection.execute(
            "SELECT chain_id FROM conversation_chains ORDER BY chain_id"
        ).fetchall()
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for chain_id in chain_ids:
            chain = history_chain_for_status(connection, chain_id)
            handle.write(
                json.dumps(
                    chain,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
            for migration in chain["chatgpt_conversation_migrations"]:
                handle.write(
                    json.dumps(
                        migration,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
    temporary.replace(output)
    return {
        "output": str(output),
        "chains": len(chain_ids),
    }


def history_status(connection: sqlite3.Connection) -> dict[str, Any]:
    chains = int(
        connection.execute(
            "SELECT COUNT(*) AS count FROM conversation_chains"
        ).fetchone()["count"]
    )
    turns = int(
        connection.execute(
            "SELECT COUNT(*) AS count FROM conversation_turns"
        ).fetchone()["count"]
    )
    sources = int(
        connection.execute(
            "SELECT COUNT(*) AS count FROM conversation_sources"
        ).fetchone()["count"]
    )
    migrations = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM chatgpt_conversation_migrations
            """
        ).fetchone()["count"]
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "chains": chains,
        "turns": turns,
        "sources": sources,
        "chatgpt_conversation_migrations": migrations,
    }
