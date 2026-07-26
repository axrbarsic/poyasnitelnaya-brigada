#!/usr/bin/env python3
"""Two-phase intake for an official X archive stored outside the project."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import memory_snapshot
import x_archive_import
import xmention_watcher as watcher


FORMAT_VERSION = 1
PLAN_FILE_NAME = "intake-plan.json"
RECEIPT_FILE_NAME = "intake-receipt.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError(f"Report directory must not be a symlink: {path.parent}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _plan_fingerprint(payload: dict[str, Any]) -> str:
    stable = {
        "format_version": payload["format_version"],
        "archive": payload["archive"],
        "import_plan": payload["import_plan"],
    }
    encoded = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _external_report_directory(
    archive: Path,
    *,
    project_root: Path,
    report_directory: Path | None,
) -> Path:
    requested = (
        report_directory.expanduser()
        if report_directory is not None
        else archive.parent / "reports"
    )
    if requested.is_symlink():
        raise ValueError(f"Report directory must not be a symlink: {requested}")
    selected = requested.resolve()
    x_archive_import.require_outside_project(
        selected,
        project_root=project_root,
        label="report directory",
    )
    if selected.exists() and not selected.is_dir():
        raise ValueError(f"Report directory is not a directory: {selected}")
    if selected.is_symlink():
        raise ValueError(f"Report directory must not be a symlink: {selected}")
    return selected


def _validate_archive(path: Path, *, project_root: Path) -> Path:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ValueError(f"Archive must not be a symlink: {requested}")
    archive = x_archive_import.require_outside_project(
        requested,
        project_root=project_root,
        label="archive",
    )
    if not archive.is_file():
        raise ValueError(f"Archive is not a regular file: {archive}")
    if archive.suffix.lower() != ".zip":
        raise ValueError("Official X archive intake requires the original ZIP file")
    if archive.stat().st_size <= 0:
        raise ValueError("Official X archive ZIP is empty")
    if not zipfile.is_zipfile(archive):
        raise ValueError("Official X archive is not a valid ZIP file")
    return archive


def build_intake_plan(
    config_path: Path,
    archive_path: Path,
    *,
    report_directory: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    config_file = config_path.expanduser().resolve()
    project_root = config_file.parent
    archive = _validate_archive(archive_path, project_root=project_root)
    reports = _external_report_directory(
        archive,
        project_root=project_root,
        report_directory=report_directory,
    )
    config = watcher.load_config(config_file)
    import_plan = x_archive_import.plan_archive_import(
        archive,
        expected_user_id=config.user_id,
    )
    payload: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "mode": "dry_run",
        "created_at": watcher.isoformat(),
        "project_root": str(project_root),
        "archive": {
            "path": str(archive),
            "bytes": archive.stat().st_size,
            "sha256": _sha256(archive),
        },
        "import_plan": import_plan.summary(),
    }
    payload["plan_fingerprint"] = _plan_fingerprint(payload)
    plan_path = reports / PLAN_FILE_NAME
    _atomic_json(plan_path, payload)
    return payload, plan_path


def _load_matching_plan(
    plan_path: Path,
    current: dict[str, Any],
) -> dict[str, Any]:
    if not plan_path.is_file():
        raise ValueError(
            "Dry-run intake plan is missing. Run without --apply first: "
            f"{plan_path}"
        )
    stored = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(stored, dict):
        raise ValueError("Dry-run intake plan must be a JSON object")
    stored_fingerprint = str(stored.get("plan_fingerprint") or "")
    if stored_fingerprint != _plan_fingerprint(stored):
        raise ValueError("Stored dry-run intake plan fingerprint is invalid")
    if stored_fingerprint != current["plan_fingerprint"]:
        raise ValueError(
            "Archive or import plan changed after dry-run. "
            "Run a new dry-run before --apply"
        )
    return stored


def _matching_receipt(
    receipt_path: Path,
    *,
    plan_fingerprint: str,
) -> dict[str, Any] | None:
    if receipt_path.is_symlink():
        raise ValueError(f"Intake receipt must not be a symlink: {receipt_path}")
    if not receipt_path.is_file():
        return None
    try:
        stored = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(stored, dict):
        return None
    if stored.get("plan_fingerprint") != plan_fingerprint:
        return None
    snapshots = stored.get("snapshots")
    if not isinstance(snapshots, dict):
        return None
    after = snapshots.get("after")
    if not isinstance(after, str):
        return None
    if not (Path(after) / "manifest.json").is_file():
        return None
    return stored


def apply_intake(
    config_path: Path,
    archive_path: Path,
    *,
    report_directory: Path | None = None,
) -> dict[str, Any]:
    config_file = config_path.expanduser().resolve()
    project_root = config_file.parent
    archive = _validate_archive(archive_path, project_root=project_root)
    reports = _external_report_directory(
        archive,
        project_root=project_root,
        report_directory=report_directory,
    )
    plan_path = reports / PLAN_FILE_NAME
    if not plan_path.is_file():
        raise ValueError(
            "Dry-run intake plan is missing. Run without --apply first: "
            f"{plan_path}"
        )
    current_plan = _current_plan(
        config_file,
        archive,
    )
    stored_plan = _load_matching_plan(plan_path, current_plan)
    receipt_path = reports / RECEIPT_FILE_NAME
    prior_receipt = _matching_receipt(
        receipt_path,
        plan_fingerprint=stored_plan["plan_fingerprint"],
    )
    config = watcher.load_config(config_file)
    import_plan = x_archive_import.plan_archive_import(
        archive,
        expected_user_id=config.user_id,
    )
    snapshots: dict[str, str | None] = {
        "before": None,
        "after": None,
    }
    connection = watcher.connect_database(config.database)
    try:
        existing = connection.execute(
            """
            SELECT id
            FROM archive_imports
            WHERE archive_fingerprint = ?
            """,
            (import_plan.archive_fingerprint,),
        ).fetchone()
        if existing is None:
            before = memory_snapshot.create_memory_snapshot(
                config,
                connection,
                output_root=project_root / "var" / "snapshots",
            )
            snapshots["before"] = str(before["snapshot"])
        import_result = x_archive_import.apply_archive_import(
            connection,
            import_plan,
        )
        audit = watcher.memory_audit(config, connection)
        if not audit["final_complete"]:
            raise ValueError(
                "Post-import memory audit did not reach complete state: "
                + ", ".join([*audit["errors"], *audit["archive_errors"]])
            )
        if existing is not None and prior_receipt is not None:
            return {
                **prior_receipt,
                "status": "already_applied",
                "checked_at": watcher.isoformat(),
                "import_result": import_result,
                "memory_audit": audit,
            }
        if existing is None:
            after = memory_snapshot.create_memory_snapshot(
                config,
                connection,
                output_root=project_root / "var" / "snapshots",
            )
            snapshots["after"] = str(after["snapshot"])
        else:
            recovered = memory_snapshot.create_memory_snapshot(
                config,
                connection,
                output_root=project_root / "var" / "snapshots",
            )
            snapshots["after"] = str(recovered["snapshot"])
    finally:
        connection.close()
    receipt: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "status": (
            "recovered_existing_import"
            if import_result["status"] == "already_imported"
            else "applied"
        ),
        "completed_at": watcher.isoformat(),
        "project_root": str(project_root),
        "archive": stored_plan["archive"],
        "plan_fingerprint": stored_plan["plan_fingerprint"],
        "plan_report": str(plan_path),
        "import_result": import_result,
        "memory_audit": audit,
        "snapshots": snapshots,
    }
    receipt["receipt"] = str(receipt_path)
    _atomic_json(receipt_path, receipt)
    return receipt


def _current_plan(
    config_path: Path,
    archive: Path,
) -> dict[str, Any]:
    config = watcher.load_config(config_path)
    import_plan = x_archive_import.plan_archive_import(
        archive,
        expected_user_id=config.user_id,
    )
    payload: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "archive": {
            "path": str(archive),
            "bytes": archive.stat().st_size,
            "sha256": _sha256(archive),
        },
        "import_plan": import_plan.summary(),
    }
    payload["plan_fingerprint"] = _plan_fingerprint(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare or apply a two-phase official X archive intake. "
            "Default mode is a non-mutating dry-run."
        ),
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--report-directory", type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Import only after a matching dry-run plan exists, then audit "
            "memory and create consistent before/after snapshots."
        ),
    )
    args = parser.parse_args()
    if args.apply:
        result = apply_intake(
            args.config,
            args.archive,
            report_directory=args.report_directory,
        )
    else:
        result, plan_path = build_intake_plan(
            args.config,
            args.archive,
            report_directory=args.report_directory,
        )
        result = {
            **result,
            "plan_report": str(plan_path),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
