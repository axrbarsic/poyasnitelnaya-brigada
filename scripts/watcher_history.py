#!/usr/bin/env python3
"""Append-only conversation history storage for the X watcher."""

from __future__ import annotations

import contextlib
import json
import sqlite3
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


def import_history_snapshot(
    connection: sqlite3.Connection,
    record: dict[str, Any],
    *,
    within_transaction: bool = False,
) -> dict[str, Any]:
    chain_id = _validate_status_id(
        _required_text(
            record,
            "chain_id",
            "conversation_id",
            "root_id",
            "conversation_root_id",
        ),
        "chain_id",
    )
    root_status_id = _validate_status_id(
        _required_text(
            record,
            "root_status_id",
            "root_id",
            "conversation_id",
            "conversation_root_id",
        ),
        "root_status_id",
    )
    turns = record.get("turns")
    if not isinstance(turns, list) or not turns:
        raise ValueError("History record must contain a non-empty turns array")
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
    chatgpt_url = _optional_text(
        record,
        "chatgpt_conversation_url",
        "conversation_url",
    )
    ledger_reference = _optional_text(record, "ledger_reference", "ledger_ref")
    observed_at = isoformat()
    inserted_turns = 0
    inserted_sources = 0

    transaction = contextlib.nullcontext() if within_transaction else connection
    with transaction:
        existing_chain = connection.execute(
            "SELECT * FROM conversation_chains WHERE chain_id = ?",
            (chain_id,),
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
                    chain_id,
                    root_status_id,
                    provenance,
                    chatgpt_url,
                    ledger_reference,
                    observed_at,
                    observed_at,
                ),
            )
        else:
            _assert_existing_values(
                existing_chain,
                {
                    "root_status_id": root_status_id,
                    "provenance": provenance,
                    "chatgpt_conversation_url": chatgpt_url,
                    "ledger_reference": ledger_reference,
                },
                resource=f"chain {chain_id}",
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
                (chatgpt_url, ledger_reference, observed_at, chain_id),
            )

        for turn in turns:
            if not isinstance(turn, dict):
                raise ValueError("Each history turn must be an object")
            status_id = _validate_status_id(
                _required_text(turn, "status_id", "event_id"),
                "status_id",
            )
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
            actor = {
                "axrbarsic": "alex",
                "root": "other",
            }.get(actor, actor)
            if actor not in {"target", "alex", "user", "other"}:
                raise ValueError(
                    "History actor must be target, alex, user, or other"
                )
            author = _optional_text(turn, "author", "username")
            url = _required_text(turn, "url", "status_url", "event_url")
            exact_text = _required_exact_text(turn, "exact_text", "text")
            media = turn.get("media", [])
            if media is None:
                media = []
            if not isinstance(media, list):
                raise ValueError("History turn media must be an array")
            media_json = json.dumps(
                media,
                ensure_ascii=False,
                sort_keys=True,
            )
            posted_at = _optional_text(
                turn,
                "posted_at",
                "created_at",
                "timestamp",
                "timestamp_visible",
            )
            turn_provenance = _optional_text(turn, "provenance", "reply_type")
            existing_turn = connection.execute(
                "SELECT * FROM conversation_turns WHERE status_id = ?",
                (status_id,),
            ).fetchone()
            expected_turn = {
                "chain_id": chain_id,
                "parent_status_id": parent_status_id,
                "actor": actor,
                "author": author,
                "url": url,
                "exact_text": exact_text,
                "posted_at": posted_at,
                "provenance": turn_provenance,
            }
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
                        status_id,
                        chain_id,
                        parent_status_id,
                        actor,
                        author,
                        url,
                        exact_text,
                        media_json,
                        posted_at,
                        observed_at,
                        turn_provenance,
                    ),
                )
                inserted_turns += 1
            else:
                _assert_existing_values(
                    existing_turn,
                    expected_turn,
                    resource=f"turn {status_id}",
                )
                existing_media_json = existing_turn["media_json"]
                if (
                    existing_media_json not in {None, "[]"}
                    and existing_media_json != media_json
                ):
                    raise ValueError(
                        f"Append-only history conflict for turn {status_id}: "
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
                        parent_status_id,
                        author,
                        posted_at,
                        turn_provenance,
                        media_json,
                        status_id,
                    ),
                )

            sources = turn.get("source_urls", turn.get("sources", []))
            if sources is None:
                sources = []
            if not isinstance(sources, list):
                raise ValueError("History turn sources must be an array")
            for source_url in sources:
                source = str(source_url).strip()
                if not source:
                    continue
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO conversation_sources(
                        status_id, source_url
                    ) VALUES(?, ?)
                    """,
                    (status_id, source),
                )
                inserted_sources += int(cursor.rowcount)

    return {
        "chain_id": chain_id,
        "inserted_turns": inserted_turns,
        "inserted_sources": inserted_sources,
        "total_turns": len(turns),
    }

def import_chatgpt_conversation_migration(
    connection: sqlite3.Connection,
    record: dict[str, Any],
    *,
    within_transaction: bool = False,
) -> dict[str, Any]:
    chain_id = _validate_status_id(
        _required_text(record, "chain_id", "conversation_root_id"),
        "chain_id",
    )
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

    source_model = _required_text(record, "source_model")
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
    branch_from_status_id = _validate_status_id(
        _required_text(record, "branch_from_status_id"),
        "branch_from_status_id",
    )
    reason = _required_text(record, "reason", "migration_reason")
    verified_at_value = _required_text(record, "verified_at")
    verified_at = parse_time(verified_at_value)
    if (
        verified_at is None
        or verified_at.tzinfo is None
        or verified_at.utcoffset() is None
    ):
        raise ValueError(
            "ChatGPT migration verified_at must be timezone-aware"
        )
    evidence = record.get("evidence")
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(not str(item).strip() for item in evidence)
    ):
        raise ValueError(
            "ChatGPT migration requires a non-empty evidence array"
        )
    evidence_json = json.dumps(
        [str(item).strip() for item in evidence],
        ensure_ascii=False,
        sort_keys=True,
    )
    canonical_verified_at = isoformat(verified_at.astimezone(timezone.utc))

    transaction = contextlib.nullcontext() if within_transaction else connection
    with transaction:
        chain = connection.execute(
            "SELECT * FROM conversation_chains WHERE chain_id = ?",
            (chain_id,),
        ).fetchone()
        if chain is None:
            raise ValueError(
                f"ChatGPT migration chain does not exist: {chain_id}"
            )
        if chain["provenance"] not in {"pro", "mixed"}:
            raise ValueError(
                "ChatGPT migration requires a Pro or mixed chain"
            )
        branch_turn = connection.execute(
            "SELECT * FROM conversation_turns WHERE status_id = ?",
            (branch_from_status_id,),
        ).fetchone()
        if (
            branch_turn is None
            or branch_turn["chain_id"] != chain_id
            or branch_turn["actor"] != "alex"
            or branch_turn["provenance"] != "pro"
        ):
            raise ValueError(
                "ChatGPT migration requires the exact Pro-generated Alex "
                "branch turn"
            )

        existing = connection.execute(
            """
            SELECT *
            FROM chatgpt_conversation_migrations
            WHERE chain_id = ?
              AND source_url = ?
              AND target_url = ?
            """,
            (chain_id, source_url, target_url),
        ).fetchone()
        if existing is not None:
            _assert_existing_values(
                existing,
                {
                    "source_model": source_model,
                    "target_model": target_model,
                    "branch_from_status_id": branch_from_status_id,
                    "method": method,
                    "reason": reason,
                    "evidence_json": evidence_json,
                    "verified_at": canonical_verified_at,
                },
                resource=f"ChatGPT migration {chain_id}",
            )
            return {
                "chain_id": chain_id,
                "source_url": source_url,
                "target_url": target_url,
                "inserted": False,
            }

        if chain["chatgpt_conversation_url"] not in {
            source_url,
            target_url,
        }:
            raise ValueError(
                "ChatGPT migration source must equal the active chain URL, "
                "or target must already be active during exact restore"
            )
        connection.execute(
            """
            INSERT INTO chatgpt_conversation_migrations(
                chain_id, source_url, target_url, source_model,
                target_model, branch_from_status_id, method, reason,
                evidence_json, verified_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chain_id,
                source_url,
                target_url,
                source_model,
                target_model,
                branch_from_status_id,
                method,
                reason,
                evidence_json,
                canonical_verified_at,
            ),
        )
        if chain["chatgpt_conversation_url"] == source_url:
            connection.execute(
                """
                UPDATE conversation_chains
                SET chatgpt_conversation_url = ?,
                    updated_at = ?
                WHERE chain_id = ?
                """,
                (target_url, canonical_verified_at, chain_id),
            )
    return {
        "chain_id": chain_id,
        "source_url": source_url,
        "target_url": target_url,
        "inserted": True,
    }


