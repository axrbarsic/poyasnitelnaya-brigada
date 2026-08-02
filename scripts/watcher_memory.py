#!/usr/bin/env python3
"""Cross-conversation commenter memory for the X watcher."""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from scripts import watcher_constants, watcher_history, watcher_validation



def _excerpt(value: str, limit: int = 400) -> dict[str, Any]:
    code_points = list(value)
    truncated = len(code_points) > limit
    excerpt = "".join(code_points[:limit])
    return {
        "text": excerpt,
        "truncated": truncated,
    }


CANDIDATE_MEMORY_CONTRACT = (
    "Unverified candidates are search hints only. Verify the exact "
    "live X post before quoting or using it as evidence."
)


def _empty_commenter_history(event_id: str) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "identity_kind": "unavailable",
        "author_id": None,
        "username": None,
        "total_prior_interactions": 0,
        "first_interaction_at": None,
        "last_interaction_at": None,
        "returned_interactions": 0,
        "interactions": [],
        "total_prior_archive_alex_replies": 0,
        "first_archive_alex_reply_at": None,
        "last_archive_alex_reply_at": None,
        "returned_archive_alex_replies": 0,
        "archive_alex_replies": [],
        "total_candidate_public_posts": 0,
        "returned_candidate_public_posts": 0,
        "candidate_public_posts": [],
        "candidate_memory_contract": CANDIDATE_MEMORY_CONTRACT,
    }


def _commenter_identity(
    connection: sqlite3.Connection,
    event_id: str,
) -> tuple[sqlite3.Row, str, str, str] | tuple[sqlite3.Row, None, None, None]:
    current = connection.execute(
        """
        SELECT event_id, author_id, username
        FROM events
        WHERE event_id = ?
        """,
        (event_id,),
    ).fetchone()
    if current is None:
        raise KeyError(f"Unknown event {event_id}")
    author_id = current["author_id"]
    username = current["username"]
    if author_id:
        return current, "e.author_id = ?", str(author_id), "x_user_id"
    if username:
        return (
            current,
            "LOWER(e.username) = LOWER(?)",
            str(username),
            "x_handle_fallback",
        )
    return current, None, None, None


def _prior_interactions(
    connection: sqlite3.Connection,
    event_id: str,
    *,
    identity_clause: str,
    identity_value: str,
    limit: int,
) -> tuple[sqlite3.Row, list[dict[str, Any]]]:
    aggregate = connection.execute(
        f"""
        SELECT COUNT(*) AS count,
               MIN(e.created_at) AS first_interaction_at,
               MAX(e.created_at) AS last_interaction_at
        FROM events e
        WHERE e.event_id != ? AND {identity_clause}
        """,
        (event_id, identity_value),
    ).fetchone()
    rows = connection.execute(
        f"""
        SELECT e.event_id, e.username, e.created_at, e.conversation_id,
               e.payload_json, r.disposition, r.stance, r.stance_detail
        FROM events e
        LEFT JOIN event_resolutions r ON r.event_id = e.event_id
        WHERE e.event_id != ? AND {identity_clause}
        ORDER BY e.created_at DESC, CAST(e.event_id AS INTEGER) DESC
        LIMIT ?
        """,
        (event_id, identity_value, limit),
    ).fetchall()
    interactions: list[dict[str, Any]] = []
    for row in rows:
        status_id = str(row["event_id"])
        payload = json.loads(str(row["payload_json"]))
        prior_username = row["username"]
        alex_rows = connection.execute(
            """
            SELECT status_id, url, exact_text, posted_at
            FROM conversation_turns
            WHERE parent_status_id = ? AND actor = 'alex'
            ORDER BY posted_at, CAST(status_id AS INTEGER)
            LIMIT 3
            """,
            (status_id,),
        ).fetchall()
        interactions.append(
            {
                "status_id": status_id,
                "url": (
                    f"https://x.com/{prior_username}/status/{status_id}"
                    if prior_username
                    else f"https://x.com/i/web/status/{status_id}"
                ),
                "created_at": row["created_at"],
                "conversation_id": row["conversation_id"],
                "disposition": row["disposition"],
                "stance": row["stance"],
                "stance_detail": row["stance_detail"],
                **_excerpt(str(payload.get("text") or "")),
                "alex_replies": [
                    {
                        "status_id": str(reply["status_id"]),
                        "url": str(reply["url"]),
                        "posted_at": reply["posted_at"],
                        **_excerpt(str(reply["exact_text"])),
                    }
                    for reply in alex_rows
                ],
            }
        )
    return aggregate, interactions


