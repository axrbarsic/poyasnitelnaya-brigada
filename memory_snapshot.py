#!/usr/bin/env python3
"""Create a transactionally consistent, self-verifying X memory snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import xmention_watcher as watcher


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def create_memory_snapshot(
    config: watcher.Config,
    connection: sqlite3.Connection,
    *,
    output_root: Path,
) -> dict[str, Any]:
    live_audit = watcher.memory_audit(config, connection)
    if not live_audit["current_memory_ok"]:
        raise ValueError(
            "Refusing to snapshot invalid memory: "
            + ", ".join(
                [
                    *live_audit["errors"],
                    *live_audit["archive_errors"],
                ]
            )
        )
    output_root.mkdir(parents=True, exist_ok=True)
    created_at = watcher.isoformat()
    snapshot_name = watcher.utc_now().strftime("%Y%m%dT%H%M%S.%fZ")
    final_directory = output_root / snapshot_name
    if final_directory.exists():
        raise FileExistsError(f"Snapshot already exists: {final_directory}")
    temporary_directory = Path(
        tempfile.mkdtemp(prefix=".memory-snapshot-", dir=output_root)
    )
    try:
        database_path = temporary_directory / "watcher.sqlite3"
        destination = _snapshot_connection(database_path)
        try:
            connection.backup(destination)
        finally:
            destination.close()

        snapshot = _snapshot_connection(database_path)
        try:
            integrity = [
                str(row[0])
                for row in snapshot.execute("PRAGMA integrity_check").fetchall()
            ]
            foreign_key_violations = len(
                snapshot.execute("PRAGMA foreign_key_check").fetchall()
            )
            if integrity != ["ok"] or foreign_key_violations:
                raise ValueError(
                    "Snapshot SQLite validation failed: "
                    f"integrity={integrity}, "
                    f"foreign_key_violations={foreign_key_violations}"
                )
            snapshot_audit = watcher.memory_audit(config, snapshot)
            if snapshot_audit != live_audit:
                raise ValueError(
                    "Snapshot memory audit does not match the live source"
                )
            history_path = temporary_directory / "conversation-history.jsonl"
            resolutions_path = (
                temporary_directory / "initial-audit-resolutions.jsonl"
            )
            watcher.export_history(snapshot, history_path)
            watcher.export_audit_resolutions(snapshot, resolutions_path)
        finally:
            snapshot.close()

        durable_files = [
            database_path,
            history_path,
            resolutions_path,
        ]
        manifest = {
            "format_version": 1,
            "created_at": created_at,
            "configured_user_id": config.user_id,
            "schema_version": watcher.SCHEMA_VERSION,
            "memory_audit": live_audit,
            "files": {
                path.name: {
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
                for path in durable_files
            },
        }
        manifest_path = temporary_directory / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary_directory.replace(final_directory)
        return {
            "status": "created",
            "snapshot": str(final_directory),
            "created_at": created_at,
            "configured_user_id": config.user_id,
            "files": manifest["files"],
            "manifest": str(final_directory / "manifest.json"),
        }
    except Exception:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a consistent, self-verifying X memory snapshot.",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--output-root",
        type=Path,
        help="Default: var/snapshots next to the config file.",
    )
    args = parser.parse_args()
    config = watcher.load_config(args.config)
    output_root = (
        args.output_root.expanduser().resolve()
        if args.output_root
        else config.source_path.parent / "var" / "snapshots"
    )
    connection = watcher.connect_database(config.database)
    try:
        result = create_memory_snapshot(
            config,
            connection,
            output_root=output_root,
        )
    finally:
        connection.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
