#!/usr/bin/env python3
"""Hardened public read-only Lightpanda worker."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from scripts.lightpanda_contract import (
        DEFAULT_CAPABILITIES,
        DEFAULT_MANIFEST,
        DEFAULT_POLICY,
        MAX_PUBLIC_URLS,
        PROJECT_ROOT,
        LightpandaError,
        manifest_asset,
        minimal_environment,
        read_json,
        read_policy,
        resolve_project_path,
        resolve_within_project,
        runtime_binary,
        runtime_capabilities,
        validate_policy,
        validate_public_url,
    )
except ModuleNotFoundError:
    from lightpanda_contract import (  # type: ignore[no-redef]
        DEFAULT_CAPABILITIES,
        DEFAULT_MANIFEST,
        DEFAULT_POLICY,
        MAX_PUBLIC_URLS,
        PROJECT_ROOT,
        LightpandaError,
        manifest_asset,
        minimal_environment,
        read_json,
        read_policy,
        resolve_project_path,
        resolve_within_project,
        runtime_binary,
        runtime_capabilities,
        validate_policy,
        validate_public_url,
    )

_PREFLIGHT_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
FORBIDDEN_SCRIPT_PATTERNS = (
    (re.compile(r"\.\s*click\s*\("), "click"),
    (re.compile(r"\.\s*fill\s*\("), "fill"),
    (re.compile(r"\.\s*press\s*\("), "press"),
    (re.compile(r"\.\s*selectOption\s*\("), "selectOption"),
    (re.compile(r"\.\s*setChecked\s*\("), "setChecked"),
    (re.compile(r"\.\s*evaluate\s*\("), "evaluate"),
    (re.compile(r"\beval\s*\("), "eval"),
    (re.compile(r"\bFunction\s*\("), "Function"),
    (re.compile(r"\bwaitForScript\s*\("), "waitForScript"),
)
STATIC_GOTO = re.compile(
    r"""\.\s*goto\s*\(\s*(['"])(https://[^'"]+)\1(?:\s*,[^)]*)?\)"""
)
ANY_GOTO = re.compile(r"\.\s*goto\s*\(")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preflight(
    manifest_path: Path = DEFAULT_MANIFEST,
    policy_path: Path = DEFAULT_POLICY,
    *,
    project_root: Path = PROJECT_ROOT,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    policy = read_policy(policy_path)
    asset = manifest_asset(manifest)
    expected_platform = str(asset["platform"])
    expected_architecture = str(asset["architecture"])
    actual_platform = sys.platform
    actual_architecture = platform.machine()
    if actual_platform != expected_platform:
        raise LightpandaError(
            f"platform mismatch: {actual_platform} != {expected_platform}"
        )
    if actual_architecture != expected_architecture:
        raise LightpandaError(
            "architecture mismatch: "
            f"{actual_architecture} != {expected_architecture}"
        )

    binary = runtime_binary(manifest, project_root=project_root)
    if not binary.is_file():
        raise LightpandaError(f"Lightpanda binary is missing: {binary}")
    if not os.access(binary, os.X_OK):
        raise LightpandaError(f"Lightpanda binary is not executable: {binary}")
    expected_digest = str(asset["sha256"])
    capabilities = runtime_capabilities(manifest)
    configuration_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "manifest": manifest,
                "policy": policy,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    metadata = binary.stat()
    cache_key = (
        str(binary),
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        expected_digest,
        str(asset["version"]),
        configuration_fingerprint,
    )
    cached = _PREFLIGHT_CACHE.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)
    actual_digest = file_sha256(binary)
    if actual_digest != expected_digest:
        raise LightpandaError(
            f"Lightpanda digest mismatch: {actual_digest}"
        )
    try:
        completed = runner(
            [str(binary), "version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=minimal_environment(policy),
        )
    except subprocess.TimeoutExpired as error:
        raise LightpandaError(
            "Lightpanda version check exceeded 10 seconds"
        ) from error
    version = completed.stdout.strip()
    if completed.returncode != 0:
        raise LightpandaError(
            "Lightpanda version check failed: "
            + completed.stderr.strip()[:500]
        )
    expected_version = str(asset["version"])
    if version != expected_version:
        raise LightpandaError(
            f"Lightpanda version mismatch: {version} != {expected_version}"
        )
    result = {
        "status": "ready",
        "role": policy.get("role"),
        "binary": str(binary),
        "version": version,
        "sha256": actual_digest,
        "telemetry": "disabled",
        "storage": "none",
        "authenticated": False,
        "capabilities": capabilities,
    }
    _PREFLIGHT_CACHE.clear()
    _PREFLIGHT_CACHE[cache_key] = copy.deepcopy(result)
    return result


def install(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    project_root: Path = PROJECT_ROOT,
    opener: Any = urllib.request.urlopen,
) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    asset = manifest_asset(manifest)
    binary = runtime_binary(manifest, project_root=project_root)
    expected_digest = str(asset["sha256"])
    expected_size = int(asset["size_bytes"])

    if binary.is_file():
        actual_digest = file_sha256(binary)
        if actual_digest == expected_digest:
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            return {
                "status": "already_installed",
                "binary": str(binary),
                "sha256": actual_digest,
            }
        raise LightpandaError(
            f"refusing to replace an unexpected binary: {binary}"
        )

    url = str(asset["url"])
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise LightpandaError("manifest asset URL must use HTTPS on github.com")
    binary.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "x-mention-watcher-lightpanda-installer"},
        )
        with opener(request, timeout=60) as response:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=binary.parent,
                prefix=".lightpanda-download-",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                total = 0
                digest = hashlib.sha256()
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected_size:
                        raise LightpandaError(
                            "download exceeded the pinned asset size"
                        )
                    digest.update(chunk)
                    temporary.write(chunk)
        actual_digest = digest.hexdigest()
        if total != expected_size:
            raise LightpandaError(
                f"download size mismatch: {total} != {expected_size}"
            )
        if actual_digest != expected_digest:
            raise LightpandaError(
                f"download digest mismatch: {actual_digest}"
            )
        temporary_path.chmod(0o755)
        os.replace(temporary_path, binary)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return {
        "status": "installed",
        "binary": str(binary),
        "sha256": expected_digest,
    }


def build_fetch_command(
    binary: Path,
    urls: Sequence[str],
    dump_format: str,
    policy: Mapping[str, Any],
    *,
    wait_profile: str = "fast",
    capabilities: Mapping[str, Any] | None = None,
) -> list[str]:
    if not urls:
        raise LightpandaError("at least one URL is required")
    if len(urls) > int(policy["max_urls_per_run"]):
        raise LightpandaError("URL batch exceeds the configured limit")
    if dump_format not in policy["allowed_dump_formats"]:
        raise LightpandaError(f"unsupported dump format: {dump_format}")
    command = [
        str(binary),
        "fetch",
        "--json",
        "--dump",
        dump_format,
    ]
    limits = policy["limits"]
    capabilities = (
        DEFAULT_CAPABILITIES if capabilities is None else capabilities
    )
    wait_profiles = limits["wait_profiles_ms"]
    if wait_profile not in wait_profiles:
        raise LightpandaError(f"unsupported wait profile: {wait_profile}")
    if capabilities.get("fetch_terminate"):
        command.extend(["--terminate-ms", str(limits["terminate_ms"])])
    if capabilities.get("fetch_wait"):
        command.extend(
            ["--wait-ms", str(wait_profiles[wait_profile])]
        )
    if policy.get("strip_mode") and capabilities.get("fetch_strip"):
        command.extend(["--strip-mode", str(policy["strip_mode"])])
    command.extend(build_common_options(policy, capabilities))
    command.extend(urls)
    return command


def build_common_options(
    policy: Mapping[str, Any],
    capabilities: Mapping[str, Any] | None = None,
) -> list[str]:
    limits = policy["limits"]
    capabilities = (
        DEFAULT_CAPABILITIES if capabilities is None else capabilities
    )
    command = [
        "--storage-engine",
        "none",
        "--http-connect-timeout",
        str(limits["http_connect_timeout_ms"]),
        "--http-timeout",
        str(limits["http_timeout_ms"]),
        "--http-max-response-size",
        str(limits["http_max_response_size"]),
        "--http-max-concurrent",
        str(limits["http_max_concurrent"]),
        "--http-max-host-open",
        str(limits["http_max_host_open"]),
        "--ws-max-concurrent",
        str(limits["websocket_max_concurrent"]),
        "--log-level",
        "error",
        "--user-agent-suffix",
        str(policy["user_agent_suffix"]),
    ]
    if capabilities.get("v8_max_heap"):
        command.extend(
            ["--v8-max-heap-mb", str(limits["v8_max_heap_mb"])]
        )
    if capabilities.get("watchdog"):
        command.extend(["--watchdog-ms", str(limits["watchdog_ms"])])
    if policy.get("block_private_networks"):
        command.append("--block-private-networks")
    if policy.get("disable_subframes"):
        command.append("--disable-subframes")
    if policy.get("disable_workers"):
        command.append("--disable-workers")
    if policy.get("obey_robots"):
        command.append("--obey-robots")
    blocked_patterns = policy.get("blocked_url_patterns", ())
    if blocked_patterns and capabilities.get("block_urls"):
        command.extend(
            ["--block-urls", ",".join(str(item) for item in blocked_patterns)]
        )
    return command


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        values = payload["results"]
    else:
        values = [payload]
    if not all(isinstance(value, dict) for value in values):
        raise LightpandaError("Lightpanda returned an unexpected JSON shape")
    return values


def classify_record(
    record: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    raw_content = str(record.get("content") or "")
    maximum = int(policy["max_output_characters_per_page"])
    content_truncated = len(raw_content) > maximum
    content = raw_content[:maximum]
    searchable_content = re.sub(
        r"\\([\\`*_{}\[\]()#+\-.!])",
        r"\1",
        raw_content,
    )
    status = int(record.get("http_status") or 0)
    application_errors = [
        marker
        for marker in policy["application_error_markers"]
        if str(marker).casefold() in searchable_content.casefold()
    ]
    partial_markers = [
        marker
        for marker in policy["partial_content_markers"]
        if str(marker).casefold() in searchable_content.casefold()
    ]
    authentication_markers = [
        marker
        for marker in policy["authentication_required_markers"]
        if str(marker).casefold() in searchable_content.casefold()
    ]
    robots_block_markers = [
        marker
        for marker in policy["robots_block_markers"]
        if str(marker).casefold() in searchable_content.casefold()
    ]
    injection_signals = [
        marker
        for marker in policy["prompt_injection_markers"]
        if str(marker).casefold() in searchable_content.casefold()
    ]
    reasons: list[str] = []
    if status < 200 or status >= 300:
        reasons.append(f"http_status_{status}")
    if not content.strip():
        reasons.append("empty_content")
    if application_errors:
        reasons.append("application_error_document")
    final_url = str(record.get("url") or "")
    try:
        final_parts = urllib.parse.urlsplit(final_url)
    except ValueError:
        final_parts = urllib.parse.SplitResult("", "", "", "", "")
    if not final_url:
        reasons.append("missing_final_url")
    else:
        try:
            validate_public_url(final_url, policy)
        except LightpandaError:
            reasons.append(
                "insecure_redirect"
                if final_parts.scheme.lower() != "https"
                else "unsafe_final_url"
            )
    x_login_redirect = (
        final_parts.hostname in {"x.com", "www.x.com"}
        and final_parts.path.startswith("/i/jf/onboarding/")
    )
    if x_login_redirect or len(authentication_markers) >= 2:
        reasons.append("authentication_required")
    if robots_block_markers:
        reasons.append("robots_blocked")
    record_status = "ok"
    if reasons:
        record_status = "fallback_required"
    elif partial_markers or content_truncated:
        record_status = "partial"
    return {
        "url": record.get("url"),
        "http_status": status,
        "dump": record.get("dump"),
        "content": content,
        "content_trust": "untrusted_web_content",
        "automation_safe": not injection_signals,
        "prompt_injection_signals": injection_signals,
        "application_error_markers": application_errors,
        "authentication_required_markers": authentication_markers,
        "robots_block_markers": robots_block_markers,
        "partial_content_markers": partial_markers,
        "content_truncated": content_truncated,
        "completeness": (
            "partial"
            if partial_markers or content_truncated
            else "complete"
        ),
        "limitations": (
            ["output_truncated"] if content_truncated else []
        ),
        "status": record_status,
        "reasons": reasons,
    }


def parse_fetch_output(
    output: str,
    policy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise LightpandaError(
            f"Lightpanda returned invalid JSON: {error}"
        ) from error
    return [classify_record(record, policy) for record in _records(payload)]


def fetch_public(
    urls: Sequence[str],
    *,
    dump_format: str | None = None,
    wait_profile: str = "fast",
    manifest_path: Path = DEFAULT_MANIFEST,
    policy_path: Path = DEFAULT_POLICY,
    project_root: Path = PROJECT_ROOT,
    runner: Any = subprocess.run,
    verify_runtime: bool = True,
) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    policy = read_policy(policy_path)
    if isinstance(urls, (str, bytes)) or not urls:
        raise LightpandaError("at least one URL is required")
    if len(urls) > min(
        int(policy["max_urls_per_run"]),
        MAX_PUBLIC_URLS,
    ):
        raise LightpandaError("URL batch exceeds the configured limit")
    validated = [validate_public_url(url, policy) for url in urls]
    if verify_runtime:
        preflight(
            manifest_path,
            policy_path,
            project_root=project_root,
            runner=runner,
        )
    selected_format = dump_format or str(policy["default_dump_format"])
    binary = runtime_binary(manifest, project_root=project_root)
    capabilities = runtime_capabilities(manifest)
    batches = (
        [validated]
        if capabilities.get("multi_fetch")
        else [[url] for url in validated]
    )
    records: list[dict[str, Any]] = []
    stderr_parts: list[str] = []
    for batch in batches:
        command = build_fetch_command(
            binary,
            batch,
            selected_format,
            policy,
            wait_profile=wait_profile,
            capabilities=capabilities,
        )
        process_timeout = int(
            policy["limits"]["process_timeout_seconds"]
        )
        try:
            completed = runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=process_timeout,
                env=minimal_environment(policy),
            )
        except subprocess.TimeoutExpired as error:
            raise LightpandaError(
                f"Lightpanda fetch exceeded {process_timeout} seconds"
            ) from error
        if completed.returncode != 0:
            raise LightpandaError(
                "Lightpanda fetch failed: "
                + completed.stderr.strip()[:1000]
            )
        records.extend(parse_fetch_output(completed.stdout, policy))
        if completed.stderr.strip():
            stderr_parts.append(completed.stderr.strip())
    if len(records) != len(validated):
        raise LightpandaError(
            "Lightpanda result count does not match the requested URL count"
        )
    for requested_url, record in zip(validated, records):
        record["requested_url"] = requested_url
        record["redirected"] = record.get("url") != requested_url
    statuses = {item["status"] for item in records}
    if statuses == {"ok"}:
        overall_status = "ok"
    elif statuses.issubset({"ok", "partial"}):
        overall_status = "partial"
    else:
        overall_status = "fallback_required"
    return {
        "status": overall_status,
        "role": policy.get("role"),
        "authenticated": False,
        "robots_policy": (
            "obey" if policy.get("obey_robots") else "operator_override"
        ),
        "invocation_count": len(batches),
        "records": records,
        "stderr": "\n".join(stderr_parts)[:1000],
    }


def audit_script(
    script_path: Path,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    if script_path.stat().st_size > int(policy["max_script_size_bytes"]):
        return {
            "status": "fail",
            "script": str(script_path),
            "urls": [],
            "violations": ["script_too_large"],
        }
    source = script_path.read_text(encoding="utf-8")
    violations = [
        name
        for pattern, name in FORBIDDEN_SCRIPT_PATTERNS
        if pattern.search(source)
    ]
    static_gotos = list(STATIC_GOTO.finditer(source))
    if len(static_gotos) != len(ANY_GOTO.findall(source)):
        violations.append("dynamic_goto")
    urls: list[str] = []
    for match in static_gotos:
        value = match.group(2)
        try:
            urls.append(validate_public_url(value, policy))
        except LightpandaError as error:
            violations.append(f"unsafe_goto:{error}")
    return {
        "status": "pass" if not violations and urls else "fail",
        "script": str(script_path),
        "urls": urls,
        "violations": sorted(set(violations)),
    }


def run_script(
    script_path: Path,
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    policy_path: Path = DEFAULT_POLICY,
    project_root: Path = PROJECT_ROOT,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    policy = read_policy(policy_path)
    preflight(
        manifest_path,
        policy_path,
        project_root=project_root,
        runner=runner,
    )
    allowed_root = resolve_within_project(
        project_root,
        str(policy["allowed_script_root"]),
        label="PandaScript root",
    )
    resolved_script = script_path.resolve()
    try:
        resolved_script.relative_to(allowed_root)
    except ValueError as error:
        raise LightpandaError(
            f"PandaScript is outside the allowed root: {allowed_root}"
        ) from error
    audit = audit_script(resolved_script, policy)
    if audit["status"] != "pass":
        raise LightpandaError(
            "PandaScript audit failed: "
            + ", ".join(audit["violations"])
        )
    binary = runtime_binary(manifest, project_root=project_root)
    capabilities = runtime_capabilities(manifest)
    command = [
        str(binary),
        str(capabilities["script_subcommand"]),
        str(resolved_script),
    ]
    command.extend(build_common_options(policy, capabilities))
    process_timeout = int(policy["limits"]["process_timeout_seconds"])
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=process_timeout,
            env=minimal_environment(policy),
        )
    except subprocess.TimeoutExpired as error:
        raise LightpandaError(
            f"PandaScript exceeded {process_timeout} seconds"
        ) from error
    if completed.returncode != 0:
        raise LightpandaError(
            "PandaScript failed: " + completed.stderr.strip()[:1000]
        )
    return {
        "status": "ok",
        "audit": audit,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip()[:1000],
    }


def doctor(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    policy_path: Path = DEFAULT_POLICY,
    project_root: Path = PROJECT_ROOT,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    runtime = preflight(
        manifest_path,
        policy_path,
        project_root=project_root,
        runner=runner,
    )
    policy = read_policy(policy_path)
    audits: list[dict[str, Any]] = []
    for definition in policy.get("canaries", {}).values():
        script = resolve_within_project(
            project_root,
            str(definition["script"]),
            label="Canary script",
        )
        audits.append(audit_script(script, policy))
    status = (
        "pass"
        if audits and all(item["status"] == "pass" for item in audits)
        else "fail"
    )
    return {
        "status": status,
        "runtime": runtime,
        "canary_audits": audits,
    }


def validate_canary_output(
    output: str,
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise LightpandaError("canary produced no output")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as error:
        raise LightpandaError(
            f"canary output is not JSON: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise LightpandaError("canary output must be a JSON object")
    mismatches = {
        key: {"expected": value, "actual": payload.get(key)}
        for key, value in expected.items()
        if payload.get(key) != value
    }
    if mismatches:
        raise LightpandaError(
            "canary output mismatch: "
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
        )
    return payload


def canary(
    name: str = "public_read",
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    policy_path: Path = DEFAULT_POLICY,
    project_root: Path = PROJECT_ROOT,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    policy = read_policy(policy_path)
    definition = policy.get("canaries", {}).get(name)
    if not isinstance(definition, Mapping):
        raise LightpandaError(f"unknown canary: {name}")
    script = resolve_within_project(
        project_root,
        str(definition["script"]),
        label="Canary script",
    )
    execution = run_script(
        script,
        manifest_path=manifest_path,
        policy_path=policy_path,
        project_root=project_root,
        runner=runner,
    )
    payload = validate_canary_output(
        execution["stdout"],
        definition.get("expected", {}),
    )
    return {
        "status": "pass",
        "name": name,
        "payload": payload,
        "execution": execution,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    root.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("install")
    commands.add_parser("preflight")
    commands.add_parser("doctor")
    canary_parser = commands.add_parser("canary")
    canary_parser.add_argument("--name", default="public_read")
    fetch_parser = commands.add_parser("fetch")
    fetch_parser.add_argument(
        "--format",
        dest="dump_format",
        choices=("html", "markdown", "semantic_tree", "semantic_tree_text"),
    )
    fetch_parser.add_argument(
        "--profile",
        dest="wait_profile",
        choices=("fast", "thread"),
        default="fast",
    )
    fetch_parser.add_argument("urls", nargs="+")
    audit_parser = commands.add_parser("audit-script")
    audit_parser.add_argument("script", type=Path)
    run_parser = commands.add_parser("run-script")
    run_parser.add_argument("script", type=Path)
    return root


def main(argv: Iterable[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "install":
            result = install(arguments.manifest)
        elif arguments.command == "preflight":
            result = preflight(arguments.manifest, arguments.policy)
        elif arguments.command == "doctor":
            result = doctor(
                manifest_path=arguments.manifest,
                policy_path=arguments.policy,
            )
        elif arguments.command == "canary":
            result = canary(
                arguments.name,
                manifest_path=arguments.manifest,
                policy_path=arguments.policy,
            )
        elif arguments.command == "fetch":
            result = fetch_public(
                arguments.urls,
                dump_format=arguments.dump_format,
                wait_profile=arguments.wait_profile,
                manifest_path=arguments.manifest,
                policy_path=arguments.policy,
            )
        elif arguments.command == "audit-script":
            policy = read_policy(arguments.policy)
            result = audit_script(arguments.script, policy)
        elif arguments.command == "run-script":
            result = run_script(
                arguments.script,
                manifest_path=arguments.manifest,
                policy_path=arguments.policy,
            )
        else:
            raise AssertionError(arguments.command)
    except (LightpandaError, OSError, subprocess.SubprocessError) as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_class": type(error).__name__,
                    "error": str(error),
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
