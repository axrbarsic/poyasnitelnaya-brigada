#!/usr/bin/env python3
"""Quarantined search index for externally collected public X posts."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import xmention_watcher as watcher


UNVERIFIED_TRUST_STATE = "unverified_candidate"


@dataclass(frozen=True)
class CandidatePost:
    status_id: str
    subject_user_id: str
    account_handle: str
    created_at: str | None
    exact_text: str | None
    text_state: str
    canonical_url: str
    source_verification_state: str
    source_file: str
    payload_sha256: str

    def database_values(self, import_id: int) -> tuple[Any, ...]:
        return (
            self.status_id,
            import_id,
            self.subject_user_id,
            self.account_handle,
            self.created_at,
            self.exact_text,
            self.text_state,
            self.canonical_url,
            self.source_verification_state,
            UNVERIFIED_TRUST_STATE,
            self.source_file,
            self.payload_sha256,
        )


@dataclass(frozen=True)
class CandidatePlan:
    corpus_fingerprint: str
    subject_user_id: str
    expected_handle: str
    source_name: str
    posts: tuple[CandidatePost, ...]
    text_state_counts: dict[str, int]
    source_verification_counts: dict[str, int]

    def summary(self) -> dict[str, Any]:
        return {
            "status": "planned",
            "corpus_fingerprint": self.corpus_fingerprint,
            "subject_user_id": self.subject_user_id,
            "expected_handle": self.expected_handle,
            "source_name": self.source_name,
            "record_count": len(self.posts),
            "text_state_counts": self.text_state_counts,
            "source_verification_counts": self.source_verification_counts,
            "trust_state": UNVERIFIED_TRUST_STATE,
            "auto_usable_as_evidence": False,
        }


def _required_numeric_id(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result.isdigit():
        raise ValueError(f"{field} must be a numeric X ID")
    return result


def _normalize_handle(value: Any, field: str) -> str:
    result = str(value or "").strip().lstrip("@")
    if not result:
        raise ValueError(f"{field} is required")
    return result


def _normalize_timestamp(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Candidate created_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_status_url(value: Any, status_id: str) -> str:
    url = str(value or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {
        "x.com",
        "www.x.com",
        "twitter.com",
        "www.twitter.com",
    }:
        raise ValueError(f"Candidate {status_id} has a non-X canonical URL")
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 3 or path_parts[-2] != "status":
        raise ValueError(f"Candidate {status_id} has an invalid status URL")
    if path_parts[-1] != status_id:
        raise ValueError(f"Candidate {status_id} URL points to another status")
    return f"https://x.com/i/web/status/{status_id}"


def _payload_sha256(fields: dict[str, Any]) -> str:
    encoded = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _candidate_post(
    record: dict[str, Any],
    *,
    subject_user_id: str,
    expected_handle: str,
    source_file: str,
) -> CandidatePost:
    record_type = str(record.get("record_type") or "")
    if not record_type.startswith("x_post"):
        raise ValueError(f"{source_file} contains a non-x_post record")
    status_id = _required_numeric_id(record.get("status_id"), "status_id")
    account_handle = _normalize_handle(
        record.get("account_handle"),
        "account_handle",
    )
    if account_handle.lower() != expected_handle.lower():
        raise ValueError(
            f"Candidate {status_id} belongs to @{account_handle}, "
            f"expected @{expected_handle}"
        )
    canonical_url = _validate_status_url(
        record.get("canonical_url"),
        status_id,
    )
    text_state = str(record.get("text_state") or "unknown").strip()
    exact_value = record.get("text_verbatim")
    exact_text = None if exact_value is None else str(exact_value)
    if text_state == "full_as_returned" and exact_text is None:
        raise ValueError(f"Candidate {status_id} lacks full text")
    source_verification_state = str(
        record.get("verification_state") or "unknown"
    ).strip()
    created_at = _normalize_timestamp(record.get("created_at_utc"))
    immutable_fields = {
        "status_id": status_id,
        "subject_user_id": subject_user_id,
        "account_handle": account_handle.lower(),
        "created_at": created_at,
        "exact_text": exact_text,
        "text_state": text_state,
        "canonical_url": canonical_url,
        "source_verification_state": source_verification_state,
    }
    return CandidatePost(
        status_id=status_id,
        subject_user_id=subject_user_id,
        account_handle=account_handle,
        created_at=created_at,
        exact_text=exact_text,
        text_state=text_state,
        canonical_url=canonical_url,
        source_verification_state=source_verification_state,
        source_file=source_file,
        payload_sha256=_payload_sha256(immutable_fields),
    )


def _input_paths(values: Iterable[Path]) -> tuple[Path, ...]:
    paths = tuple(sorted(path.expanduser().resolve() for path in values))
    if not paths:
        raise ValueError("At least one candidate JSONL file is required")
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise ValueError("Candidate JSONL file not found: " + ", ".join(missing))
    return paths


def plan_candidate_import(
    paths: Iterable[Path],
    *,
    subject_user_id: str,
    expected_handle: str,
) -> CandidatePlan:
    source_paths = _input_paths(paths)
    stable_user_id = _required_numeric_id(subject_user_id, "subject_user_id")
    handle = _normalize_handle(expected_handle, "expected_handle")
    posts: list[CandidatePost] = []
    seen_ids: set[str] = set()
    digest = hashlib.sha256()
    for path in source_paths:
        raw = path.read_bytes()
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
        for line_number, line in enumerate(
            raw.decode("utf-8-sig").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path.name}:{line_number} is not an object")
            post = _candidate_post(
                value,
                subject_user_id=stable_user_id,
                expected_handle=handle,
                source_file=path.name,
            )
            if post.status_id in seen_ids:
                raise ValueError(
                    f"Duplicate candidate status ID: {post.status_id}"
                )
            seen_ids.add(post.status_id)
            posts.append(post)
    posts.sort(key=lambda post: (post.created_at or "", int(post.status_id)))
    text_state_counts: dict[str, int] = {}
    verification_counts: dict[str, int] = {}
    for post in posts:
        text_state_counts[post.text_state] = (
            text_state_counts.get(post.text_state, 0) + 1
        )
        verification_counts[post.source_verification_state] = (
            verification_counts.get(post.source_verification_state, 0) + 1
        )
    return CandidatePlan(
        corpus_fingerprint=digest.hexdigest(),
        subject_user_id=stable_user_id,
        expected_handle=handle,
        source_name=",".join(path.name for path in source_paths),
        posts=tuple(posts),
        text_state_counts=dict(sorted(text_state_counts.items())),
        source_verification_counts=dict(sorted(verification_counts.items())),
    )


def _candidate_conflicts(
    row: sqlite3.Row,
    post: CandidatePost,
) -> list[str]:
    expected = {
        "subject_user_id": post.subject_user_id,
        "account_handle": post.account_handle,
        "created_at": post.created_at,
        "exact_text": post.exact_text,
        "text_state": post.text_state,
        "canonical_url": post.canonical_url,
        "source_verification_state": post.source_verification_state,
        "trust_state": UNVERIFIED_TRUST_STATE,
        "payload_sha256": post.payload_sha256,
    }
    return [
        field
        for field, value in expected.items()
        if row[field] != value
    ]


def apply_candidate_import(
    connection: sqlite3.Connection,
    plan: CandidatePlan,
) -> dict[str, Any]:
    existing_import = connection.execute(
        """
        SELECT id, imported_at, inserted_record_count
        FROM candidate_corpus_imports
        WHERE corpus_fingerprint = ?
        """,
        (plan.corpus_fingerprint,),
    ).fetchone()
    if existing_import is not None:
        return {
            **plan.summary(),
            "status": "already_imported",
            "import_id": int(existing_import["id"]),
            "inserted_record_count": int(
                existing_import["inserted_record_count"]
            ),
            "imported_at": existing_import["imported_at"],
        }
    existing_ids: set[str] = set()
    for post in plan.posts:
        row = connection.execute(
            "SELECT * FROM candidate_public_posts WHERE status_id = ?",
            (post.status_id,),
        ).fetchone()
        if row is None:
            continue
        conflicts = _candidate_conflicts(row, post)
        if conflicts:
            raise ValueError(
                "Append-only candidate conflict for status "
                f"{post.status_id}: {', '.join(conflicts)}"
            )
        existing_ids.add(post.status_id)
    inserted_count = len(plan.posts) - len(existing_ids)
    imported_at = watcher.isoformat()
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO candidate_corpus_imports(
                corpus_fingerprint, subject_user_id, expected_handle,
                source_name, imported_at, record_count,
                inserted_record_count
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan.corpus_fingerprint,
                plan.subject_user_id,
                plan.expected_handle,
                plan.source_name,
                imported_at,
                len(plan.posts),
                inserted_count,
            ),
        )
        import_id = int(cursor.lastrowid)
        for post in plan.posts:
            if post.status_id in existing_ids:
                continue
            connection.execute(
                """
                INSERT INTO candidate_public_posts(
                    status_id, first_import_id, subject_user_id,
                    account_handle, created_at, exact_text, text_state,
                    canonical_url, source_verification_state, trust_state,
                    source_file, payload_sha256
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                post.database_values(import_id),
            )
    return {
        **plan.summary(),
        "status": "imported",
        "import_id": import_id,
        "inserted_record_count": inserted_count,
        "existing_record_count": len(existing_ids),
        "imported_at": imported_at,
    }