def _archive_commenter_memory(
    connection: sqlite3.Connection,
    author_id: str,
    *,
    limit: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    aggregate_row = connection.execute(
        """
        SELECT COUNT(*) AS count,
               MIN(post.posted_at) AS first_reply_at,
               MAX(post.posted_at) AS last_reply_at
        FROM archive_posts AS post
        LEFT JOIN conversation_turns AS turn
          ON turn.status_id = post.status_id
        WHERE post.counterparty_user_id = ?
          AND turn.status_id IS NULL
        """,
        (author_id,),
    ).fetchone()
    aggregate = {
        "count": int(aggregate_row["count"]),
        "first_reply_at": aggregate_row["first_reply_at"],
        "last_reply_at": aggregate_row["last_reply_at"],
    }
    archive_limit = min(limit, max(3, math.ceil(limit / 3)))
    rows = connection.execute(
        """
        SELECT post.status_id, post.parent_status_id, post.posted_at,
               post.exact_text, post.canonical_url,
               post.counterparty_username, post.source_member
        FROM archive_posts AS post
        LEFT JOIN conversation_turns AS turn
          ON turn.status_id = post.status_id
        WHERE post.counterparty_user_id = ?
          AND turn.status_id IS NULL
        ORDER BY post.posted_at DESC, CAST(post.status_id AS INTEGER) DESC
        LIMIT ?
        """,
        (author_id, archive_limit),
    ).fetchall()
    return aggregate, [
        {
            "status_id": str(reply["status_id"]),
            "parent_status_id": reply["parent_status_id"],
            "url": str(reply["canonical_url"]),
            "posted_at": reply["posted_at"],
            "counterparty_username": reply["counterparty_username"],
            "source_kind": "official_x_archive_alex_reply",
            "source_member": str(reply["source_member"]),
            **_excerpt(str(reply["exact_text"])),
        }
        for reply in rows
    ]


def _candidate_commenter_memory(
    connection: sqlite3.Connection,
    author_id: str,
    *,
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    total = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM candidate_public_posts
            WHERE subject_user_id = ?
            """,
            (author_id,),
        ).fetchone()[0]
    )
    rows = connection.execute(
        """
        SELECT post.status_id, post.created_at,
               post.exact_text AS candidate_text,
               post.canonical_url AS candidate_url,
               post.text_state, post.source_verification_state,
               post.source_file,
               verification.exact_text AS verified_exact_text,
               verification.canonical_url AS verified_url,
               verification.observed_at AS verified_at,
               verification.verification_method
        FROM candidate_public_posts AS post
        LEFT JOIN candidate_post_verifications AS verification
          ON verification.status_id = post.status_id
        WHERE post.subject_user_id = ?
        ORDER BY post.created_at DESC, CAST(post.status_id AS INTEGER) DESC
        LIMIT ?
        """,
        (author_id, min(limit, 3)),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for post in rows:
        verified = post["verified_exact_text"] is not None
        text = (
            str(post["verified_exact_text"])
            if verified
            else str(post["candidate_text"] or "")
        )
        result.append(
            {
                "status_id": str(post["status_id"]),
                "url": str(
                    post["verified_url"] if verified else post["candidate_url"]
                ),
                "created_at": post["created_at"],
                "text_state": str(post["text_state"]),
                "source_verification_state": str(
                    post["source_verification_state"]
                ),
                "source_file": str(post["source_file"]),
                "verification_method": post["verification_method"],
                "verified_at": post["verified_at"],
                "usable_as_evidence": verified,
                **_excerpt(text),
            }
        )
    return total, result


def commenter_history_for_event(
    connection: sqlite3.Connection,
    event_id: str,
    *,
    limit: int,
) -> dict[str, Any]:
    if limit <= 0:
        raise ValueError("Commenter history limit must be positive")
    event_id = watcher_validation.validate_status_id(str(event_id), "event_id")
    current, identity_clause, identity_value, identity_kind = _commenter_identity(
        connection,
        event_id,
    )
    if identity_clause is None or identity_value is None or identity_kind is None:
        return _empty_commenter_history(event_id)

    aggregate, interactions = _prior_interactions(
        connection,
        event_id,
        identity_clause=identity_clause,
        identity_value=identity_value,
        limit=limit,
    )
    archive_aggregate = {
        "count": 0,
        "first_reply_at": None,
        "last_reply_at": None,
    }
    archive_replies: list[dict[str, Any]] = []
    candidate_total = 0
    candidate_posts: list[dict[str, Any]] = []
    author_id = current["author_id"]
    if author_id:
        archive_aggregate, archive_replies = _archive_commenter_memory(
            connection,
            str(author_id),
            limit=limit,
        )
        candidate_total, candidate_posts = _candidate_commenter_memory(
            connection,
            str(author_id),
            limit=limit,
        )
    return {
        "event_id": event_id,
        "identity_kind": identity_kind,
        "author_id": str(author_id) if author_id else None,
        "username": current["username"],
        "total_prior_interactions": int(aggregate["count"]),
        "first_interaction_at": aggregate["first_interaction_at"],
        "last_interaction_at": aggregate["last_interaction_at"],
        "returned_interactions": len(interactions),
        "interactions": interactions,
        "total_prior_archive_alex_replies": archive_aggregate["count"],
        "first_archive_alex_reply_at": archive_aggregate["first_reply_at"],
        "last_archive_alex_reply_at": archive_aggregate["last_reply_at"],
        "returned_archive_alex_replies": len(archive_replies),
        "archive_alex_replies": archive_replies,
        "total_candidate_public_posts": candidate_total,
        "returned_candidate_public_posts": len(candidate_posts),
        "candidate_public_posts": candidate_posts,
        "candidate_memory_contract": CANDIDATE_MEMORY_CONTRACT,
    }

def _identity_memory_rows(
    connection: sqlite3.Connection,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    event_row = connection.execute(
        """
        SELECT COUNT(*) AS event_count,
               COUNT(DISTINCT author_id) AS stable_author_count,
               COUNT(DISTINCT lower(username)) AS username_count,
               SUM(
                   CASE
                       WHEN COALESCE(author_id, '') = ''
                         OR COALESCE(username, '') = ''
                       THEN 1 ELSE 0
                   END
               ) AS missing_identity_count
        FROM events
        """
    ).fetchone()
    turn_row = connection.execute(
        """
        SELECT SUM(
                   CASE
                       WHEN actor IN ('user', 'target')
                        AND COALESCE(author, '') = ''
                       THEN 1 ELSE 0
                   END
               ) AS missing_user_author_count
        FROM conversation_turns
        """
    ).fetchone()
    return event_row, turn_row


def _archive_memory_rows(
    connection: sqlite3.Connection,
    user_id: str,
) -> tuple[sqlite3.Row, sqlite3.Row, int]:
    import_row = connection.execute(
        """
        SELECT COUNT(*) AS import_count,
               COALESCE(SUM(post_count), 0) AS declared_post_count,
               COALESCE(SUM(inserted_post_count), 0) AS inserted_post_count,
               SUM(
                   CASE WHEN account_user_id <> ? THEN 1 ELSE 0 END
               ) AS owner_mismatch_count,
               SUM(
                   CASE
                       WHEN post_count <= 0
                         OR inserted_post_count < 0
                         OR inserted_post_count > post_count
                       THEN 1 ELSE 0
                   END
               ) AS invalid_count_count
        FROM archive_imports
        """,
        (user_id,),
    ).fetchone()
    post_row = connection.execute(
        """
        SELECT COUNT(*) AS post_count,
               SUM(
                   CASE WHEN author_id <> ? THEN 1 ELSE 0 END
               ) AS owner_mismatch_count,
               SUM(
                   CASE
                       WHEN canonical_url <>
                            'https://x.com/i/web/status/' || status_id
                         OR length(payload_sha256) <> 64
                       THEN 1 ELSE 0
                   END
               ) AS invalid_record_count
        FROM archive_posts
        """,
        (user_id,),
    ).fetchone()
    alias_mismatch = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM archive_imports i
            LEFT JOIN archive_account_aliases a
              ON a.account_user_id = i.account_user_id
             AND lower(a.username) = lower(i.username)
            WHERE a.account_user_id IS NULL
            """
        ).fetchone()[0]
    )
    return import_row, post_row, alias_mismatch


