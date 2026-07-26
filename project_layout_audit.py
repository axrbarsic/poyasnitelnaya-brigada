#!/usr/bin/env python3
"""Verify that the X autopilot implementation uses one canonical project root."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


REQUIRED_PATHS = (
    ".codex/config.toml",
    "AGENTS.md",
    "README.md",
    "README.ru.md",
    "backup.example.json",
    "candidate_corpus.py",
    "config.example.json",
    "docs/project-layout.md",
    "docs/project-layout.ru.md",
    "evidence_import.py",
    "macos/com.axrbarsic.xmention.poll.plist.example",
    "macos/com.axrbarsic.xmention.watchdog.plist.example",
    "memory_snapshot.py",
    "scripts/autopilot_bridge.py",
    "scripts/autopilot_contract.py",
    "scripts/autopilot_dispatch.py",
    "skill-backup/x-twitter-operator/SKILL.md",
    "restic_backup.py",
    "tests/test_restic_backup.py",
    "tests/test_xmention_watcher.py",
    "x_archive_import.py",
    "xmention_watcher.py",
)

IGNORED_DIRECTORY_NAMES = {"__pycache__"}
IGNORED_SUFFIXES = {".pyc"}


def resolve_path(config_path: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (config_path.resolve().parent / candidate).resolve()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def directory_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    if not root.is_dir():
        return manifest
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in IGNORED_DIRECTORY_NAMES for part in relative.parts):
            continue
        if path.suffix in IGNORED_SUFFIXES:
            continue
        manifest[relative.as_posix()] = file_sha256(path)
    return manifest


def audit_layout(
    *,
    root: Path,
    config_path: Path,
    installed_skill: Path | None,
    require_installed_skill: bool,
) -> dict[str, Any]:
    canonical_root = root.resolve()
    resolved_config = config_path.resolve()
    missing_paths = [
        relative
        for relative in REQUIRED_PATHS
        if not (canonical_root / relative).exists()
    ]

    errors: list[str] = []
    browser_owner_cwd: str | None = None
    browser_owner_cwd_ok = False
    try:
        config = json.loads(resolved_config.read_text(encoding="utf-8"))
        browser_owner_cwd = str(
            resolve_path(
                resolved_config,
                str(config.get("browser_owner_cwd", ".")),
            )
        )
        browser_owner_cwd_ok = Path(browser_owner_cwd) == canonical_root
        if not browser_owner_cwd_ok:
            errors.append("browser_owner_cwd_outside_canonical_root")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        errors.append(f"config_error:{error}")

    skill_backup = canonical_root / "skill-backup/x-twitter-operator"
    backup_manifest = directory_manifest(skill_backup)
    installed_skill_exists = installed_skill is not None and installed_skill.is_dir()
    installed_skill_matches: bool | None = None
    if installed_skill_exists and installed_skill is not None:
        installed_skill_matches = (
            directory_manifest(installed_skill.resolve()) == backup_manifest
        )
        if not installed_skill_matches:
            errors.append("installed_skill_differs_from_repository_backup")
    elif require_installed_skill:
        errors.append("installed_skill_missing")

    if missing_paths:
        errors.append("required_project_paths_missing")
    if not backup_manifest:
        errors.append("skill_backup_empty")

    complete = not errors
    return {
        "version": 1,
        "complete": complete,
        "status": "complete" if complete else "invalid",
        "canonical_root": str(canonical_root),
        "config_path": str(resolved_config),
        "browser_owner_cwd": browser_owner_cwd,
        "browser_owner_cwd_ok": browser_owner_cwd_ok,
        "missing_paths": missing_paths,
        "skill_backup_files": len(backup_manifest),
        "installed_skill": (
            str(installed_skill.resolve())
            if installed_skill_exists and installed_skill is not None
            else None
        ),
        "installed_skill_matches": installed_skill_matches,
        "errors": errors,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--installed-skill",
        type=Path,
        default=Path.home() / ".codex/skills/x-twitter-operator",
    )
    parser.add_argument("--require-installed-skill", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    config_path = (
        args.config.resolve()
        if args.config is not None
        else root / "config.example.json"
    )
    result = audit_layout(
        root=root,
        config_path=config_path,
        installed_skill=args.installed_skill,
        require_installed_skill=args.require_installed_skill,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