def import_history_file(
    connection: sqlite3.Connection,
    path: Path,
) -> dict[str, Any]:
    records = _history_records(path)
    provenance_corrections: dict[str, str] = {}
    chain_provenance_corrections: dict[str, str] = {}
    for record in records:
        snapshot_type = record.get("snapshot_type")
        if snapshot_type == "provenance_correction":
            corrected_status_id = _optional_text(
                record,
                "corrected_status_id",
            )
            corrected_provenance = _optional_text(
                record,
                "corrected_provenance",
            )
            if corrected_status_id and corrected_provenance in {"short", "pro"}:
                provenance_corrections[corrected_status_id] = (
                    corrected_provenance
                )
        elif snapshot_type == "chain_provenance_correction":
            chain_id = _validate_status_id(
                _required_text(
                    record,
                    "chain_id",
                    "conversation_root_id",
                ),
                "chain_id",
            )
            corrected_provenance = _required_text(
                record,
                "corrected_provenance",
            )
            if corrected_provenance not in {"short", "pro", "mixed"}:
                raise ValueError(
                    "Corrected chain provenance must be short, pro, or mixed"
                )
            _required_text(record, "correction_reason")
            previous = chain_provenance_corrections.get(chain_id)
            if previous is not None and previous != corrected_provenance:
                raise ValueError(
                    f"Conflicting chain provenance corrections for {chain_id}"
                )
            chain_provenance_corrections[chain_id] = corrected_provenance

    chain_records: list[dict[str, Any]] = []
    migration_records: list[dict[str, Any]] = []
    for source_record in records:
        if (
            source_record.get("snapshot_type")
            == "chatgpt_conversation_migration"
        ):
            migration_records.append(source_record)
            continue
        if isinstance(source_record.get("turns"), list):
            record = json.loads(json.dumps(source_record))
        elif source_record.get("record_type") in {
            "initial_audit_event_turn",
            "initial_audit_alex_turn",
        }:
            media = source_record.get("media_json", [])
            if isinstance(media, str):
                try:
                    media = json.loads(media)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        "Flat history turn media_json must be valid JSON"
                    ) from error
            if not isinstance(media, list):
                raise ValueError(
                    "Flat history turn media_json must be an array"
                )
            chain_id = _required_text(
                source_record,
                "chain_id",
                "conversation_root_id",
            )
            existing_chain = connection.execute(
                """
                SELECT provenance
                FROM conversation_chains
                WHERE chain_id = ?
                """,
                (chain_id,),
            ).fetchone()
            record = {
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
                "provenance": _optional_text(
                    source_record,
                    "chain_provenance",
                )
                or (
                    str(existing_chain["provenance"])
                    if existing_chain is not None
                    else None
                )
                or "short",
                "turns": [
                    {
                        "status_id": _required_text(
                            source_record,
                            "status_id",
                        ),
                        "parent_status_id": source_record.get(
                            "parent_status_id"
                        ),
                        "actor": _required_text(source_record, "actor"),
                        "author": source_record.get("author"),
                        "url": _required_text(source_record, "url"),
                        "exact_text": _required_exact_text(
                            source_record,
                            "exact_text",
                        ),
                        "posted_at": source_record.get("posted_at"),
                        "provenance": source_record.get("provenance"),
                        "media": media,
                        "source_urls": source_record.get(
                            "sources",
                            source_record.get("source_urls", []),
                        ),
                    }
                ],
            }
        else:
            continue
        for turn in record["turns"]:
            if not isinstance(turn, dict):
                continue
            status_id = str(turn.get("status_id") or "")
            if status_id in provenance_corrections:
                turn["provenance"] = provenance_corrections[status_id]
        chain_id = str(record.get("chain_id") or "")
        if chain_id in chain_provenance_corrections:
            record["provenance"] = chain_provenance_corrections[chain_id]
        chain_records.append(record)

    with connection:
        results = [
            import_history_snapshot(
                connection,
                record,
                within_transaction=True,
            )
            for record in chain_records
        ]
        migration_results = [
            import_chatgpt_conversation_migration(
                connection,
                record,
                within_transaction=True,
            )
            for record in migration_records
        ]
    return {
        "records": len(results),
        "migration_records": len(migration_results),
        "inserted_migrations": sum(
            int(item["inserted"]) for item in migration_results
        ),
        "skipped_metadata_records": (
            len(records) - len(chain_records) - len(migration_records)
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