def _candidate_memory_rows(
    connection: sqlite3.Connection,
) -> tuple[sqlite3.Row, sqlite3.Row, int]:
    import_row = connection.execute(
        """
        SELECT COUNT(*) AS import_count,
               COALESCE(SUM(inserted_record_count), 0) AS inserted_count,
               SUM(
                   CASE
                       WHEN record_count <= 0
                         OR inserted_record_count < 0
                         OR inserted_record_count > record_count
                       THEN 1 ELSE 0
                   END
               ) AS invalid_count_count
        FROM candidate_corpus_imports
        """
    ).fetchone()
    post_row = connection.execute(
        """
        SELECT COUNT(*) AS post_count,
               SUM(
                   CASE
                       WHEN post.subject_user_id <> import.subject_user_id
                         OR post.trust_state <> 'unverified_candidate'
                         OR length(post.payload_sha256) <> 64
                       THEN 1 ELSE 0
                   END
               ) AS invalid_record_count
        FROM candidate_public_posts post
        JOIN candidate_corpus_imports import
          ON import.id = post.first_import_id
        """
    ).fetchone()
    verification_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM candidate_post_verifications"
        ).fetchone()[0]
    )
    return import_row, post_row, verification_count


def _current_memory_errors(
    *,
    integrity_rows: list[str],
    foreign_key_violations: int,
    configured_user_id: str | None,
    expected_user_id: str,
    event_row: sqlite3.Row,
    turn_row: sqlite3.Row,
    initial_audit: dict[str, Any],
    candidate_row: sqlite3.Row,
    candidate_post_row: sqlite3.Row,
) -> list[str]:
    errors: list[str] = []
    checks = (
        (integrity_rows != ["ok"], "sqlite_integrity_check_failed"),
        (bool(foreign_key_violations), "foreign_key_violations"),
        (configured_user_id != expected_user_id, "configured_user_id_mismatch"),
        (
            bool(int(event_row["missing_identity_count"] or 0)),
            "events_missing_stable_identity",
        ),
        (
            bool(int(turn_row["missing_user_author_count"] or 0)),
            "history_turns_missing_author",
        ),
        (not initial_audit["complete"], "initial_audit_incomplete"),
        (
            not initial_audit["history_complete"],
            "resolution_history_incomplete",
        ),
        (not initial_audit["invariant_ok"], "resolution_invariant_failed"),
        (
            bool(int(candidate_row["invalid_count_count"] or 0)),
            "candidate_import_counts_invalid",
        ),
        (
            bool(int(candidate_post_row["invalid_record_count"] or 0)),
            "candidate_records_invalid",
        ),
        (
            int(candidate_row["inserted_count"] or 0)
            != int(candidate_post_row["post_count"] or 0),
            "candidate_inserted_count_mismatch",
        ),
    )
    return [code for failed, code in checks if failed]