def candidate_history_for_event(
    connection: sqlite3.Connection,
    event_id: str,
    *,
    limit: int,
) -> dict[str, Any]:
    if limit <= 0:
        raise ValueError("Candidate history limit must be positive")
    status_id = _required_numeric_id(event_id, "event_id")
    event = connection.execute(
        """
        SELECT author_id, username
        FROM events
        WHERE event_id = ?
        """,
        (status_id,),
    ).fetchone()
    if event is None:
        raise KeyError(f"Unknown event {status_id}")
    author_id = str(event["author_id"] or "")
    if not author_id:
        return {
            "event_id": status_id,
            "author_id": None,
            "username": event["username"],
            "total_candidates": 0,
            "returned_candidates": 0,
            "candidates": [],
        }
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
        SELECT post.status_id, post.account_handle, post.created_at,
               post.exact_text AS candidate_text, post.text_state,
               post.canonical_url, post.source_verification_state,
               post.trust_state, post.source_file,
               verification.exact_text AS verified_exact_text,
               verification.canonical_url AS verified_url,
               verification.observed_at, verification.verification_method,
               verification.verified_by
        FROM candidate_public_posts AS post
        LEFT JOIN candidate_post_verifications AS verification
          ON verification.status_id = post.status_id
        WHERE post.subject_user_id = ?
        ORDER BY post.created_at DESC, CAST(post.status_id AS INTEGER) DESC
        LIMIT ?
        """,
        (author_id, limit),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        verified = row["verified_exact_text"] is not None
        candidates.append(
            {
                "status_id": str(row["status_id"]),
                "account_handle": str(row["account_handle"]),
                "created_at": row["created_at"],
                "candidate_text": row["candidate_text"],
                "text_state": str(row["text_state"]),
                "candidate_url": str(row["canonical_url"]),
                "source_verification_state": str(
                    row["source_verification_state"]
                ),
                "trust_state": (
                    "verified_public_x"
                    if verified
                    else str(row["trust_state"])
                ),
                "source_file": str(row["source_file"]),
                "verified_exact_text": row["verified_exact_text"],
                "verified_url": row["verified_url"],
                "verified_at": row["observed_at"],
                "verification_method": row["verification_method"],
                "verified_by": row["verified_by"],
                "usable_as_evidence": verified,
            }
        )
    return {
        "event_id": status_id,
        "author_id": author_id,
        "username": event["username"],
        "total_candidates": total,
        "returned_candidates": len(candidates),
        "candidates": candidates,
        "contract": (
            "Unverified candidates are search hints only. Verify the exact "
            "live X post before quoting or using it as evidence."
        ),
    }


def record_candidate_verification(
    connection: sqlite3.Connection,
    *,
    status_id: str,
    exact_text: str,
    canonical_url: str,
    observed_at: str,
    verification_method: str,
    verified_by: str,
) -> dict[str, Any]:
    stable_status_id = _required_numeric_id(status_id, "status_id")
    stable_url = _validate_status_url(canonical_url, stable_status_id)
    normalized_observed_at = _normalize_timestamp(observed_at)
    if normalized_observed_at is None:
        raise ValueError("observed_at is required")
    candidate = connection.execute(
        """
        SELECT exact_text
        FROM candidate_public_posts
        WHERE status_id = ?
        """,
        (stable_status_id,),
    ).fetchone()
    if candidate is None:
        raise KeyError(f"Unknown candidate {stable_status_id}")
    exact_sha = hashlib.sha256(exact_text.encode("utf-8")).hexdigest()
    existing = connection.execute(
        """
        SELECT *
        FROM candidate_post_verifications
        WHERE status_id = ?
        """,
        (stable_status_id,),
    ).fetchone()
    values = (
        exact_text,
        stable_url,
        normalized_observed_at,
        verification_method,
        verified_by,
        exact_sha,
    )
    if existing is not None:
        existing_values = (
            existing["exact_text"],
            existing["canonical_url"],
            existing["observed_at"],
            existing["verification_method"],
            existing["verified_by"],
            existing["exact_text_sha256"],
        )
        if existing_values != values:
            raise ValueError(
                "Append-only candidate verification conflict for "
                f"{stable_status_id}"
            )
        status = "already_verified"
    else:
        with connection:
            connection.execute(
                """
                INSERT INTO candidate_post_verifications(
                    status_id, exact_text, canonical_url, observed_at,
                    verification_method, verified_by, exact_text_sha256
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (stable_status_id, *values),
            )
        status = "verified"
    return {
        "status": status,
        "status_id": stable_status_id,
        "canonical_url": stable_url,
        "observed_at": normalized_observed_at,
        "verification_method": verification_method,
        "verified_by": verified_by,
        "candidate_text_match": candidate["exact_text"] == exact_text,
        "exact_text_sha256": exact_sha,
    }


