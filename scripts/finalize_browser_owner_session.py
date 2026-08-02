#!/usr/bin/env python3
"""Finalize one Browser-owner evidence session with one deterministic command."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import evidence_import
import xmention_watcher as watcher
from scripts import autopilot_dispatch


HISTORY_NAME = "conversation-history.jsonl"
LEDGER_NAME = "run-ledger.jsonl"
AGGREGATE_NAMES = (HISTORY_NAME, LEDGER_NAME)


def _canonical_json(record: dict[str, Any]) -> str:
    return json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Evidence JSONL is not a regular file: {path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"Invalid JSONL in {path} at line {line_number}"
            ) from error
        if not isinstance(record, dict):
            raise ValueError(
                f"JSONL record must be an object in {path} "
                f"at line {line_number}"
            )
        records.append(record)
    if not records:
        raise ValueError(f"Evidence JSONL contains no records: {path}")
    return records


def _atomic_write_records(path: Path, records: Sequence[dict[str, Any]]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(_canonical_json(record))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _status_ids_from_history(record: dict[str, Any]) -> set[str]:
    status_ids: set[str] = set()
    status_id = str(record.get("status_id") or "").strip()
    if status_id:
        status_ids.add(status_id)
    turns = record.get("turns")
    if isinstance(turns, list):
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            turn_status_id = str(turn.get("status_id") or "").strip()
            if turn_status_id:
                status_ids.add(turn_status_id)
    return status_ids


def _event_id_from_ledger(
    records: Sequence[dict[str, Any]],
    *,
    source: Path,
) -> str:
    dispositions = [
        record
        for record in records
        if record.get("event") == "initial_audit_disposition"
    ]
    if len(dispositions) != 1:
        raise ValueError(
            f"Expected exactly one final disposition in {source}, "
            f"found {len(dispositions)}"
        )
    event_id = str(dispositions[0].get("event_id") or "").strip()
    if not event_id.isdigit() or len(event_id) > 19:
        raise ValueError(f"Invalid disposition event_id in {source}")
    return event_id


def _collect_event_records(
    session_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    histories: list[dict[str, Any]] = []
    ledgers: list[dict[str, Any]] = []
    event_ids: list[str] = []
    seen_records: dict[tuple[str, str], Path] = {}

    for child in sorted(session_dir.iterdir(), key=lambda path: path.name):
        if child.name in AGGREGATE_NAMES or child.name == "manifest.json":
            continue
        if child.is_symlink():
            raise ValueError(f"Symlink is not allowed in evidence: {child}")
        if not child.is_dir():
            continue
        history_path = child / HISTORY_NAME
        ledger_path = child / LEDGER_NAME
        if not history_path.exists() and not ledger_path.exists():
            continue
        if not history_path.is_file() or not ledger_path.is_file():
            raise ValueError(
                f"Event evidence must contain both JSONL files: {child}"
            )
        history_records = _read_jsonl(history_path)
        ledger_records = _read_jsonl(ledger_path)
        event_id = _event_id_from_ledger(ledger_records, source=ledger_path)
        if child.name != event_id:
            raise ValueError(
                f"Event directory {child.name} does not match ledger "
                f"event_id {event_id}"
            )
        if event_id not in {
            status_id
            for record in history_records
            for status_id in _status_ids_from_history(record)
        }:
            raise ValueError(
                f"History in {child} does not contain its exact event turn"
            )
        if event_id in event_ids:
            raise ValueError(f"Duplicate event evidence: {event_id}")
        event_ids.append(event_id)

        for kind, source, records in (
            ("history", history_path, history_records),
            ("ledger", ledger_path, ledger_records),
        ):
            for record in records:
                fingerprint = _canonical_json(record)
                key = (kind, fingerprint)
                previous = seen_records.get(key)
                if previous is not None:
                    raise ValueError(
                        f"Duplicate {kind} record in {previous} and {source}"
                    )
                seen_records[key] = source
        histories.extend(history_records)
        ledgers.extend(ledger_records)

    if not event_ids:
        raise ValueError("Browser-owner session contains no event evidence")
    return histories, ledgers, event_ids


def _write_or_confirm_aggregate(
    path: Path,
    records: Sequence[dict[str, Any]],
) -> bool:
    if path.exists():
        existing = _read_jsonl(path)
        if list(existing) != list(records):
            raise ValueError(
                f"Existing aggregate differs from event evidence: {path}"
            )
        return False
    _atomic_write_records(path, records)
    return True


def _confirm_existing_aggregate(
    path: Path,
    records: Sequence[dict[str, Any]],
) -> None:
    if path.exists() and _read_jsonl(path) != list(records):
        raise ValueError(
            f"Existing aggregate differs from event evidence: {path}"
        )


def _validated_session_dir(
    config: watcher.Config,
    requested: Path,
) -> tuple[Path, Path]:
    if requested.is_symlink():
        raise ValueError(
            f"Evidence session directory must not be a symlink: {requested}"
        )
    session_dir = requested.expanduser().resolve()
    evidence_root = (
        config.source_path.parent / "var/evidence/browser-owner"
    ).resolve()
    if session_dir.parent != evidence_root:
        raise ValueError(
            "Evidence session must be a direct child of the canonical "
            f"Browser-owner root: {evidence_root}"
        )
    if not session_dir.is_dir():
        raise ValueError(f"Evidence session is not a directory: {session_dir}")
    if evidence_import.LABEL_PATTERN.fullmatch(session_dir.name) is None:
        raise ValueError("Evidence session has an invalid directory name")
    return session_dir, evidence_root


def _active_claim_event_ids(
    state_file: Path,
    *,
    claim_token: str,
) -> list[str]:
    state = autopilot_dispatch.load_state(state_file)
    owner = state.get("owner")
    if not isinstance(owner, dict) or owner.get("claim_token") != claim_token:
        raise ValueError(
            "Evidence session does not match the active autopilot claim"
        )
    event_ids = [str(value) for value in owner.get("event_ids", [])]
    if not event_ids or any(
        not event_id.isdigit() or len(event_id) > 19
        for event_id in event_ids
    ):
        raise ValueError("Active autopilot claim has invalid event IDs")
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("Active autopilot claim contains duplicate event IDs")
    return event_ids


def finalize_session(
    *,
    config_path: Path,
    requested_session_dir: Path,
) -> dict[str, Any]:
    config = watcher.load_config(config_path)
    session_dir, evidence_root = _validated_session_dir(
        config,
        requested_session_dir,
    )
    connection = watcher.connect_database(config.database)
    try:
        if (session_dir / "manifest.json").is_file():
            result = evidence_import.finalize_runtime_evidence(
                destination=session_dir,
                connection=connection,
                output_root=evidence_root,
            )
            return {
                "status": result["status"],
                "session_dir": str(session_dir),
                "manifest": result,
            }

        _, state_file = autopilot_dispatch.load_paths(config_path)
        with autopilot_dispatch.locked_state(state_file):
            expected_event_ids = _active_claim_event_ids(
                state_file,
                claim_token=session_dir.name,
            )
            histories, ledgers, evidence_event_ids = _collect_event_records(
                session_dir
            )
            if set(evidence_event_ids) != set(expected_event_ids):
                missing = sorted(set(expected_event_ids) - set(evidence_event_ids))
                unexpected = sorted(
                    set(evidence_event_ids) - set(expected_event_ids)
                )
                raise ValueError(
                    "Evidence event set does not match active claim: "
                    f"missing={missing}, unexpected={unexpected}"
                )
            aggregate_paths = (
                session_dir / HISTORY_NAME,
                session_dir / LEDGER_NAME,
            )
            for path, records in zip(
                aggregate_paths,
                (histories, ledgers),
                strict=True,
            ):
                _confirm_existing_aggregate(path, records)

            with tempfile.TemporaryDirectory(
                prefix=".handoff-sync.",
                dir=session_dir,
            ) as temporary_name:
                staging = Path(temporary_name)
                staging_history = staging / HISTORY_NAME
                staging_ledger = staging / LEDGER_NAME
                _atomic_write_records(staging_history, histories)
                _atomic_write_records(staging_ledger, ledgers)
                result = watcher.sync_browser_handoffs(
                    config,
                    connection,
                    history_path=staging_history,
                    ledger_path=staging_ledger,
                )

            current_histories, current_ledgers, current_event_ids = (
                _collect_event_records(session_dir)
            )
            if (
                current_histories != histories
                or current_ledgers != ledgers
                or current_event_ids != evidence_event_ids
            ):
                raise ValueError(
                    "Per-event evidence changed during finalization"
                )
            created_paths: list[Path] = []
            try:
                for path, records in zip(
                    aggregate_paths,
                    (histories, ledgers),
                    strict=True,
                ):
                    if _write_or_confirm_aggregate(path, records):
                        created_paths.append(path)
                manifest = evidence_import.finalize_runtime_evidence(
                    destination=session_dir,
                    connection=connection,
                    output_root=evidence_root,
                )
            except Exception:
                if not (session_dir / "manifest.json").exists():
                    for path in created_paths:
                        path.unlink(missing_ok=True)
                raise

        if not manifest.get("complete"):
            raise ValueError("Browser-owner evidence manifest is incomplete")
        result["evidence_manifest"] = manifest
        return {
            "status": "finalized",
            "session_dir": str(session_dir),
            "event_ids": sorted(evidence_event_ids, key=int),
            "history_records": len(histories),
            "ledger_records": len(ledgers),
            "sync": result,
        }
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--session-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = finalize_session(
            config_path=arguments.config,
            requested_session_dir=arguments.session_dir,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(
            json.dumps(
                {"status": "blocked", "message": str(error)},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
