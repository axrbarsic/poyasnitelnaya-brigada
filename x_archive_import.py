#!/usr/bin/env python3
"""Safe, idempotent import of public posts from an official X data archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import xmention_watcher as watcher


ACCOUNT_MEMBER = "account.js"
PUBLIC_POST_MEMBER = re.compile(r"^tweets?(?:-part\d+)?\.js$", re.IGNORECASE)
PRIVATE_MEMBER_PREFIXES = (
    "direct-message",
    "direct-messages",
    "message",
)


@dataclass(frozen=True)
class ArchivePost:
    status_id: str
    author_id: str
    username_at_import: str
    posted_at: str
    exact_text: str
    parent_status_id: str | None
    counterparty_user_id: str | None
    counterparty_username: str | None
    conversation_id: str | None
    canonical_url: str
    source_member: str
    payload_sha256: str

    def database_values(self, import_id: int) -> tuple[Any, ...]:
        return (
            self.status_id,
            import_id,
            self.author_id,
            self.username_at_import,
            self.posted_at,
            self.exact_text,
            self.parent_status_id,
            self.counterparty_user_id,
            self.counterparty_username,
            self.conversation_id,
            self.canonical_url,
            self.source_member,
            self.payload_sha256,
        )


@dataclass(frozen=True)
class ArchivePlan:
    source_name: str
    archive_fingerprint: str
    account_user_id: str
    username: str
    posts: tuple[ArchivePost, ...]
    public_members: tuple[str, ...]
    private_members_ignored: tuple[str, ...]

    def summary(self) -> dict[str, Any]:
        reply_count = sum(post.parent_status_id is not None for post in self.posts)
        counterparty_count = len(
            {
                post.counterparty_user_id
                for post in self.posts
                if post.counterparty_user_id
            }
        )
        return {
            "status": "planned",
            "source_name": self.source_name,
            "archive_fingerprint": self.archive_fingerprint,
            "account_user_id": self.account_user_id,
            "username": self.username,
            "post_count": len(self.posts),
            "reply_count": reply_count,
            "stable_counterparty_count": counterparty_count,
            "public_members": list(self.public_members),
            "private_members_ignored": list(self.private_members_ignored),
            "direct_messages_imported": 0,
        }


def _member_basename(name: str) -> str:
    return PurePosixPath(name.replace("\\", "/")).name


def _is_private_member(name: str) -> bool:
    basename = _member_basename(name).lower()
    return basename.endswith(".js") and basename.startswith(PRIVATE_MEMBER_PREFIXES)


def _selected_member_names(names: Iterable[str]) -> tuple[list[str], list[str]]:
    public: list[str] = []
    private: list[str] = []
    for name in names:
        basename = _member_basename(name)
        if basename.lower() == ACCOUNT_MEMBER or PUBLIC_POST_MEMBER.fullmatch(basename):
            public.append(name)
        elif _is_private_member(name):
            private.append(name)
    return sorted(public), sorted(private)


def _read_selected_members(path: Path) -> tuple[dict[str, bytes], tuple[str, ...]]:
    if path.is_dir():
        file_names = [
            file.relative_to(path).as_posix()
            for file in path.rglob("*")
            if file.is_file()
        ]
        selected, private = _selected_member_names(file_names)
        return {
            name: (path / PurePosixPath(name)).read_bytes()
            for name in selected
        }, tuple(private)
    if path.is_file() and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            selected, private = _selected_member_names(archive.namelist())
            return {
                name: archive.read(name)
                for name in selected
            }, tuple(private)
    raise ValueError("Archive path must be an extracted X archive directory or ZIP file")


def _parse_js_array(raw: bytes, member: str) -> list[Any]:
    text = raw.decode("utf-8-sig")
    assignment = text.find("=")
    if assignment < 0:
        raise ValueError(f"{member} is not an X archive JavaScript assignment")
    payload = text[assignment + 1 :].strip()
    if payload.endswith(";"):
        payload = payload[:-1].rstrip()
    value = json.loads(payload)
    if not isinstance(value, list):
        raise ValueError(f"{member} must contain a JSON array")
    return value


def _digits_or_none(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    result = str(value).strip()
    if not result.isdigit():
        raise ValueError(f"{field} must be a numeric X ID")
    return result


def _required_digits(value: Any, field: str) -> str:
    result = _digits_or_none(value, field)
    if result is None:
        raise ValueError(f"{field} is required")
    return result


def _normalize_timestamp(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Tweet created_at is required")
    parsed: datetime
    try:
        parsed = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Tweet created_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _account_identity(
    members: dict[str, bytes],
) -> tuple[str, str, str]:
    account_members = [
        name for name in members if _member_basename(name).lower() == ACCOUNT_MEMBER
    ]
    if len(account_members) != 1:
        raise ValueError("Archive must contain exactly one data/account.js")
    member = account_members[0]
    records = _parse_js_array(members[member], member)
    identities: set[tuple[str, str]] = set()
    for entry in records:
        if not isinstance(entry, dict):
            continue
        account = entry.get("account")
        if not isinstance(account, dict):
            continue
        user_id = _required_digits(
            account.get("accountId") or account.get("account_id"),
            "accountId",
        )
        username = str(account.get("username") or "").strip().lstrip("@")
        if not username:
            raise ValueError("Archive account username is required")
        identities.add((user_id, username))
    if len(identities) != 1:
        raise ValueError("Archive account identity is missing or ambiguous")
    user_id, username = identities.pop()
    return user_id, username, member


def _post_payload_sha256(fields: dict[str, Any]) -> str:
    encoded = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _archive_post(
    entry: Any,
    *,
    account_user_id: str,
    username: str,
    source_member: str,
) -> ArchivePost:
    if not isinstance(entry, dict) or not isinstance(entry.get("tweet"), dict):
        raise ValueError(f"{source_member} contains an invalid tweet record")
    tweet = entry["tweet"]
    status_id = _required_digits(tweet.get("id_str") or tweet.get("id"), "tweet id")
    parent_status_id = _digits_or_none(
        tweet.get("in_reply_to_status_id_str")
        or tweet.get("in_reply_to_status_id"),
        "in_reply_to_status_id",
    )
    counterparty_user_id = _digits_or_none(
        tweet.get("in_reply_to_user_id_str")
        or tweet.get("in_reply_to_user_id"),
        "in_reply_to_user_id",
    )
    conversation_id = _digits_or_none(
        tweet.get("conversation_id_str") or tweet.get("conversation_id"),
        "conversation_id",
    )
    counterparty_username = str(
        tweet.get("in_reply_to_screen_name") or ""
    ).strip().lstrip("@") or None
    exact_text = str(tweet.get("full_text") or tweet.get("text") or "")
    posted_at = _normalize_timestamp(tweet.get("created_at"))
    stable_url = f"https://x.com/i/web/status/{status_id}"
    immutable_fields = {
        "status_id": status_id,
        "author_id": account_user_id,
        "posted_at": posted_at,
        "exact_text": exact_text,
        "parent_status_id": parent_status_id,
        "counterparty_user_id": counterparty_user_id,
        "counterparty_username": counterparty_username,
        "conversation_id": conversation_id,
    }
    return ArchivePost(
        status_id=status_id,
        author_id=account_user_id,
        username_at_import=username,
        posted_at=posted_at,
        exact_text=exact_text,
        parent_status_id=parent_status_id,
        counterparty_user_id=counterparty_user_id,
        counterparty_username=counterparty_username,
        conversation_id=conversation_id,
        canonical_url=stable_url,
        source_member=source_member,
        payload_sha256=_post_payload_sha256(immutable_fields),
    )


def plan_archive_import(path: Path, *, expected_user_id: str) -> ArchivePlan:
    members, private_members = _read_selected_members(path)
    account_user_id, username, account_member = _account_identity(members)
    expected = _required_digits(expected_user_id, "configured user_id")
    if account_user_id != expected:
        raise ValueError(
            "Archive account does not match configured user_id: "
            f"{account_user_id} != {expected}"
        )
    post_members = [
        name
        for name in members
        if PUBLIC_POST_MEMBER.fullmatch(_member_basename(name))
    ]
    if not post_members:
        raise ValueError("Archive contains no supported data/tweets.js members")
    posts: list[ArchivePost] = []
    seen_ids: set[str] = set()
    for member in sorted(post_members):
        for entry in _parse_js_array(members[member], member):
            post = _archive_post(
                entry,
                account_user_id=account_user_id,
                username=username,
                source_member=member,
            )
            if post.status_id in seen_ids:
                raise ValueError(f"Duplicate tweet ID inside archive: {post.status_id}")
            seen_ids.add(post.status_id)
            posts.append(post)
    posts.sort(key=lambda item: (item.posted_at, int(item.status_id)))
    digest = hashlib.sha256()
    for member in sorted((account_member, *post_members)):
        digest.update(member.encode("utf-8"))
        digest.update(b"\0")
        digest.update(members[member])
        digest.update(b"\0")
    return ArchivePlan(
        source_name=path.name,
        archive_fingerprint=digest.hexdigest(),
        account_user_id=account_user_id,
        username=username,
        posts=tuple(posts),
        public_members=tuple(sorted((account_member, *post_members))),
        private_members_ignored=private_members,
    )


def _existing_post_conflicts(
    row: sqlite3.Row,
    post: ArchivePost,
) -> list[str]:
    expected = {
        "author_id": post.author_id,
        "posted_at": post.posted_at,
        "exact_text": post.exact_text,
        "parent_status_id": post.parent_status_id,
        "counterparty_user_id": post.counterparty_user_id,
        "counterparty_username": post.counterparty_username,
        "conversation_id": post.conversation_id,
        "payload_sha256": post.payload_sha256,
    }
    return [
        field
        for field, value in expected.items()
        if row[field] != value
    ]


def apply_archive_import(
    connection: sqlite3.Connection,
    plan: ArchivePlan,
) -> dict[str, Any]:
    existing_import = connection.execute(
        """
        SELECT id, post_count, inserted_post_count, imported_at
        FROM archive_imports
        WHERE archive_fingerprint = ?
        """,
        (plan.archive_fingerprint,),
    ).fetchone()
    if existing_import is not None:
        return {
            **plan.summary(),
            "status": "already_imported",
            "import_id": int(existing_import["id"]),
            "inserted_post_count": int(existing_import["inserted_post_count"]),
            "imported_at": existing_import["imported_at"],
        }
    existing_rows: dict[str, sqlite3.Row] = {}
    for post in plan.posts:
        row = connection.execute(
            "SELECT * FROM archive_posts WHERE status_id = ?",
            (post.status_id,),
        ).fetchone()
        if row is None:
            continue
        conflicts = _existing_post_conflicts(row, post)
        if conflicts:
            raise ValueError(
                "Append-only archive conflict for tweet "
                f"{post.status_id}: {', '.join(conflicts)}"
            )
        existing_rows[post.status_id] = row
    inserted_count = len(plan.posts) - len(existing_rows)
    imported_at = watcher.isoformat()
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO archive_imports(
                archive_fingerprint, account_user_id, username, source_name,
                imported_at, post_count, inserted_post_count
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan.archive_fingerprint,
                plan.account_user_id,
                plan.username,
                plan.source_name,
                imported_at,
                len(plan.posts),
                inserted_count,
            ),
        )
        import_id = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO archive_account_aliases(
                account_user_id, username, first_import_id, last_import_id
            ) VALUES(?, ?, ?, ?)
            ON CONFLICT(account_user_id, username)
            DO UPDATE SET last_import_id = excluded.last_import_id
            """,
            (plan.account_user_id, plan.username, import_id, import_id),
        )
        for post in plan.posts:
            if post.status_id in existing_rows:
                continue
            connection.execute(
                """
                INSERT INTO archive_posts(
                    status_id, first_import_id, author_id, username_at_import,
                    posted_at, exact_text, parent_status_id,
                    counterparty_user_id, counterparty_username,
                    conversation_id, canonical_url, source_member,
                    payload_sha256
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                post.database_values(import_id),
            )
    return {
        **plan.summary(),
        "status": "imported",
        "import_id": import_id,
        "inserted_post_count": inserted_count,
        "existing_post_count": len(existing_rows),
        "imported_at": imported_at,
    }


def archive_status(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT COUNT(*) AS import_count,
               COALESCE(SUM(inserted_post_count), 0) AS inserted_post_count
        FROM archive_imports
        """
    ).fetchone()
    post_row = connection.execute(
        """
        SELECT COUNT(*) AS post_count,
               SUM(CASE WHEN parent_status_id IS NOT NULL THEN 1 ELSE 0 END)
                   AS reply_count,
               COUNT(DISTINCT counterparty_user_id) AS counterparty_count,
               MIN(posted_at) AS first_post_at,
               MAX(posted_at) AS last_post_at
        FROM archive_posts
        """
    ).fetchone()
    return {
        "schema_version": watcher.SCHEMA_VERSION,
        "import_count": int(row["import_count"]),
        "inserted_post_count": int(row["inserted_post_count"]),
        "post_count": int(post_row["post_count"]),
        "reply_count": int(post_row["reply_count"] or 0),
        "stable_counterparty_count": int(post_row["counterparty_count"] or 0),
        "first_post_at": post_row["first_post_at"],
        "last_post_at": post_row["last_post_at"],
        "direct_messages_imported": 0,
    }


