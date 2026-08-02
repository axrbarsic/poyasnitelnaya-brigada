#!/usr/bin/env python3
"""Validate durable completion proof for one Browser-owner claim."""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import evidence_import
import xmention_watcher as watcher

if "browser_owner_evidence" in sys.modules:
    browser_owner_evidence = sys.modules["browser_owner_evidence"]
else:
    from scripts import browser_owner_evidence


def require_durable_resolutions(
    config_path: Path,
    event_ids: list[str],
) -> None:
    requested = sorted(set(event_ids), key=int)
    if not requested:
        raise ValueError("completed claim must contain event IDs")
    config = watcher.load_config(config_path)
    placeholders = ",".join("?" for _ in requested)
    with closing(
        sqlite3.connect(f"file:{config.database}?mode=ro", uri=True)
    ) as connection:
        rows = connection.execute(
            f"SELECT event_id FROM event_resolutions "
            f"WHERE event_id IN ({placeholders})",
            requested,
        ).fetchall()
    resolved = sorted((str(row[0]) for row in rows), key=int)
    if resolved != requested:
        missing = sorted(set(requested) - set(resolved), key=int)
        raise ValueError(
            "events lack durable resolution: " + ", ".join(missing)
        )


def verify_evidence_manifest(
    config_path: Path,
    *,
    claim_token: str,
    event_ids: list[str],
) -> dict[str, Any]:
    if evidence_import.LABEL_PATTERN.fullmatch(claim_token) is None:
        raise ValueError("claim token is not a valid evidence label")
    evidence_root = (
        config_path.resolve().parent / "var/evidence/browser-owner"
    )
    session_dir = evidence_root / claim_token
    if session_dir.is_symlink():
        raise ValueError("claim evidence directory must not be a symlink")
    audit = evidence_import.audit_evidence(session_dir)
    if not audit.get("complete"):
        errors = audit.get("errors") or ["manifest_missing_or_invalid"]
        raise ValueError(
            "claim evidence manifest is incomplete: "
            + ", ".join(str(value) for value in errors)
        )
    manifest = evidence_import.load_existing_manifest(session_dir)
    if manifest is None:
        raise ValueError("claim evidence manifest is missing")
    if (
        manifest.get("source") != "runtime_browser_owner"
        or manifest.get("label") != claim_token
    ):
        raise ValueError("claim evidence manifest identity is invalid")
    file_records = manifest.get("files")
    if not isinstance(file_records, list):
        raise ValueError("claim evidence manifest files are invalid")
    paths = {
        str(record.get("path"))
        for record in file_records
        if isinstance(record, dict)
    }
    required_paths = {
        browser_owner_evidence.HISTORY_NAME,
        browser_owner_evidence.LEDGER_NAME,
    }
    if not required_paths.issubset(paths):
        raise ValueError("claim evidence manifest lacks aggregate JSONL files")
    proof = manifest.get("resolution_proof")
    if not isinstance(proof, list) or not all(
        isinstance(record, dict) for record in proof
    ):
        raise ValueError("claim evidence resolution proof is invalid")
    proof_event_ids = [str(record.get("event_id") or "") for record in proof]
    if any(not value.isdigit() for value in proof_event_ids):
        raise ValueError("claim evidence resolution proof has invalid event IDs")
    expected_event_ids = sorted(set(event_ids), key=int)
    if (
        len(proof_event_ids) != len(set(proof_event_ids))
        or sorted(proof_event_ids, key=int) != expected_event_ids
    ):
        raise ValueError(
            "claim evidence event set does not match completed claim"
        )
    return {
        "status": "complete",
        "session_dir": str(session_dir.resolve()),
        "event_ids": expected_event_ids,
        "file_count": audit["file_count"],
        "source_fingerprint": audit["source_fingerprint"],
    }
