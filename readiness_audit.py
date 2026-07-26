#!/usr/bin/env python3
"""Prove end-to-end readiness of durable X memory without mutating state."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import project_layout_audit
import restic_backup
import xmention_watcher as watcher


PROJECT_ROOT = Path(__file__).resolve().parent
HEX_64 = re.compile(r"^[0-9a-f]{64}$")


def read_only_memory_audit(config: watcher.Config) -> dict[str, Any]:
    if not config.database.is_file():
        raise ValueError("watcher_database_missing")
    connection = sqlite3.connect(
        f"{config.database.resolve().as_uri()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return watcher.memory_audit(config, connection)
    finally:
        connection.close()


def audit_latest_snapshot(
    plan: dict[str, Any],
    memory: dict[str, Any],
) -> dict[str, Any]:
    snapshots = list(plan.get("snapshots") or [])
    if not snapshots:
        return {
            "complete": False,
            "current": False,
            "errors": ["memory_snapshot_missing"],
        }
    latest = max(
        snapshots,
        key=lambda item: str(item.get("created_at") or ""),
    )
    errors: list[str] = []
    if latest.get("memory_audit") != memory:
        errors.append("latest_snapshot_does_not_match_current_memory")
    return {
        "complete": not errors,
        "current": not errors,
        "path": latest.get("snapshot"),
        "created_at": latest.get("created_at"),
        "errors": errors,
    }


def audit_snapshot_root(
    *,
    settings: restic_backup.BackupSettings,
    config: watcher.Config,
    memory: dict[str, Any],
) -> dict[str, Any]:
    if not settings.snapshot_root.is_dir():
        return audit_latest_snapshot({"snapshots": []}, memory)
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for entry in sorted(settings.snapshot_root.iterdir()):
        if entry.name.startswith("."):
            errors.append(f"temporary_snapshot_present:{entry.name}")
            continue
        audit = restic_backup.audit_memory_snapshot(entry, config)
        if not audit["complete"]:
            errors.extend(
                f"{entry.name}:{error}"
                for error in audit.get("errors") or []
            )
            continue
        records.append(audit)
    result = audit_latest_snapshot({"snapshots": records}, memory)
    if errors:
        result["complete"] = False
        result["current"] = False
        result["errors"] = list(result.get("errors") or []) + errors
    return result


def audit_remote_receipts(
    *,
    plan: dict[str, Any],
    backup_receipt: dict[str, Any] | None,
    restore_receipt: dict[str, Any] | None,
) -> dict[str, Any]:
    errors: list[str] = []
    expected_fingerprint = str(plan.get("plan_fingerprint") or "")
    expected_source_count = int(plan.get("source_count") or 0)
    snapshot_id: str | None = None

    if backup_receipt is None:
        errors.append("remote_backup_receipt_missing")
    else:
        snapshot_id = str(backup_receipt.get("snapshot_id") or "")
        backup_plan = backup_receipt.get("plan")
        backup_fingerprint = (
            str(backup_plan.get("plan_fingerprint") or "")
            if isinstance(backup_plan, dict)
            else ""
        )
        if not HEX_64.fullmatch(snapshot_id):
            errors.append("remote_backup_snapshot_id_invalid")
        if backup_fingerprint != expected_fingerprint:
            errors.append("remote_backup_plan_is_not_current")

    if restore_receipt is None:
        errors.append("restore_smoke_receipt_missing")
    else:
        restore_snapshot_id = str(restore_receipt.get("snapshot_id") or "")
        restore_fingerprint = str(
            restore_receipt.get("plan_fingerprint") or ""
        )
        restored_source_count = int(
            restore_receipt.get("restored_source_count") or 0
        )
        if restore_snapshot_id != snapshot_id:
            errors.append("restore_smoke_snapshot_mismatch")
        if restore_fingerprint != expected_fingerprint:
            errors.append("restore_smoke_plan_mismatch")
        if restored_source_count != expected_source_count:
            errors.append("restore_smoke_source_count_mismatch")

    return {
        "complete": not errors,
        "snapshot_id": snapshot_id,
        "plan_fingerprint": expected_fingerprint,
        "source_count": expected_source_count,
        "errors": errors,
    }


def aggregate_readiness(
    *,
    layout: dict[str, Any],
    memory: dict[str, Any],
    watcher_health: dict[str, Any],
    backup_plan: dict[str, Any],
    snapshot: dict[str, Any],
    remote_configured: bool,
    remote_configuration_error: str | None,
    remote_receipts: dict[str, Any],
) -> dict[str, Any]:
    blockers: list[str] = []
    if not layout.get("complete"):
        blockers.append("canonical_layout_invalid")
    if not memory.get("current_memory_ok"):
        initial_audit = (
            memory.get("history", {}).get("initial_audit", {})
            if isinstance(memory.get("history"), dict)
            else {}
        )
        memory_errors = set(memory.get("errors") or [])
        if (
            int(initial_audit.get("pending_events") or 0) > 0
            and memory_errors == {"initial_audit_incomplete"}
        ):
            blockers.append("response_queue_pending")
        else:
            blockers.append("current_memory_invalid")
    if not memory.get("archive_ready"):
        blockers.append("official_x_archive_pending")
    if not watcher_health.get("healthy"):
        blockers.append("watcher_unhealthy")
    if not backup_plan.get("complete"):
        blockers.append("local_backup_plan_invalid")
    if memory.get("archive_ready") and not (
        backup_plan.get("archive_vault")
        and backup_plan["archive_vault"].get("present")
    ):
        blockers.append("official_archive_missing_from_backup_plan")
    if not snapshot.get("current"):
        blockers.append("memory_snapshot_not_current")
    if not remote_configured:
        blockers.append("remote_backup_not_configured")
    if not remote_receipts.get("complete"):
        blockers.extend(
            str(error) for error in remote_receipts.get("errors") or []
        )
    blockers = list(dict.fromkeys(blockers))
    complete = not blockers
    return {
        "version": 1,
        "generated_at": watcher.isoformat(),
        "complete": complete,
        "status": "complete" if complete else "pending",
        "blockers": blockers,
        "components": {
            "canonical_layout": layout,
            "current_memory": memory,
            "watcher": watcher_health,
            "local_backup_plan": {
                "complete": bool(backup_plan.get("complete")),
                "errors": list(backup_plan.get("errors") or []),
                "plan_fingerprint": backup_plan.get("plan_fingerprint"),
                "source_count": backup_plan.get("source_count"),
                "snapshot_count": len(backup_plan.get("snapshots") or []),
                "evidence_count": len(backup_plan.get("evidence") or []),
                "archive_vault": backup_plan.get("archive_vault"),
            },
            "latest_memory_snapshot": snapshot,
            "remote_configuration": {
                "complete": remote_configured,
                "error": remote_configuration_error,
            },
            "remote_backup_and_restore": remote_receipts,
        },
    }


def run_readiness_audit(
    *,
    root: Path,
    config_path: Path,
    backup_settings_path: Path,
    installed_skill: Path | None,
) -> dict[str, Any]:
    root = root.resolve()
    layout = project_layout_audit.audit_layout(
        root=root,
        config_path=config_path,
        installed_skill=installed_skill,
        require_installed_skill=True,
    )
    config = watcher.load_config(config_path)
    memory = read_only_memory_audit(config)
    watcher_health = watcher.evaluate_health(config)
    settings = restic_backup.load_settings(
        backup_settings_path,
        project_root=root,
        require_restic=False,
        require_remote=False,
    )
    try:
        backup_plan = restic_backup.build_backup_plan(settings)
    except (OSError, ValueError) as error:
        backup_plan = {
            "complete": False,
            "errors": [str(error)],
            "plan_fingerprint": None,
            "source_count": 0,
            "snapshots": [],
            "evidence": [],
            "archive_vault": None,
        }
    snapshot = audit_snapshot_root(
        settings=settings,
        config=config,
        memory=memory,
    )

    remote_configured = True
    remote_configuration_error = None
    try:
        restic_backup.load_settings(
            backup_settings_path,
            project_root=root,
            require_restic=False,
            require_remote=True,
        )
    except (OSError, ValueError) as error:
        remote_configured = False
        remote_configuration_error = str(error)

    backup_receipt = None
    restore_receipt = None
    if backup_plan["complete"]:
        try:
            backup_receipt = restic_backup.load_backup_receipt(settings)
        except (OSError, ValueError):
            pass
        try:
            restore_receipt = restic_backup.load_restore_receipt(settings)
        except (OSError, ValueError):
            pass
    remote_receipts = audit_remote_receipts(
        plan=backup_plan,
        backup_receipt=backup_receipt,
        restore_receipt=restore_receipt,
    )
    return aggregate_readiness(
        layout=layout,
        memory=memory,
        watcher_health=watcher_health,
        backup_plan=backup_plan,
        snapshot=snapshot,
        remote_configured=remote_configured,
        remote_configuration_error=remote_configuration_error,
        remote_receipts=remote_receipts,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail-closed end-to-end readiness audit.",
    )
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--backup-settings", type=Path)
    parser.add_argument(
        "--installed-skill",
        type=Path,
        default=Path.home() / ".codex/skills/x-twitter-operator",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    config_path = (
        args.config.resolve()
        if args.config is not None
        else root / "config.json"
    )
    backup_settings_path = (
        args.backup_settings.resolve()
        if args.backup_settings is not None
        else root / "backup.json"
    )
    try:
        result = run_readiness_audit(
            root=root,
            config_path=config_path,
            backup_settings_path=backup_settings_path,
            installed_skill=args.installed_skill,
        )
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
    ) as error:
        result = {
            "version": 1,
            "generated_at": watcher.isoformat(),
            "complete": False,
            "status": "invalid",
            "blockers": ["readiness_audit_failed"],
            "error": str(error),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
