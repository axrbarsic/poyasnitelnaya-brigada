#!/usr/bin/env python3
"""Resolve and update the runtime voice policy for X autopilot replies."""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from scripts import json_contract
except ModuleNotFoundError:
    import json_contract  # type: ignore[no-redef]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = PROJECT_ROOT / "personality" / "policy.json"
DEFAULT_RUNTIME = PROJECT_ROOT / "var" / "personality-overrides.json"
FORBIDDEN_CHARACTERS = ("\u2013", "\u2014")
VALID_SCOPES = {"global", "topic", "conversation", "author"}


def read_json(path: Path, *, missing: Any = None) -> Any:
    try:
        return json_contract.read(path)
    except FileNotFoundError:
        if missing is not None:
            return missing
        raise


atomic_write_json = json_contract.atomic_write


def validate_instruction(value: str) -> str:
    instruction = " ".join(value.strip().split())
    if not instruction:
        raise ValueError("instruction must not be empty")
    if len(instruction) > 500:
        raise ValueError("instruction must not exceed 500 characters")
    if any(character in instruction for character in FORBIDDEN_CHARACTERS):
        raise ValueError("instruction contains a forbidden dash character")
    return instruction


def validate_policy(policy: dict[str, Any]) -> None:
    if policy.get("schema_version") != 1:
        raise ValueError("unsupported personality policy schema")
    default_profile = policy.get("default_profile")
    if not isinstance(default_profile, dict):
        raise ValueError("default_profile must be an object")
    instructions = default_profile.get("instructions")
    if not isinstance(instructions, list) or not instructions:
        raise ValueError("default_profile instructions must be a non-empty list")
    for instruction in instructions:
        validate_instruction(str(instruction))
    profiles = policy.get("topic_profiles")
    if not isinstance(profiles, list):
        raise ValueError("topic_profiles must be a list")
    identifiers: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError("topic profile must be an object")
        identifier = str(profile.get("id", "")).strip()
        if not identifier or identifier in identifiers:
            raise ValueError("topic profile IDs must be unique and non-empty")
        identifiers.add(identifier)
        keywords = profile.get("keywords")
        if not isinstance(keywords, list) or not keywords:
            raise ValueError(f"topic profile {identifier} has no keywords")
        for instruction in profile.get("instructions", []):
            validate_instruction(str(instruction))
    for key in ("conversation_profiles", "author_profiles"):
        if not isinstance(policy.get(key, {}), dict):
            raise ValueError(f"{key} must be an object")


def load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise ValueError("personality policy must be an object")
    validate_policy(payload)
    return payload


def load_runtime(path: Path = DEFAULT_RUNTIME) -> dict[str, Any]:
    payload = read_json(
        path,
        missing={"schema_version": 1, "revision": 0, "overrides": []},
    )
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported runtime personality schema")
    overrides = payload.get("overrides")
    if not isinstance(overrides, list):
        raise ValueError("runtime overrides must be a list")
    return payload


def event_text(event: dict[str, Any]) -> str:
    values: list[str] = []
    for key in ("text", "note_tweet_text", "context", "quoted_text"):
        value = event.get(key)
        if isinstance(value, str):
            values.append(value)
    return "\n".join(values).casefold()


def profile_instructions(profile: Any) -> list[str]:
    if isinstance(profile, dict):
        values = profile.get("instructions", [])
    else:
        values = profile
    if not isinstance(values, list):
        return []
    return [validate_instruction(str(value)) for value in values]


def runtime_override_matches(
    override: dict[str, Any],
    event: dict[str, Any],
    matched_topic_ids: set[str],
) -> bool:
    if not override.get("enabled", True):
        return False
    scope = str(override.get("scope", "")).strip()
    selector = str(override.get("selector", "")).strip().casefold()
    author_selector = str(
        override.get("author_selector", "")
    ).strip().lstrip("@").casefold()
    if author_selector:
        username = str(
            event.get("username")
            or event.get("author_username")
            or ""
        ).strip().lstrip("@").casefold()
        if username != author_selector:
            return False
    if scope == "global":
        return True
    if scope == "topic":
        return selector in {value.casefold() for value in matched_topic_ids}
    if scope == "conversation":
        return selector == str(event.get("conversation_id", "")).casefold()
    if scope == "author":
        username = str(
            event.get("username")
            or event.get("author_username")
            or ""
        ).lstrip("@")
        return selector.lstrip("@") == username.casefold()
    return False


