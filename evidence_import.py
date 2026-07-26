#!/usr/bin/env python3
"""Import legacy Browser-owner evidence into one manifested canonical tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

IGNORED_NAMES = {".DS_Store"}
IGNORED_DIRECTORY_NAMES = {"__pycache__"}
FORBIDDEN_FILE_NAMES = {
    "config.json",
    "config.toml",
    "cookies",
    "cookies.json",
    "credentials",
    "credentials.json",
    "localstorage",
    "local_storage",
    "secrets",
    "secrets.json",
    "sessionstorage",
    "session_storage",
}
FORBIDDEN_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
SECRET_PATTERNS = (
    re.compile(
        rb"(?i)(api[_-]?key|access[_-]?token|bearer[_-]?token|"
        rb"client[_-]?secret|password)\s*[=:]\s*[\"']?[^\s\"']{8,}"
    ),
    re.compile(rb"\bgh[opsu]_[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        rb"(?i)authorization\s*:\s*bearer\s+"
        rb"[A-Za-z0-9._~+/=-]{8,}"
    ),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
)
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
STATUS_URL_PATTERN = re.compile(
    r"^https://x\.com/axrbarsic/status/([0-9]{1,19})/?$"
)
CANONICAL_OUTPUT_ROOT = (
    Path(__file__).resolve().parent / "var/evidence/browser-owner"
)


def isoformat() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sensitive_path_reason(relative: Path) -> str | None:
    for part in relative.parts:
        lowered = part.lower()
        if lowered == ".env" or lowered.startswith(".env."):
            return "environment file"
        if lowered in FORBIDDEN_FILE_NAMES:
            return "sensitive filename"
    if relative.suffix.lower() in FORBIDDEN_SUFFIXES:
        return "private-key container"
    return None


def contains_potential_secret(path: Path) -> bool:
    carry = b""
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            window = carry + chunk
            if any(pattern.search(window) for pattern in SECRET_PATTERNS):
                return True
            carry = window[-1024:]
    return False


def collect_source_files(source: Path) -> list[Path]:
    if not source.is_dir():
        raise ValueError(f"Evidence source is not a directory: {source}")
    files: list[Path] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in IGNORED_DIRECTORY_NAMES for part in relative.parts):
            continue
        if path.name in IGNORED_NAMES:
            continue
        if path.is_symlink():
            raise ValueError(f"Symlinks are not allowed in evidence: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"Unsupported evidence entry: {relative}")
        sensitive_reason = sensitive_path_reason(relative)
        if sensitive_reason is not None:
            raise ValueError(
                f"Sensitive filename is not allowed ({sensitive_reason}): "
                f"{relative}"
            )
        if contains_potential_secret(path):
            raise ValueError(
                f"Potential secret detected in evidence: {relative}"
            )
        files.append(path)
    if not files:
        raise ValueError("Evidence source contains no importable files")
    return files


def build_file_manifest(source: Path, files: list[Path]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    total_bytes = 0
    for path in files:
        size = path.stat().st_size
        total_bytes += size
        records.append(
            {
                "path": path.relative_to(source).as_posix(),
                "bytes": size,
                "sha256": sha256(path),
            }
        )
    canonical = json.dumps(
        records,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "files": records,
        "file_count": len(records),
        "total_bytes": total_bytes,
        "source_fingerprint": hashlib.sha256(canonical).hexdigest(),
    }


def load_existing_manifest(destination: Path) -> dict[str, Any] | None:
    manifest_path = destination / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def audit_evidence(destination: Path) -> dict[str, Any]:
    destination = destination.expanduser().resolve()
    manifest = load_existing_manifest(destination)
    errors: list[str] = []
    if manifest is None:
        return {
            "complete": False,
            "status": "invalid",
            "destination": str(destination),
            "errors": ["manifest_missing_or_invalid"],
        }
    try:
        expected_files = manifest["files"]
        expected_fingerprint = str(manifest["source_fingerprint"])
        expected_count = int(manifest["file_count"])
        expected_bytes = int(manifest["total_bytes"])
        if not isinstance(expected_files, list):
            raise TypeError("files must be an array")
    except (KeyError, TypeError, ValueError):
        return {
            "complete": False,
            "status": "invalid",
            "destination": str(destination),
            "errors": ["manifest_contract_invalid"],
        }

    try:
        collected = [
            path
            for path in collect_source_files(destination)
            if path.relative_to(destination).as_posix() != "manifest.json"
        ]
        actual = build_file_manifest(destination, collected)
    except ValueError as error:
        errors.append(f"evidence_tree_invalid:{error}")
        actual = {
            "files": [],
            "file_count": 0,
            "total_bytes": 0,
            "source_fingerprint": "",
        }

    if actual["files"] != expected_files:
        errors.append("file_manifest_mismatch")
    if actual["file_count"] != expected_count:
        errors.append("file_count_mismatch")
    if actual["total_bytes"] != expected_bytes:
        errors.append("total_bytes_mismatch")
    if actual["source_fingerprint"] != expected_fingerprint:
        errors.append("source_fingerprint_mismatch")
    return {
        "complete": not errors,
        "status": "complete" if not errors else "invalid",
        "destination": str(destination),
        "file_count": actual["file_count"],
        "total_bytes": actual["total_bytes"],
        "source_fingerprint": actual["source_fingerprint"],
        "errors": errors,
    }


def _jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"Required evidence JSONL is missing: {path.name}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"Invalid JSONL in {path.name} line {line_number}"
            ) from error
        if not isinstance(payload, dict):
            raise ValueError(
                f"JSONL record must be an object in {path.name} "
                f"line {line_number}"
            )
        records.append(payload)
    return records


def _source_file_from_record(
    destination: Path,
    record: dict[str, Any],
) -> Path:
    source_value = str(record.get("source_path") or "").strip()
    if not source_value:
        raise ValueError(
            f"Published event {record.get('event_id')} has no source_path"
        )
    source_path = Path(source_value).expanduser().resolve()
    try:
        source_path.relative_to(destination)
    except ValueError as error:
        raise ValueError(
            f"Published source_path is outside evidence: {source_path}"
        ) from error
    if not source_path.is_file() or source_path.is_symlink():
        raise ValueError(
            f"Published source_path is not a regular evidence file: "
            f"{source_path}"
        )
    return source_path


def verify_runtime_evidence(
    destination: Path,
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    ledger = _jsonl_records(destination / "run-ledger.jsonl")
    _jsonl_records(destination / "conversation-history.jsonl")
    dispositions: dict[str, dict[str, Any]] = {}
    published_events: set[str] = set()
    for record in ledger:
        event_id = str(record.get("event_id") or "").strip()
        if record.get("event") == "publication_verified":
            if event_id:
                published_events.add(event_id)
        if record.get("event") != "initial_audit_disposition":
            continue
        if not event_id.isdigit():
            raise ValueError("Evidence disposition has an invalid event_id")
        dispositions[event_id] = record
    if not dispositions:
        raise ValueError("Evidence contains no final disposition records")
    missing_dispositions = sorted(published_events - set(dispositions))
    if missing_dispositions:
        raise ValueError(
            "Verified publications lack final dispositions: "
            + ", ".join(missing_dispositions)
        )

    proof: list[dict[str, Any]] = []
    ordered_dispositions = sorted(
        dispositions.items(),
        key=lambda item: int(item[0]),
    )
    for event_id, record in ordered_dispositions:
        resolution = connection.execute(
            """
            SELECT disposition, reason, reply_url, blocker_code, stance,
                   stance_detail, confidence, media_meaning, evidence_json,
                   resolved_at
            FROM event_resolutions
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        if resolution is None:
            raise ValueError(
                f"Evidence event is not durably resolved in SQLite: {event_id}"
            )
        expected_fields = (
            "disposition",
            "reason",
            "reply_url",
            "blocker_code",
            "stance",
            "stance_detail",
            "confidence",
            "media_meaning",
        )
        mismatches = [
            field
            for field in expected_fields
            if resolution[field] != record.get(field)
        ]
        if mismatches:
            raise ValueError(
                f"Evidence resolution mismatch for {event_id}: "
                + ", ".join(mismatches)
            )
        ledger_evidence = sorted(
            {
                str(item).strip()
                for item in record.get("evidence", [])
                if str(item).strip()
            }
        )
        stored_evidence = sorted(
            json.loads(resolution["evidence_json"] or "[]")
        )
        if ledger_evidence != stored_evidence:
            raise ValueError(
                f"Evidence source list mismatch for event {event_id}"
            )
        user_turn = connection.execute(
            """
            SELECT 1
            FROM conversation_turns
            WHERE status_id = ?
              AND actor IN ('user', 'target')
            LIMIT 1
            """,
            (event_id,),
        ).fetchone()
        if user_turn is None:
            raise ValueError(
                f"Evidence event has no exact user history turn: {event_id}"
            )

        disposition = str(resolution["disposition"])
        reply_url = resolution["reply_url"]
        alex_turn_present = False
        if disposition == "published":
            match = STATUS_URL_PATTERN.fullmatch(str(reply_url or ""))
            if match is None:
                raise ValueError(
                    f"Published event has a non-canonical reply_url: {event_id}"
                )
            reply_status_id = match.group(1)
            alex_turn = connection.execute(
                """
                SELECT exact_text
                FROM conversation_turns
                WHERE status_id = ?
                  AND parent_status_id = ?
                  AND actor = 'alex'
                  AND url = ?
                """,
                (reply_status_id, event_id, reply_url),
            ).fetchone()
            if alex_turn is None:
                raise ValueError(
                    f"Published event has no matching Alex child turn: {event_id}"
                )
            source_path = _source_file_from_record(destination, record)
            source_text = source_path.read_text(encoding="utf-8")
            expected_sha = str(record.get("source_sha256") or "")
            if sha256(source_path) != expected_sha:
                raise ValueError(
                    f"Published source SHA-256 mismatch for event {event_id}"
                )
            terminal_lf_only = (
                source_text.endswith("\n")
                and source_text[:-1] == alex_turn["exact_text"]
            )
            published_text = (
                source_text[:-1] if terminal_lf_only else source_text
            )
            allowed_lengths = {len(published_text)}
            if terminal_lf_only:
                allowed_lengths.add(len(source_text))
            recorded_length = int(
                record.get("source_code_points") or -1
            )
            if recorded_length not in allowed_lengths:
                raise ValueError(
                    f"Published source length mismatch for event {event_id}"
                )
            if published_text != alex_turn["exact_text"]:
                raise ValueError(
                    f"Published source differs from Alex history: {event_id}"
                )
            alex_turn_present = True
        elif reply_url is not None:
            raise ValueError(
                f"Non-published event unexpectedly has reply_url: {event_id}"
            )

        proof.append(
            {
                "event_id": event_id,
                "disposition": disposition,
                "reply_url": reply_url,
                "resolved_at": resolution["resolved_at"],
                "user_turn_present": True,
                "alex_turn_present": alex_turn_present,
            }
        )
    return proof


