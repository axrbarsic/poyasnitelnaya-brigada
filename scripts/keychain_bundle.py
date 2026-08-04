#!/usr/bin/env python3
"""Verify the signed macOS wrapper used by the Keychain helper."""

from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


EXPECTED_BUNDLE_ID = "com.axrbarsic.xmention.keychain-helper"
EXPECTED_TEAM_ID = "J6MW4855LU"
EXPECTED_APPLICATION_ID = f"{EXPECTED_TEAM_ID}.{EXPECTED_BUNDLE_ID}"


@dataclass(frozen=True)
class BundleVerification:
    ok: bool
    app_path: str | None
    executable_path: str
    bundle_id: str | None
    application_id: str | None
    profile_name: str | None
    profile_expires_at: str | None
    errors: tuple[str, ...]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess(
            list(command),
            125,
            b"",
            str(error).encode("utf-8", errors="replace"),
        )


def _plist_from_command(
    command: Sequence[str],
) -> tuple[dict[str, Any] | None, str | None]:
    completed = _run(command)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        return None, detail or f"exit={completed.returncode}"
    payload = completed.stdout
    if not payload:
        payload = completed.stderr
    xml_offset = payload.find(b"<?xml")
    binary_offset = payload.find(b"bplist")
    offsets = [offset for offset in (xml_offset, binary_offset) if offset >= 0]
    if offsets:
        payload = payload[min(offsets) :]
    try:
        decoded = plistlib.loads(payload)
    except (plistlib.InvalidFileException, ValueError) as error:
        return None, f"invalid plist: {error}"
    if not isinstance(decoded, dict):
        return None, "plist root is not a dictionary"
    return decoded, None


def _find_app(executable: Path) -> Path | None:
    resolved = executable.expanduser().resolve(strict=False)
    if (
        resolved.parent.name == "MacOS"
        and resolved.parent.parent.name == "Contents"
        and resolved.parent.parent.parent.suffix == ".app"
    ):
        return resolved.parent.parent.parent
    return None


def _profile_allows(
    claimed: str,
    allowed_values: object,
) -> bool:
    if not isinstance(allowed_values, list):
        return False
    for value in allowed_values:
        if not isinstance(value, str):
            continue
        if value == claimed:
            return True
        if (
            value == f"{EXPECTED_TEAM_ID}.*"
            and claimed.startswith(f"{EXPECTED_TEAM_ID}.")
        ):
            return True
    return False


def _codesign_value(payload: bytes, key: str) -> str | None:
    prefix = f"{key}=".encode()
    for line in payload.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].decode(
                "utf-8",
                errors="replace",
            ).strip() or None
    return None


def _verify_signature(app: Path, errors: list[str]) -> None:
    signature = _run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)]
    )
    if signature.returncode != 0:
        errors.append("bundle signature verification failed")
    details = _run(["/usr/bin/codesign", "-d", "--verbose=4", str(app)])
    team_id = _codesign_value(
        details.stdout + b"\n" + details.stderr,
        "TeamIdentifier",
    )
    if details.returncode != 0 or team_id != EXPECTED_TEAM_ID:
        errors.append("unexpected code-signing team")


def _bundle_identifier(app: Path, errors: list[str]) -> str | None:
    info_path = app / "Contents" / "Info.plist"
    try:
        info = plistlib.loads(info_path.read_bytes())
        if not isinstance(info, dict):
            raise ValueError("Info.plist root is not a dictionary")
        bundle_id = str(info.get("CFBundleIdentifier", "")).strip()
    except (OSError, plistlib.InvalidFileException, ValueError):
        errors.append("bundle Info.plist is unreadable")
        bundle_id = None
    if bundle_id != EXPECTED_BUNDLE_ID:
        errors.append("unexpected bundle identifier")
    return bundle_id


def _verify_signed_entitlements(
    app: Path,
    errors: list[str],
) -> str | None:
    entitlements, entitlement_error = _plist_from_command(
        [
            "/usr/bin/codesign",
            "-d",
            "--entitlements",
            ":-",
            str(app),
        ]
    )
    if entitlements is None:
        errors.append(
            "code-signing entitlements are unreadable"
            + (f": {entitlement_error}" if entitlement_error else "")
        )
        return None
    application_id = str(
        entitlements.get("com.apple.application-identifier", "")
    ).strip()
    if application_id != EXPECTED_APPLICATION_ID:
        errors.append("unexpected application identifier entitlement")
    groups = entitlements.get("keychain-access-groups")
    if not isinstance(groups, list) or EXPECTED_APPLICATION_ID not in groups:
        errors.append("expected keychain access group is missing")
    return application_id