def unique_instructions(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        instruction = validate_instruction(value)
        key = instruction.casefold()
        if key not in seen:
            seen.add(key)
            result.append(instruction)
    return result


def resolve_event_policy(
    event: dict[str, Any],
    *,
    policy_path: Path = DEFAULT_POLICY,
    runtime_path: Path = DEFAULT_RUNTIME,
) -> dict[str, Any]:
    policy = load_policy(policy_path)
    runtime = load_runtime(runtime_path)
    text = event_text(event)
    topic_profiles: list[dict[str, Any]] = []
    for profile in policy.get("topic_profiles", []):
        keywords = [
            str(value).strip().casefold()
            for value in profile.get("keywords", [])
            if str(value).strip()
        ]
        if any(keyword in text for keyword in keywords):
            topic_profiles.append(profile)
    topic_profiles.sort(
        key=lambda item: (
            int(item.get("priority", 0)),
            str(item.get("id", "")),
        )
    )
    matched_topic_ids = {
        str(profile.get("id", "")).strip() for profile in topic_profiles
    }
    sources = ["default"]
    instructions = profile_instructions(policy["default_profile"])
    for profile in topic_profiles:
        profile_id = str(profile["id"])
        sources.append(f"topic:{profile_id}")
        instructions.extend(profile_instructions(profile))

    conversation_id = str(event.get("conversation_id", "")).strip()
    conversation_profile = policy.get("conversation_profiles", {}).get(
        conversation_id
    )
    if conversation_profile:
        sources.append(f"conversation:{conversation_id}")
        instructions.extend(profile_instructions(conversation_profile))

    username = str(
        event.get("username") or event.get("author_username") or ""
    ).lstrip("@")
    author_profile = policy.get("author_profiles", {}).get(username.casefold())
    if author_profile:
        sources.append(f"author:{username.casefold()}")
        instructions.extend(profile_instructions(author_profile))

    applied_overrides: list[str] = []
    for override in runtime.get("overrides", []):
        if not isinstance(override, dict):
            continue
        if runtime_override_matches(override, event, matched_topic_ids):
            override_id = str(override.get("id", "")).strip()
            if override_id:
                applied_overrides.append(override_id)
                sources.append(f"runtime:{override_id}")
            instructions.extend(
                profile_instructions(
                    {"instructions": override.get("instructions", [])}
                )
            )

    return {
        "policy_id": str(policy.get("policy_id", "personality-policy")),
        "policy_revision": int(runtime.get("revision", 0)),
        "matched_topics": sorted(matched_topic_ids),
        "applied_runtime_overrides": applied_overrides,
        "sources": sources,
        "instructions": unique_instructions(instructions),
    }


def resolve_batch(
    events: Iterable[dict[str, Any]],
    *,
    policy_path: Path = DEFAULT_POLICY,
    runtime_path: Path = DEFAULT_RUNTIME,
) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for index, event in enumerate(events):
        event_id = str(event.get("id") or event.get("event_id") or index)
        resolved[event_id] = resolve_event_policy(
            event,
            policy_path=policy_path,
            runtime_path=runtime_path,
        )
    return {
        "precedence": (
            "Стиль ниже фактов, безопасности, правил X, запрета дублей "
            "и требований durable resolve."
        ),
        "events": resolved,
    }


def add_override(
    runtime_path: Path,
    *,
    scope: str,
    selector: str,
    instruction: str,
    label: str = "",
    author_selector: str = "",
) -> dict[str, Any]:
    if scope not in VALID_SCOPES:
        raise ValueError(f"unsupported scope: {scope}")
    normalized_selector = selector.strip()
    if scope != "global" and not normalized_selector:
        raise ValueError(f"selector is required for scope {scope}")
    clean_instruction = validate_instruction(instruction)
    runtime = load_runtime(runtime_path)
    revision = int(runtime.get("revision", 0)) + 1
    override = {
        "id": f"voice-{uuid.uuid4().hex[:12]}",
        "scope": scope,
        "selector": normalized_selector,
        "author_selector": author_selector.strip().lstrip("@"),
        "label": label.strip(),
        "instructions": [clean_instruction],
        "enabled": True,
        "created_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    runtime["revision"] = revision
    runtime["overrides"].append(override)
    atomic_write_json(runtime_path, runtime)
    return override


def disable_override(runtime_path: Path, override_id: str) -> dict[str, Any]:
    runtime = load_runtime(runtime_path)
    for override in runtime["overrides"]:
        if isinstance(override, dict) and override.get("id") == override_id:
            override["enabled"] = False
            runtime["revision"] = int(runtime.get("revision", 0)) + 1
            atomic_write_json(runtime_path, runtime)
            return override
    raise ValueError(f"runtime override not found: {override_id}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    root.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    subparsers = root.add_subparsers(dest="command", required=True)

    show = subparsers.add_parser("show")
    show.add_argument("--json", action="store_true")

    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--text", default="")
    resolve.add_argument("--conversation-id", default="")
    resolve.add_argument("--author", default="")
    resolve.add_argument("--json", action="store_true")

    set_parser = subparsers.add_parser("set")
    set_parser.add_argument("--scope", choices=sorted(VALID_SCOPES), required=True)
    set_parser.add_argument("--selector", default="")
    set_parser.add_argument("--instruction", required=True)
    set_parser.add_argument("--label", default="")
    set_parser.add_argument("--author-selector", default="")

    disable = subparsers.add_parser("disable")
    disable.add_argument("--id", required=True)
    return root


def main() -> int:
    arguments = parser().parse_args()
    policy_path = arguments.policy.expanduser().resolve()
    runtime_path = arguments.runtime.expanduser().resolve()
    if arguments.command == "show":
        payload = {
            "policy": load_policy(policy_path),
            "runtime": load_runtime(runtime_path),
        }
    elif arguments.command == "resolve":
        payload = resolve_event_policy(
            {
                "text": arguments.text,
                "conversation_id": arguments.conversation_id,
                "username": arguments.author,
            },
            policy_path=policy_path,
            runtime_path=runtime_path,
        )
    elif arguments.command == "set":
        payload = add_override(
            runtime_path,
            scope=arguments.scope,
            selector=arguments.selector,
            instruction=arguments.instruction,
            label=arguments.label,
            author_selector=arguments.author_selector,
        )
    elif arguments.command == "disable":
        payload = disable_override(runtime_path, arguments.id)
    else:
        raise AssertionError(arguments.command)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
