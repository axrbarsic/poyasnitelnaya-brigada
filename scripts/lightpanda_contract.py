#!/usr/bin/env python3
"""Validated security contract for the read-only Lightpanda worker."""

from __future__ import annotations

import ipaddress
import json
import os
import urllib.parse
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "lightpanda" / "runtime-manifest.json"
DEFAULT_POLICY = PROJECT_ROOT / "lightpanda" / "policy.json"
SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_CAPABILITIES = {
    "script_subcommand": "run",
    "multi_fetch": True,
    "block_urls": True,
    "v8_max_heap": True,
    "watchdog": True,
    "fetch_terminate": True,
    "fetch_wait": True,
    "fetch_strip": True,
}
MAX_PUBLIC_URLS = 8
SAFE_ENVIRONMENT_KEYS = frozenset(
    {
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
    }
)
REQUIRED_BLOCKED_HOST_SUFFIXES = frozenset(
    {
        ".localhost",
        ".local",
        ".internal",
        ".lan",
        ".home",
        ".arpa",
    }
)


class LightpandaError(RuntimeError):
    """Raised when the worker cannot safely complete an operation."""


def read_json(path: Path) -> dict[str, Any]:
    def reject_duplicate_keys(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise LightpandaError(
                    f"duplicate JSON key in {path}: {key}"
                )
            result[key] = value
        return result

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except json.JSONDecodeError as error:
        raise LightpandaError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(payload, dict):
        raise LightpandaError(f"expected a JSON object: {path}")
    if payload.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise LightpandaError(f"unsupported schema version: {path}")
    return payload


def validate_policy(policy: Mapping[str, Any]) -> Mapping[str, Any]:
    if policy.get("role") != "public-read-only-worker":
        raise LightpandaError("unexpected Lightpanda worker role")
    if set(policy.get("allowed_schemes", ())) != {"https"}:
        raise LightpandaError("policy must allow only HTTPS")
    if set(policy.get("allowed_ports", ())) != {443}:
        raise LightpandaError("policy must allow only port 443")
    for name in (
        "block_private_networks",
        "disable_subframes",
        "disable_workers",
        "obey_robots",
    ):
        if policy.get(name) is not True:
            raise LightpandaError(f"required policy flag is not true: {name}")
    try:
        maximum_urls = int(policy["max_urls_per_run"])
    except (KeyError, TypeError, ValueError) as error:
        raise LightpandaError("invalid max_urls_per_run") from error
    if maximum_urls < 1 or maximum_urls > MAX_PUBLIC_URLS:
        raise LightpandaError(
            f"max_urls_per_run must be between 1 and {MAX_PUBLIC_URLS}"
        )
    suffixes = {
        str(value).lower()
        for value in policy.get("blocked_host_suffixes", ())
    }
    missing_suffixes = sorted(REQUIRED_BLOCKED_HOST_SUFFIXES - suffixes)
    if missing_suffixes:
        raise LightpandaError(
            "policy is missing blocked hostname suffixes: "
            + ", ".join(missing_suffixes)
        )
    allowed_environment = policy.get("environment_allowlist")
    if not isinstance(allowed_environment, list) or not all(
        isinstance(value, str) for value in allowed_environment
    ):
        raise LightpandaError("environment_allowlist must be an array of strings")
    unsafe_environment = sorted(
        set(allowed_environment) - SAFE_ENVIRONMENT_KEYS
    )
    if unsafe_environment:
        raise LightpandaError(
            "unsafe environment keys are forbidden: "
            + ", ".join(unsafe_environment)
        )
    return policy


def read_policy(path: Path) -> dict[str, Any]:
    policy = read_json(path)
    validate_policy(policy)
    return policy


def resolve_project_path(root: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return root / candidate


def resolve_within_project(
    root: Path,
    value: str,
    *,
    label: str,
) -> Path:
    resolved_root = root.resolve()
    candidate = resolve_project_path(resolved_root, value).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise LightpandaError(f"{label} must stay inside the project root")
    return candidate


def manifest_asset(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    asset = manifest.get("asset")
    if not isinstance(asset, Mapping):
        raise LightpandaError("manifest asset is missing")
    required = {
        "architecture",
        "install_path",
        "platform",
        "sha256",
        "size_bytes",
        "url",
        "version",
    }
    missing = sorted(required.difference(asset))
    if missing:
        raise LightpandaError(
            "manifest asset fields are missing: " + ", ".join(missing)
        )
    return asset


def runtime_capabilities(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    configured = manifest.get("capabilities", {})
    if not isinstance(configured, Mapping):
        raise LightpandaError("manifest capabilities must be an object")
    unknown = sorted(set(configured).difference(DEFAULT_CAPABILITIES))
    if unknown:
        raise LightpandaError(
            "unknown runtime capabilities: " + ", ".join(unknown)
        )
    missing = sorted(set(DEFAULT_CAPABILITIES).difference(configured))
    if missing:
        raise LightpandaError(
            "runtime capabilities must be explicit: " + ", ".join(missing)
        )
    result = dict(DEFAULT_CAPABILITIES)
    result.update(configured)
    if result["script_subcommand"] not in {"agent", "run"}:
        raise LightpandaError("unsupported script subcommand")
    for name, value in result.items():
        if name == "script_subcommand":
            continue
        if not isinstance(value, bool):
            raise LightpandaError(
                f"runtime capability must be boolean: {name}"
            )
    return result


def runtime_binary(
    manifest: Mapping[str, Any],
    *,
    project_root: Path = PROJECT_ROOT,
) -> Path:
    asset = manifest_asset(manifest)
    return resolve_within_project(
        project_root,
        str(asset["install_path"]),
        label="Lightpanda install path",
    )


def minimal_environment(
    policy: Mapping[str, Any],
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if source is None else source
    allowed = policy.get("environment_allowlist", ())
    if not isinstance(allowed, list):
        raise LightpandaError("environment_allowlist must be an array")
    unsafe = sorted(
        {
            key
            for key in allowed
            if not isinstance(key, str) or key not in SAFE_ENVIRONMENT_KEYS
        },
        key=str,
    )
    if unsafe:
        raise LightpandaError(
            "unsafe environment keys are forbidden: "
            + ", ".join(str(key) for key in unsafe)
        )
    result = {
        key: str(source[key])
        for key in allowed
        if isinstance(key, str) and key in source
    }
    result["LIGHTPANDA_DISABLE_TELEMETRY"] = "true"
    return result


def validate_public_url(
    value: str,
    policy: Mapping[str, Any],
) -> str:
    if not isinstance(value, str):
        raise LightpandaError("URL must be a string")
    if len(value) > int(policy["max_url_length"]):
        raise LightpandaError("URL exceeds the configured length limit")
    if any(ord(character) < 32 for character in value):
        raise LightpandaError("URL contains a control character")
    if "\\" in value or any(character.isspace() for character in value):
        raise LightpandaError("URL contains whitespace or a backslash")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise LightpandaError(f"invalid URL: {error}") from error
    if (
        parsed.scheme.lower() != "https"
        or parsed.scheme.lower() not in policy["allowed_schemes"]
    ):
        raise LightpandaError("only public HTTPS URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise LightpandaError("URL credentials are forbidden")
    if not parsed.hostname:
        raise LightpandaError("URL hostname is missing")
    try:
        hostname = (
            parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        )
    except UnicodeError as error:
        raise LightpandaError("URL hostname is not valid IDNA") from error
    if not hostname:
        raise LightpandaError("URL hostname is empty after normalization")
    if "%" in hostname:
        raise LightpandaError("percent-encoded hostname is forbidden")
    if hostname == "localhost":
        raise LightpandaError("localhost is forbidden")
    blocked_suffixes = REQUIRED_BLOCKED_HOST_SUFFIXES.union(
        str(value).lower()
        for value in policy["blocked_host_suffixes"]
    )
    for suffix in blocked_suffixes:
        if hostname.endswith(str(suffix).lower()):
            raise LightpandaError(f"blocked hostname suffix: {suffix}")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise LightpandaError("non-public IP address is forbidden")
    effective_port = 443 if port is None else port
    if effective_port != 443 or effective_port not in policy["allowed_ports"]:
        raise LightpandaError(f"port is forbidden: {effective_port}")
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )
