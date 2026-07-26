#!/usr/bin/env python3
"""Fail-closed restic backup orchestration for durable X memory."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import urlsplit

import evidence_import
import memory_snapshot
import xmention_watcher as watcher


PROJECT_ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = PROJECT_ROOT / "backup.json"
WATCHER_CONFIG_PATH = PROJECT_ROOT / "config.json"
SNAPSHOT_ROOT = PROJECT_ROOT / "var/snapshots"
EVIDENCE_ROOT = PROJECT_ROOT / "var/evidence/browser-owner"
STATE_ROOT = PROJECT_ROOT / "var/backup-state"
BACKUP_RECEIPT_NAME = "last-successful-backup.json"
KEYCHAIN_HELPER = PROJECT_ROOT / "var/keychain-helper"
KEYCHAIN_ACCOUNTS = {
    "repository": "repository",
    "password": "restic-password",
    "aws_access_key_id": "aws-access-key-id",
    "aws_secret_access_key": "aws-secret-access-key",
}
ALLOWED_SETTING_KEYS = {
    "archive_vault",
    "aws_region",
    "backend",
    "keychain_service",
    "restic_binary",
    "tag",
}
REQUIRED_SNAPSHOT_FILES = {
    "watcher.sqlite3",
    "conversation-history.jsonl",
    "initial-audit-resolutions.jsonl",
}
TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SECRET_ENV_NAMES = {
    "RESTIC_PASSWORD",
    "RESTIC_PASSWORD_COMMAND",
    "RESTIC_PASSWORD_FILE",
    "RESTIC_REPOSITORY",
    "RESTIC_REPOSITORY_FILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "B2_ACCOUNT_ID",
    "B2_ACCOUNT_KEY",
}


@dataclasses.dataclass(frozen=True)
class BackupSettings:
    project_root: Path
    watcher_config: Path
    snapshot_root: Path
    evidence_root: Path
    state_root: Path
    archive_vault: Path | None
    restic_binary: str
    keychain_helper: Path
    keychain_service: str
    tag: str
    backend: str
    aws_region: str


@dataclasses.dataclass(frozen=True)
class ResticCredentials:
    repository: str
    password: str
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None

    def secret_values(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                self.password,
                self.aws_access_key_id,
                self.aws_secret_access_key,
            )
            if value
        )


@dataclasses.dataclass(frozen=True)
class ResticResult:
    returncode: int
    stdout: str
    stderr: str


class ResticCommandError(RuntimeError):
    pass


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid JSON object at {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object at {path}")
    return payload


def _resolve_restic_binary(value: str) -> str:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ValueError(f"restic binary is unavailable: {candidate}")
        return str(candidate.resolve())
    resolved = shutil.which(value)
    if resolved is None:
        raise ValueError(
            "restic binary is unavailable; install restic before remote setup"
        )
    return str(Path(resolved).resolve())


def _validate_archive_vault(path: Path, project_root: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("archive_vault must be an absolute path")
    resolved = path.expanduser().resolve()
    if _is_within(resolved, project_root) or _is_within(project_root, resolved):
        raise ValueError("archive_vault must be outside the canonical project")
    broad_paths = {
        Path("/"),
        Path.home().resolve(),
        (Path.home() / "Documents").resolve(),
        (Path.home() / "Developer").resolve(),
        (Path.home() / "Archives").resolve(),
    }
    if resolved in broad_paths:
        raise ValueError("archive_vault is too broad")
    return resolved


def load_settings(
    path: Path = SETTINGS_PATH,
    *,
    project_root: Path = PROJECT_ROOT,
    require_restic: bool = True,
    require_remote: bool = True,
) -> BackupSettings:
    path = path.expanduser().resolve()
    root = project_root.expanduser().resolve()
    if path.parent != root:
        raise ValueError("backup settings must live in the canonical project root")
    payload = _json_object(path)
    unknown = set(payload) - ALLOWED_SETTING_KEYS
    missing = ALLOWED_SETTING_KEYS - set(payload)
    if unknown:
        raise ValueError(f"Unknown backup settings: {sorted(unknown)}")
    if missing:
        raise ValueError(f"Missing backup settings: {sorted(missing)}")
    backend = str(payload["backend"])
    if backend != "backblaze-b2-s3":
        raise ValueError("Only the backblaze-b2-s3 backend is supported")
    tag = str(payload["tag"])
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError("Invalid restic tag")
    keychain_service = str(payload["keychain_service"]).strip()
    if not keychain_service:
        raise ValueError("keychain_service must not be empty")
    aws_region = str(payload["aws_region"]).strip()
    if require_remote and not re.fullmatch(
        r"[a-z]{2}-[a-z]+-[0-9]{3}",
        aws_region,
    ):
        raise ValueError("aws_region must match the B2 bucket region")
    archive_value = payload["archive_vault"]
    archive_vault = (
        _validate_archive_vault(Path(str(archive_value)), root)
        if archive_value
        else None
    )
    restic_value = str(payload["restic_binary"])
    restic_binary = (
        _resolve_restic_binary(restic_value)
        if require_restic
        else restic_value
    )
    return BackupSettings(
        project_root=root,
        watcher_config=root / "config.json",
        snapshot_root=root / "var/snapshots",
        evidence_root=root / "var/evidence/browser-owner",
        state_root=root / "var/backup-state",
        archive_vault=archive_vault,
        restic_binary=restic_binary,
        keychain_helper=root / "var/keychain-helper",
        keychain_service=keychain_service,
        tag=tag,
        backend=backend,
        aws_region=aws_region,
    )


def audit_memory_snapshot(
    snapshot: Path,
    watcher_config: watcher.Config,
) -> dict[str, Any]:
    snapshot = snapshot.expanduser().resolve()
    errors: list[str] = []
    manifest_path = snapshot / "manifest.json"
    try:
        manifest = _json_object(manifest_path)
    except ValueError as error:
        return {
            "complete": False,
            "snapshot": str(snapshot),
            "errors": [str(error)],
        }
    files = manifest.get("files")
    if not isinstance(files, dict):
        return {
            "complete": False,
            "snapshot": str(snapshot),
            "errors": ["manifest_files_invalid"],
        }
    if not REQUIRED_SNAPSHOT_FILES.issubset(files):
        errors.append("required_snapshot_files_missing")
    expected_names = {"manifest.json"}
    for name, metadata in files.items():
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or name in {"", ".", ".."}
            or not isinstance(metadata, dict)
        ):
            errors.append("manifest_file_contract_invalid")
            continue
        expected_names.add(name)
        path = snapshot / name
        if path.is_symlink() or not path.is_file():
            errors.append(f"snapshot_file_missing:{name}")
            continue
        try:
            expected_bytes = int(metadata["bytes"])
            expected_hash = str(metadata["sha256"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"snapshot_metadata_invalid:{name}")
            continue
        if path.stat().st_size != expected_bytes:
            errors.append(f"snapshot_size_mismatch:{name}")
        if _sha256(path) != expected_hash:
            errors.append(f"snapshot_hash_mismatch:{name}")
    actual_names: set[str] = set()
    if snapshot.is_dir() and not snapshot.is_symlink():
        for path in snapshot.iterdir():
            if path.is_symlink() or not path.is_file():
                errors.append(f"unexpected_snapshot_entry:{path.name}")
            else:
                actual_names.add(path.name)
    else:
        errors.append("snapshot_directory_invalid")
    if actual_names != expected_names:
        errors.append("snapshot_file_set_mismatch")

    database = snapshot / "watcher.sqlite3"
    if database.is_file() and not errors:
        uri = f"{database.as_uri()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            integrity = [
                str(row[0])
                for row in connection.execute("PRAGMA integrity_check").fetchall()
            ]
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if integrity != ["ok"]:
                errors.append("snapshot_sqlite_integrity_failed")
            if foreign_keys:
                errors.append("snapshot_foreign_key_check_failed")
            if not errors:
                live_audit = watcher.memory_audit(watcher_config, connection)
                if live_audit != manifest.get("memory_audit"):
                    errors.append("snapshot_memory_audit_mismatch")
        finally:
            connection.close()
    return {
        "complete": not errors,
        "snapshot": str(snapshot),
        "created_at": manifest.get("created_at"),
        "configured_user_id": manifest.get("configured_user_id"),
        "files": len(files),
        "errors": errors,
    }


def _audit_archive_vault(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"Configured archive_vault is missing: {path}")
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"archive_vault is not a normal directory: {path}")
    file_count = 0
    total_bytes = 0
    records: list[dict[str, Any]] = []
    for entry in sorted(path.rglob("*")):
        if entry.is_symlink():
            raise ValueError(f"Symlink is not allowed in archive_vault: {entry}")
        if entry.is_dir():
            continue
        if not entry.is_file():
            raise ValueError(
                f"Unsupported entry in archive_vault: {entry}"
            )
        file_count += 1
        size = entry.stat().st_size
        total_bytes += size
        records.append(
            {
                "path": entry.relative_to(path).as_posix(),
                "bytes": size,
                "sha256": _sha256(entry),
            }
        )
    if not records:
        raise ValueError(f"Configured archive_vault is empty: {path}")
    fingerprint = hashlib.sha256(
        json.dumps(
            records,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "present": True,
        "path": str(path),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "fingerprint": fingerprint,
    }


def _plan_fingerprint(source_records: Sequence[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(
            list(source_records),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def build_backup_plan(settings: BackupSettings) -> dict[str, Any]:
    root = settings.project_root.resolve()
    for required in (
        settings.watcher_config,
        settings.snapshot_root,
        settings.evidence_root,
        settings.state_root,
        settings.keychain_helper,
    ):
        resolved = required.resolve()
        if not _is_within(resolved, root):
            raise ValueError(f"Canonical backup path escaped project root: {required}")
    watcher_config = watcher.load_config(settings.watcher_config)
    snapshot_records: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []
    if not settings.snapshot_root.is_dir():
        raise ValueError("No completed memory snapshot directory exists")
    for entry in sorted(settings.snapshot_root.iterdir()):
        if entry.name.startswith("."):
            raise ValueError(f"Temporary snapshot is present: {entry.name}")
        if entry.is_symlink() or not entry.is_dir():
            raise ValueError(f"Unexpected snapshot entry: {entry}")
        audit = audit_memory_snapshot(entry, watcher_config)
        if not audit["complete"]:
            raise ValueError(
                f"Invalid memory snapshot {entry.name}: {audit['errors']}"
            )
        snapshot_records.append(audit)
        manifest_path = entry / "manifest.json"
        manifest = _json_object(manifest_path)
        source_records.append(
            {
                "kind": "memory_snapshot",
                "path": str(entry.resolve()),
                "fingerprint": _sha256(manifest_path),
                "file_count": len(manifest["files"]) + 1,
                "total_bytes": manifest_path.stat().st_size
                + sum(
                    int(metadata["bytes"])
                    for metadata in manifest["files"].values()
                ),
            }
        )
    if not snapshot_records:
        raise ValueError("At least one completed memory snapshot is required")

    evidence_records: list[dict[str, Any]] = []
    if settings.evidence_root.exists():
        if settings.evidence_root.is_symlink() or not settings.evidence_root.is_dir():
            raise ValueError("Canonical evidence root is invalid")
        for entry in sorted(settings.evidence_root.iterdir()):
            if entry.name.startswith("."):
                raise ValueError(f"Temporary evidence tree is present: {entry.name}")
            if entry.is_symlink() or not entry.is_dir():
                raise ValueError(f"Unexpected evidence entry: {entry}")
            audit = evidence_import.audit_evidence(entry)
            if not audit["complete"]:
                raise ValueError(
                    f"Invalid evidence tree {entry.name}: {audit['errors']}"
                )
            evidence_records.append(audit)
            source_records.append(
                {
                    "kind": "browser_evidence",
                    "path": str(entry.resolve()),
                    "fingerprint": audit["source_fingerprint"],
                    "file_count": int(audit["file_count"]) + 1,
                    "total_bytes": int(audit["total_bytes"])
                    + (entry / "manifest.json").stat().st_size,
                }
            )

    archive_record = None
    if settings.archive_vault is not None:
        archive_record = _audit_archive_vault(settings.archive_vault)
        source_records.append(
            {
                "kind": "archive_vault",
                "path": str(settings.archive_vault.resolve()),
                "fingerprint": archive_record["fingerprint"],
                "file_count": int(archive_record["file_count"]),
                "total_bytes": int(archive_record["total_bytes"]),
            }
        )

    forbidden = {
        (settings.project_root / "var/watcher.sqlite3").resolve(),
        (settings.project_root / "var/watcher.sqlite3-wal").resolve(),
        (settings.project_root / "var/watcher.sqlite3-shm").resolve(),
    }
    sources = [Path(record["path"]) for record in source_records]
    if forbidden.intersection(sources):
        raise ValueError("Live SQLite files must never enter a backup plan")
    if len(set(sources)) != len(sources):
        raise ValueError("Duplicate source path in backup plan")
    fingerprint = _plan_fingerprint(source_records)
    return {
        "version": 1,
        "complete": True,
        "project_root": str(settings.project_root),
        "sources": [str(path) for path in sources],
        "source_records": source_records,
        "source_count": len(sources),
        "total_source_bytes": sum(
            int(record["total_bytes"]) for record in source_records
        ),
        "plan_fingerprint": fingerprint,
        "snapshots": snapshot_records,
        "evidence": evidence_records,
        "archive_vault": archive_record,
        "excluded_live_database": True,
    }


def _keychain(
    helper: Path,
    command: str,
    service: str,
    account: str,
) -> str:
    result = subprocess.run(
        [str(helper), command, service, account],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError(
            f"Keychain item unavailable: service={service}, account={account}"
        )
    if command == "exists":
        return ""
    value = result.stdout.decode("utf-8").strip()
    if not value:
        raise ValueError(
            f"Keychain item is empty: service={service}, account={account}"
        )
    return value


def check_keychain_contract(settings: BackupSettings) -> dict[str, Any]:
    if not settings.keychain_helper.is_file() or not os.access(
        settings.keychain_helper, os.X_OK
    ):
        raise ValueError("Bundled Keychain helper is unavailable")
    checked: list[str] = []
    for account in KEYCHAIN_ACCOUNTS.values():
        _keychain(
            settings.keychain_helper,
            "exists",
            settings.keychain_service,
            account,
        )
        checked.append(account)
    return {
        "complete": True,
        "service": settings.keychain_service,
        "accounts_present": checked,
    }


def load_credentials(settings: BackupSettings) -> ResticCredentials:
    values = {
        name: _keychain(
            settings.keychain_helper,
            "get",
            settings.keychain_service,
            account,
        )
        for name, account in KEYCHAIN_ACCOUNTS.items()
    }
    credentials = ResticCredentials(
        repository=values["repository"],
        password=values["password"],
        aws_access_key_id=values["aws_access_key_id"],
        aws_secret_access_key=values["aws_secret_access_key"],
    )
    validate_repository(settings, credentials.repository)
    return credentials


def validate_repository(settings: BackupSettings, repository: str) -> None:
    prefix = "s3:"
    if not repository.startswith(prefix):
        raise ValueError("Backblaze repository must use restic s3:https URL")
    parsed = urlsplit(repository[len(prefix) :])
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
        or not parsed.hostname.startswith("s3.")
        or not parsed.hostname.endswith(".backblazeb2.com")
    ):
        raise ValueError("Invalid Backblaze S3 repository URL")
    region = parsed.hostname[len("s3.") : -len(".backblazeb2.com")]
    if region != settings.aws_region:
        raise ValueError("Backblaze repository region does not match aws_region")
    if len([part for part in parsed.path.split("/") if part]) < 2:
        raise ValueError(
            "Backblaze repository URL must include a bucket and dedicated prefix"
        )


def _redact(text: str, secrets: Sequence[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _restic_environment(
    settings: BackupSettings,
    credentials: ResticCredentials,
) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in SECRET_ENV_NAMES
    }
    environment.update(
        {
            "RESTIC_REPOSITORY": credentials.repository,
            "RESTIC_PASSWORD": credentials.password,
            "RESTIC_CACHE_DIR": str(settings.state_root / "cache"),
            "GOMAXPROCS": "2",
        }
    )
    if settings.backend == "backblaze-b2-s3":
        if not credentials.aws_access_key_id or not credentials.aws_secret_access_key:
            raise ValueError("Backblaze S3 credentials are incomplete")
        environment.update(
            {
                "AWS_ACCESS_KEY_ID": credentials.aws_access_key_id,
                "AWS_SECRET_ACCESS_KEY": credentials.aws_secret_access_key,
                "AWS_DEFAULT_REGION": settings.aws_region,
            }
        )
    return environment


def run_restic(
    settings: BackupSettings,
    credentials: ResticCredentials,
    arguments: Sequence[str],
) -> ResticResult:
    settings.state_root.mkdir(parents=True, exist_ok=True)
    (settings.state_root / "cache").mkdir(parents=True, exist_ok=True)
    backend_options: list[str] = []
    if settings.backend == "backblaze-b2-s3":
        backend_options = ["-o", "s3.connections=2"]
    command = [
        settings.restic_binary,
        *backend_options,
        *arguments,
    ]
    result = subprocess.run(
        command,
        cwd=settings.project_root,
        env=_restic_environment(settings, credentials),
        check=False,
        capture_output=True,
        text=True,
    )
    secrets = credentials.secret_values()
    stdout = _redact(result.stdout[-32768:], secrets)
    stderr = _redact(result.stderr[-32768:], secrets)
    response = ResticResult(
        returncode=result.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    if result.returncode != 0:
        raise ResticCommandError(
            f"restic failed with code {result.returncode}: {stderr or stdout}"
        )
    return response


@contextlib.contextmanager
def backup_lock(settings: BackupSettings) -> Iterator[None]:
    settings.state_root.mkdir(parents=True, exist_ok=True)
    lock_path = settings.state_root / "backup.lock"
    with lock_path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another backup operation is already running") from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def record_failure(
    settings: BackupSettings,
    *,
    action: str,
    error: Exception,
) -> None:
    _atomic_json(
        settings.state_root / "latest.json",
        {
            "version": 1,
            "status": "error",
            "action": action,
            "failed_at": watcher.isoformat(),
            "error": str(error),
        },
    )


def _write_source_list(settings: BackupSettings, plan: dict[str, Any]) -> Path:
    settings.state_root.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=".restic-files.",
        dir=settings.state_root,
    )
    path = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            for source in plan["sources"]:
                stream.write(os.fsencode(source))
                stream.write(b"\0")
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _extract_backup_snapshot_id(stdout: str) -> str:
    for line in reversed(stdout.splitlines()):
        try:
            payload = json.loads(line)
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        snapshot_id = payload.get("snapshot_id")
        if isinstance(snapshot_id, str) and re.fullmatch(
            r"[0-9a-f]{64}",
            snapshot_id,
        ):
            return snapshot_id
    raise ValueError("restic backup output did not contain an exact snapshot ID")


def _backup_receipt_path(settings: BackupSettings) -> Path:
    return settings.state_root / BACKUP_RECEIPT_NAME


def _load_backup_receipt(settings: BackupSettings) -> dict[str, Any]:
    receipt = _json_object(_backup_receipt_path(settings))
    try:
        snapshot_id = str(receipt["snapshot_id"])
        plan = receipt["plan"]
        source_records = plan["source_records"]
        fingerprint = str(plan["plan_fingerprint"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Backup receipt contract is invalid") from error
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
        raise ValueError("Backup receipt snapshot ID is invalid")
    if not isinstance(source_records, list):
        raise ValueError("Backup receipt source records are invalid")
    if _plan_fingerprint(source_records) != fingerprint:
        raise ValueError("Backup receipt plan fingerprint is invalid")
    return receipt


def _create_fresh_memory_snapshot(settings: BackupSettings) -> dict[str, Any]:
    config = watcher.load_config(settings.watcher_config)
    connection = watcher.connect_database(config.database)
    try:
        return memory_snapshot.create_memory_snapshot(
            config,
            connection,
            output_root=settings.snapshot_root,
        )
    finally:
        connection.close()


def backup_once(
    settings: BackupSettings,
    credentials: ResticCredentials,
    *,
    create_snapshot: bool = True,
) -> dict[str, Any]:
    with backup_lock(settings):
        created_snapshot = (
            _create_fresh_memory_snapshot(settings)
            if create_snapshot
            else None
        )
        plan = build_backup_plan(settings)
        source_list = _write_source_list(settings, plan)
        try:
            result = run_restic(
                settings,
                credentials,
                [
                    "backup",
                    "--files-from-raw",
                    str(source_list),
                    "--tag",
                    settings.tag,
                    "--json",
                ],
            )
        finally:
            source_list.unlink(missing_ok=True)
        snapshot_id = _extract_backup_snapshot_id(result.stdout)
        receipt = {
            "version": 1,
            "status": "complete",
            "action": "backup",
            "completed_at": watcher.isoformat(),
            "snapshot_id": snapshot_id,
            "plan": plan,
        }
        _atomic_json(_backup_receipt_path(settings), receipt)
        state = {
            "version": 1,
            "status": "complete",
            "action": "backup",
            "completed_at": watcher.isoformat(),
            "created_snapshot": created_snapshot,
            "snapshot_id": snapshot_id,
            "plan_fingerprint": plan["plan_fingerprint"],
            "source_count": plan["source_count"],
            "snapshot_count": len(plan["snapshots"]),
            "evidence_count": len(plan["evidence"]),
            "archive_vault_present": bool(
                plan["archive_vault"] and plan["archive_vault"]["present"]
            ),
            "restic_returncode": result.returncode,
            "restic_stdout_tail": result.stdout,
            "restic_stderr_tail": result.stderr,
        }
        _atomic_json(settings.state_root / "latest.json", state)
        return state


def check_repository(
    settings: BackupSettings,
    credentials: ResticCredentials,
    *,
    read_data: bool,
) -> dict[str, Any]:
    with backup_lock(settings):
        arguments = ["check"]
        if read_data:
            arguments.append("--read-data")
        result = run_restic(settings, credentials, arguments)
        state = {
            "version": 1,
            "status": "complete",
            "action": "check-read-data" if read_data else "check",
            "completed_at": watcher.isoformat(),
            "restic_returncode": result.returncode,
            "restic_stdout_tail": result.stdout,
            "restic_stderr_tail": result.stderr,
        }
        _atomic_json(settings.state_root / "latest.json", state)
        return state


def apply_retention(
    settings: BackupSettings,
    credentials: ResticCredentials,
    *,
    apply: bool,
) -> dict[str, Any]:
    with backup_lock(settings):
        arguments = [
            "forget",
            "--tag",
            settings.tag,
            "--keep-hourly",
            "48",
            "--keep-daily",
            "30",
            "--keep-weekly",
            "12",
            "--keep-monthly",
            "24",
        ]
        if apply:
            arguments.append("--prune")
        else:
            arguments.append("--dry-run")
        result = run_restic(settings, credentials, arguments)
        state = {
            "version": 1,
            "status": "complete",
            "action": "retention-apply" if apply else "retention-dry-run",
            "completed_at": watcher.isoformat(),
            "restic_returncode": result.returncode,
            "restic_stdout_tail": result.stdout,
            "restic_stderr_tail": result.stderr,
        }
        _atomic_json(settings.state_root / "latest.json", state)
        return state


def _restored_absolute_path(target: Path, source: Path) -> Path:
    if not source.is_absolute():
        raise ValueError("Restore source path must be absolute")
    resolved = source.resolve()
    return target.joinpath(*resolved.parts[1:])


def _required_restore_free_bytes(total_source_bytes: int) -> int:
    if total_source_bytes <= 0:
        raise ValueError("Backup receipt total_source_bytes must be positive")
    return max(
        total_source_bytes + 512 * 1024 * 1024,
        (total_source_bytes * 6 + 4) // 5,
    )


def _validate_restored_source(
    target: Path,
    record: dict[str, Any],
    watcher_config: watcher.Config,
) -> dict[str, Any]:
    try:
        kind = str(record["kind"])
        source = Path(str(record["path"]))
        expected_fingerprint = str(record["fingerprint"])
        expected_file_count = int(record["file_count"])
        expected_total_bytes = int(record["total_bytes"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Backup source record is invalid") from error
    restored = _restored_absolute_path(target, source)
    if kind == "memory_snapshot":
        audit = audit_memory_snapshot(restored, watcher_config)
        if not audit["complete"]:
            raise ValueError(f"Restored memory snapshot is invalid: {audit}")
        actual_fingerprint = _sha256(restored / "manifest.json")
        manifest = _json_object(restored / "manifest.json")
        actual_file_count = len(manifest["files"]) + 1
        actual_total_bytes = (restored / "manifest.json").stat().st_size + sum(
            int(metadata["bytes"]) for metadata in manifest["files"].values()
        )
    elif kind == "browser_evidence":
        audit = evidence_import.audit_evidence(restored)
        if not audit["complete"]:
            raise ValueError(f"Restored Browser evidence is invalid: {audit}")
        actual_fingerprint = str(audit["source_fingerprint"])
        actual_file_count = int(audit["file_count"]) + 1
        actual_total_bytes = int(audit["total_bytes"]) + (
            restored / "manifest.json"
        ).stat().st_size
    elif kind == "archive_vault":
        audit = _audit_archive_vault(restored)
        actual_fingerprint = str(audit["fingerprint"])
        actual_file_count = int(audit["file_count"])
        actual_total_bytes = int(audit["total_bytes"])
    else:
        raise ValueError(f"Unsupported backup source kind: {kind}")
    if (
        actual_fingerprint != expected_fingerprint
        or actual_file_count != expected_file_count
        or actual_total_bytes != expected_total_bytes
    ):
        raise ValueError(f"Restored source differs from receipt: {source}")
    return {
        "kind": kind,
        "path": str(source),
        "fingerprint": actual_fingerprint,
        "file_count": actual_file_count,
        "total_bytes": actual_total_bytes,
    }


def restore_smoke(
    settings: BackupSettings,
    credentials: ResticCredentials,
    *,
    keep_restore: bool = False,
) -> dict[str, Any]:
    with backup_lock(settings):
        receipt = _load_backup_receipt(settings)
        plan = receipt["plan"]
        total_source_bytes = int(plan["total_source_bytes"])
        restore_root = settings.state_root / "restore-drills"
        restore_root.mkdir(parents=True, exist_ok=True)
        required_free_bytes = _required_restore_free_bytes(total_source_bytes)
        available_free_bytes = shutil.disk_usage(restore_root).free
        if available_free_bytes < required_free_bytes:
            raise ValueError(
                "Insufficient free disk space for restore drill: "
                f"required={required_free_bytes}, available={available_free_bytes}"
            )
        target = Path(tempfile.mkdtemp(prefix="restic-restore-", dir=restore_root))
        try:
            result = run_restic(
                settings,
                credentials,
                [
                    "restore",
                    receipt["snapshot_id"],
                    "--target",
                    str(target),
                ],
            )
            watcher_config = watcher.load_config(settings.watcher_config)
            restored_sources = [
                _validate_restored_source(target, record, watcher_config)
                for record in plan["source_records"]
            ]
            restored_plan_fingerprint = _plan_fingerprint(restored_sources)
            if restored_plan_fingerprint != plan["plan_fingerprint"]:
                raise ValueError("Restored source plan fingerprint mismatch")
            state = {
                "version": 1,
                "status": "complete",
                "action": "restore-smoke",
                "completed_at": watcher.isoformat(),
                "snapshot_id": receipt["snapshot_id"],
                "plan_fingerprint": plan["plan_fingerprint"],
                "restored_source_count": len(restored_sources),
                "restored_snapshot_count": len(
                    [
                        record
                        for record in restored_sources
                        if record["kind"] == "memory_snapshot"
                    ]
                ),
                "restored_evidence_count": len(
                    [
                        record
                        for record in restored_sources
                        if record["kind"] == "browser_evidence"
                    ]
                ),
                "archive_vault_restored": any(
                    record["kind"] == "archive_vault"
                    for record in restored_sources
                ),
                "required_free_bytes": required_free_bytes,
                "available_free_bytes": available_free_bytes,
                "restic_returncode": result.returncode,
                "restic_stdout_tail": result.stdout,
                "restic_stderr_tail": result.stderr,
                "restore_kept": keep_restore,
                "restore_path": str(target) if keep_restore else None,
            }
            _atomic_json(settings.state_root / "latest.json", state)
            return state
        finally:
            if not keep_restore:
                shutil.rmtree(target, ignore_errors=True)


def initialize_repository(
    settings: BackupSettings,
    credentials: ResticCredentials,
) -> dict[str, Any]:
    with backup_lock(settings):
        result = run_restic(settings, credentials, ["init"])
        state = {
            "version": 1,
            "status": "complete",
            "action": "init",
            "completed_at": watcher.isoformat(),
            "restic_returncode": result.returncode,
            "restic_stdout_tail": result.stdout,
            "restic_stderr_tail": result.stderr,
        }
        _atomic_json(settings.state_root / "latest.json", state)
        return state


def preflight(settings: BackupSettings) -> dict[str, Any]:
    plan = build_backup_plan(settings)
    keychain = check_keychain_contract(settings)
    credentials = load_credentials(settings)
    version = subprocess.run(
        [settings.restic_binary, "version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if version.returncode != 0:
        raise ValueError("restic version check failed")
    return {
        "version": 1,
        "complete": True,
        "restic": version.stdout.strip(),
        "plan": plan,
        "keychain": keychain,
        "repository_contract_valid": bool(credentials.repository),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Encrypted off-site backup for durable X memory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan")
    subparsers.add_parser("preflight")
    initialize = subparsers.add_parser("init")
    initialize.add_argument(
        "--confirm-existing-private-bucket",
        action="store_true",
    )
    subparsers.add_parser("backup")
    check = subparsers.add_parser("check")
    check.add_argument("--read-data", action="store_true")
    retention = subparsers.add_parser("retention")
    retention.add_argument("--apply", action="store_true")
    restore = subparsers.add_parser("restore-smoke")
    restore.add_argument("--keep-restore", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings: BackupSettings | None = None
    try:
        settings = load_settings(
            require_restic=args.command != "plan",
            require_remote=args.command != "plan",
        )
        if args.command == "plan":
            result = build_backup_plan(settings)
        elif args.command == "preflight":
            result = preflight(settings)
        else:
            credentials = load_credentials(settings)
            if args.command == "init":
                if not args.confirm_existing_private_bucket:
                    raise ValueError(
                        "init requires --confirm-existing-private-bucket"
                    )
                result = initialize_repository(settings, credentials)
            elif args.command == "backup":
                result = backup_once(settings, credentials)
            elif args.command == "check":
                result = check_repository(
                    settings,
                    credentials,
                    read_data=args.read_data,
                )
            elif args.command == "retention":
                result = apply_retention(
                    settings,
                    credentials,
                    apply=args.apply,
                )
            elif args.command == "restore-smoke":
                result = restore_smoke(
                    settings,
                    credentials,
                    keep_restore=args.keep_restore,
                )
            else:
                raise AssertionError(f"Unhandled command: {args.command}")
    except (OSError, ValueError, ResticCommandError) as error:
        if settings is not None:
            try:
                record_failure(
                    settings,
                    action=args.command,
                    error=error,
                )
            except OSError:
                pass
        print(
            json.dumps(
                {
                    "version": 1,
                    "status": "error",
                    "error": str(error),
                },
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