def _write_report(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def require_outside_project(
    path: Path,
    *,
    project_root: Path,
    label: str,
) -> Path:
    resolved = path.expanduser().resolve()
    canonical_root = project_root.expanduser().resolve()
    try:
        resolved.relative_to(canonical_root)
    except ValueError:
        return resolved
    raise ValueError(
        f"{label} must be stored outside the canonical project root: "
        f"{canonical_root}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely import public posts from an official X archive.",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write validated public posts to SQLite. Default is dry-run.",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    project_root = config_path.parent
    archive_path = require_outside_project(
        args.archive,
        project_root=project_root,
        label="archive",
    )
    report_path = (
        require_outside_project(
            args.report,
            project_root=project_root,
            label="report",
        )
        if args.report is not None
        else None
    )
    config = watcher.load_config(config_path)
    plan = plan_archive_import(
        archive_path,
        expected_user_id=config.user_id,
    )
    result: dict[str, Any] = {
        **plan.summary(),
        "mode": "dry_run",
    }
    if args.apply:
        connection = watcher.connect_database(config.database)
        try:
            result = {
                **apply_archive_import(connection, plan),
                "mode": "apply",
                "archive_status": archive_status(connection),
            }
        finally:
            connection.close()
    if report_path is not None:
        _write_report(report_path, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