def _archive_memory_errors(
    archive_row: sqlite3.Row,
    archive_post_row: sqlite3.Row,
    *,
    alias_mismatch: int,
) -> list[str]:
    post_count = int(archive_post_row["post_count"] or 0)
    checks = (
        (
            bool(int(archive_row["owner_mismatch_count"] or 0)),
            "archive_import_owner_mismatch",
        ),
        (
            bool(int(archive_post_row["owner_mismatch_count"] or 0)),
            "archive_post_owner_mismatch",
        ),
        (
            bool(int(archive_row["invalid_count_count"] or 0)),
            "archive_import_counts_invalid",
        ),
        (
            bool(int(archive_post_row["invalid_record_count"] or 0)),
            "archive_records_invalid",
        ),
        (bool(alias_mismatch), "archive_account_alias_missing"),
        (
            int(archive_row["inserted_post_count"] or 0) != post_count,
            "archive_inserted_count_mismatch",
        ),
    )
    return [code for failed, code in checks if failed]


def memory_audit(
    config: Any,
    connection: sqlite3.Connection,
    *,
    get_meta: Any,
    initial_audit_status: Any,
) -> dict[str, Any]:
    integrity_rows = [
        str(row[0])
        for row in connection.execute("PRAGMA integrity_check").fetchall()
    ]
    foreign_key_violations = len(
        connection.execute("PRAGMA foreign_key_check").fetchall()
    )
    configured_user_id = get_meta(connection, "configured_user_id")
    initial_audit = initial_audit_status(connection)
    history = watcher_history.history_status(connection)
    event_row, turn_row = _identity_memory_rows(connection)
    archive_row, archive_post_row, archive_alias_mismatch = _archive_memory_rows(
        connection,
        config.user_id,
    )
    candidate_row, candidate_post_row, verification_count = (
        _candidate_memory_rows(connection)
    )
    current_errors = _current_memory_errors(
        integrity_rows=integrity_rows,
        foreign_key_violations=foreign_key_violations,
        configured_user_id=configured_user_id,
        expected_user_id=config.user_id,
        event_row=event_row,
        turn_row=turn_row,
        initial_audit=initial_audit,
        candidate_row=candidate_row,
        candidate_post_row=candidate_post_row,
    )
    archive_errors = _archive_memory_errors(
        archive_row,
        archive_post_row,
        alias_mismatch=archive_alias_mismatch,
    )
    archive_import_count = int(archive_row["import_count"] or 0)
    archive_post_count = int(archive_post_row["post_count"] or 0)
    current_memory_ok = not current_errors and not archive_errors
    archive_ready = (
        archive_import_count > 0
        and archive_post_count > 0
        and not archive_errors
    )
    final_complete = current_memory_ok and archive_ready
    return {
        "status": (
            "complete"
            if final_complete
            else "archive_pending"
            if current_memory_ok
            else "invalid"
        ),
        "schema_version": watcher_constants.SCHEMA_VERSION,
        "current_memory_ok": current_memory_ok,
        "archive_ready": archive_ready,
        "final_complete": final_complete,
        "errors": current_errors,
        "archive_errors": archive_errors,
        "database": {
            "integrity_check": integrity_rows,
            "foreign_key_violations": foreign_key_violations,
        },
        "identity": {
            "configured_user_id": configured_user_id,
            "expected_user_id": config.user_id,
            "events": int(event_row["event_count"]),
            "stable_authors": int(event_row["stable_author_count"]),
            "usernames": int(event_row["username_count"]),
            "missing_event_identities": int(
                event_row["missing_identity_count"] or 0
            ),
            "missing_history_authors": int(
                turn_row["missing_user_author_count"] or 0
            ),
        },
        "history": {**history, "initial_audit": initial_audit},
        "archive": {
            "import_count": archive_import_count,
            "declared_post_count": int(
                archive_row["declared_post_count"] or 0
            ),
            "inserted_post_count": int(
                archive_row["inserted_post_count"] or 0
            ),
            "post_count": archive_post_count,
            "account_alias_mismatch": archive_alias_mismatch,
            "direct_messages_imported": 0,
        },
        "candidate_corpus": {
            "import_count": int(candidate_row["import_count"] or 0),
            "inserted_count": int(candidate_row["inserted_count"] or 0),
            "post_count": int(candidate_post_row["post_count"] or 0),
            "verified_post_count": verification_count,
            "unverified_posts_usable_as_evidence": 0,
        },
    }