def finalize_runtime_evidence(
    *,
    destination: Path,
    connection: sqlite3.Connection,
    output_root: Path = CANONICAL_OUTPUT_ROOT,
) -> dict[str, Any]:
    requested = destination.expanduser()
    if requested.is_symlink():
        raise ValueError(
            f"Evidence destination must not be a symlink: {requested}"
        )
    destination = requested.resolve()
    output_root = output_root.expanduser().resolve()
    if destination.parent != output_root:
        raise ValueError(
            "Runtime evidence must be a direct child of the canonical "
            f"Browser evidence root: {output_root}"
        )
    if not LABEL_PATTERN.fullmatch(destination.name):
        raise ValueError("Runtime evidence directory has an invalid label")
    existing = load_existing_manifest(destination)
    if existing is not None:
        audit = audit_evidence(destination)
        if not audit["complete"]:
            raise ValueError(
                "Existing runtime evidence manifest is invalid: "
                + ", ".join(audit["errors"])
            )
        return {
            **audit,
            "status": "already_finalized",
        }

    resolution_proof = verify_runtime_evidence(destination, connection)
    files = collect_source_files(destination)
    source_manifest = build_file_manifest(destination, files)
    manifest = {
        "format_version": 1,
        "label": destination.name,
        "source": "runtime_browser_owner",
        "finalized_at": isoformat(),
        "resolution_proof": resolution_proof,
        **source_manifest,
    }
    manifest_path = destination / "manifest.json"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".manifest.",
        suffix=".tmp",
        dir=destination,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                manifest,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(manifest_path)
        audit = audit_evidence(destination)
        if not audit["complete"]:
            manifest_path.unlink(missing_ok=True)
            raise ValueError(
                "Finalized runtime evidence failed its own audit: "
                + ", ".join(audit["errors"])
            )
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        **audit,
        "status": "finalized",
        "resolution_count": len(resolution_proof),
    }