def _print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage quarantined external public X candidate records.",
    )
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)

    import_command = commands.add_parser("import")
    import_command.add_argument("--input", type=Path, nargs="+", required=True)
    import_command.add_argument("--subject-user-id", required=True)
    import_command.add_argument("--handle", required=True)
    import_command.add_argument("--apply", action="store_true")

    history_command = commands.add_parser("history")
    history_command.add_argument("event_id")
    history_command.add_argument("--limit", type=int, default=50)

    verify_command = commands.add_parser("verify")
    verify_command.add_argument("status_id")
    verify_command.add_argument("--url", required=True)
    verify_command.add_argument("--exact-text-file", type=Path, required=True)
    verify_command.add_argument("--observed-at", required=True)
    verify_command.add_argument(
        "--method",
        choices=("live_x_dom", "official_x_api"),
        default="live_x_dom",
    )
    verify_command.add_argument(
        "--verified-by",
        default="sol_browser_owner",
    )

    args = parser.parse_args()
    config = watcher.load_config(args.config)
    if args.command == "import":
        plan = plan_candidate_import(
            args.input,
            subject_user_id=args.subject_user_id,
            expected_handle=args.handle,
        )
        result: dict[str, Any] = {
            **plan.summary(),
            "mode": "dry_run",
        }
        if args.apply:
            connection = watcher.connect_database(config.database)
            try:
                result = {
                    **apply_candidate_import(connection, plan),
                    "mode": "apply",
                }
            finally:
                connection.close()
        _print_json(result)
        return 0
    connection = watcher.connect_database(config.database)
    try:
        if args.command == "history":
            _print_json(
                candidate_history_for_event(
                    connection,
                    args.event_id,
                    limit=args.limit,
                )
            )
            return 0
        exact_text = args.exact_text_file.read_text(encoding="utf-8")
        _print_json(
            record_candidate_verification(
                connection,
                status_id=args.status_id,
                exact_text=exact_text,
                canonical_url=args.url,
                observed_at=args.observed_at,
                verification_method=args.method,
                verified_by=args.verified_by,
            )
        )
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