def _profile_expiration(
    profile: dict[str, Any],
    *,
    minimum_valid_days: int,
    errors: list[str],
) -> str | None:
    expiration = profile.get("ExpirationDate")
    if not isinstance(expiration, datetime):
        errors.append("provisioning profile expiration is missing")
        return None
    if expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=timezone.utc)
    expiration_utc = expiration.astimezone(timezone.utc)
    if (
        expiration_utc - datetime.now(timezone.utc)
    ).total_seconds() < minimum_valid_days * 86400:
        errors.append("provisioning profile expires too soon")
    return expiration_utc.isoformat()


def _verify_profile_identity(
    profile: dict[str, Any],
    errors: list[str],
) -> None:
    platforms = profile.get("Platform")
    if not isinstance(platforms, list) or not {"OSX", "macOS"}.intersection(
        str(value) for value in platforms
    ):
        errors.append("provisioning profile is not for macOS")
    team_identifiers = profile.get("TeamIdentifier")
    if (
        not isinstance(team_identifiers, list)
        or EXPECTED_TEAM_ID not in team_identifiers
    ):
        errors.append("provisioning profile has an unexpected team")
    application_prefixes = profile.get("ApplicationIdentifierPrefix")
    if (
        not isinstance(application_prefixes, list)
        or EXPECTED_TEAM_ID not in application_prefixes
    ):
        errors.append("provisioning profile has an unexpected app prefix")


def _verify_profile_entitlements(
    profile: dict[str, Any],
    errors: list[str],
) -> None:
    entitlements = profile.get("Entitlements", {})
    if not isinstance(entitlements, dict):
        errors.append("profile entitlements are missing")
        return
    profile_app_id = entitlements.get(
        "com.apple.application-identifier"
    ) or entitlements.get("application-identifier")
    if not isinstance(profile_app_id, str) or not _profile_allows(
        EXPECTED_APPLICATION_ID,
        [profile_app_id],
    ):
        errors.append("profile does not authorize the application ID")
    if not _profile_allows(
        EXPECTED_APPLICATION_ID,
        entitlements.get("keychain-access-groups"),
    ):
        errors.append("profile does not authorize the keychain group")


def _verify_profile(
    app: Path,
    *,
    minimum_valid_days: int,
    errors: list[str],
) -> tuple[str | None, str | None]:
    profile_path = app / "Contents" / "embedded.provisionprofile"
    if not profile_path.is_file():
        errors.append("embedded provisioning profile is missing")
        return None, None
    profile, profile_error = _plist_from_command(
        ["/usr/bin/security", "cms", "-D", "-i", str(profile_path)]
    )
    if profile is None:
        errors.append(
            "embedded provisioning profile is unreadable"
            + (f": {profile_error}" if profile_error else "")
        )
        return None, None
    profile_name = str(profile.get("Name", "")).strip() or None
    profile_expires_at = _profile_expiration(
        profile,
        minimum_valid_days=minimum_valid_days,
        errors=errors,
    )
    _verify_profile_identity(profile, errors)
    _verify_profile_entitlements(profile, errors)
    return profile_name, profile_expires_at


def verify_bundle(
    executable: Path,
    *,
    minimum_valid_days: int = 7,
) -> BundleVerification:
    executable = executable.expanduser().resolve(strict=False)
    app = _find_app(executable)
    errors: list[str] = []
    bundle_id: str | None = None
    application_id: str | None = None
    profile_name: str | None = None
    profile_expires_at: str | None = None

    if app is None:
        errors.append("helper is not inside an app-like bundle")
    elif not executable.is_file():
        errors.append("helper executable is missing")
    else:
        _verify_signature(app, errors)
        bundle_id = _bundle_identifier(app, errors)
        application_id = _verify_signed_entitlements(app, errors)
        profile_name, profile_expires_at = _verify_profile(
            app,
            minimum_valid_days=minimum_valid_days,
            errors=errors,
        )

    return BundleVerification(
        ok=not errors,
        app_path=str(app) if app is not None else None,
        executable_path=str(executable),
        bundle_id=bundle_id,
        application_id=application_id,
        profile_name=profile_name,
        profile_expires_at=profile_expires_at,
        errors=tuple(errors),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--minimum-valid-days", type=int, default=7)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = verify_bundle(
        args.executable,
        minimum_valid_days=args.minimum_valid_days,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