def import_evidence(
    *,
    source: Path,
    output_root: Path,
    label: str,
) -> dict[str, Any]:
    if not LABEL_PATTERN.fullmatch(label):
        raise ValueError(
            "Evidence label must contain only letters, digits, dot, "
            "underscore, and hyphen"
        )
    source = source.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    destination = output_root / label
    files = collect_source_files(source)
    source_manifest = build_file_manifest(source, files)

    if destination.exists():
        existing = load_existing_manifest(destination)
        existing_audit = audit_evidence(destination)
        if (
            existing is not None
            and existing_audit["complete"]
            and existing.get("source_fingerprint")
            == source_manifest["source_fingerprint"]
            and existing.get("file_count") == source_manifest["file_count"]
            and existing.get("total_bytes") == source_manifest["total_bytes"]
        ):
            return {
                "status": "already_imported",
                "destination": str(destination),
                **source_manifest,
            }
        raise FileExistsError(
            f"Evidence destination already exists with different content: "
            f"{destination}"
        )

    output_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{label}.", dir=output_root)
    )
    try:
        for source_path in files:
            relative = source_path.relative_to(source)
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target)

        copied_files = [
            temporary / record["path"]
            for record in source_manifest["files"]
        ]
        copied_manifest = build_file_manifest(temporary, copied_files)
        if copied_manifest != source_manifest:
            raise ValueError("Copied evidence differs from the source")

        manifest = {
            "format_version": 1,
            "label": label,
            "source": str(source),
            **source_manifest,
        }
        (temporary / "manifest.json").write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    return {
        "status": "imported",
        "destination": str(destination),
        **source_manifest,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--source", type=Path)
    action.add_argument("--audit", type=Path)
    action.add_argument("--finalize", type=Path)
    parser.add_argument("--label")
    parser.add_argument("--config", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.audit is not None:
        result = audit_evidence(args.audit)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["complete"] else 2
    if args.finalize is not None:
        if args.config is None:
            raise SystemExit("--config is required with --finalize")
        import xmention_watcher as watcher

        config = watcher.load_config(args.config)
        connection = sqlite3.connect(
            f"file:{config.database}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        try:
            result = finalize_runtime_evidence(
                destination=args.finalize,
                connection=connection,
            )
        finally:
            connection.close()
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.label is None:
        raise SystemExit("--label is required with --source")
    result = import_evidence(
        source=args.source,
        output_root=CANONICAL_OUTPUT_ROOT,
        label=args.label,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
