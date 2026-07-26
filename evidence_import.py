#!/usr/bin/env python3
"""Import legacy Browser-owner evidence into one manifested canonical tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
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
CANONICAL_OUTPUT_ROOT = (
    Path(__file__).resolve().parent / "var/evidence/browser-owner"
)


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
    parser.add_argument("--label")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.audit is not None:
        result = audit_evidence(args.audit)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["complete"] else 2
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
