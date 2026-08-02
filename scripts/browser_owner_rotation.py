#!/usr/bin/env python3
"""Transactional rotation for the single Codex Browser-owner task."""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts import (
        automation_target_health,
        autopilot_dispatch,
        codex_thread_state,
        json_contract,
    )
except ModuleNotFoundError:
    import automation_target_health  # type: ignore[no-redef]
    import autopilot_dispatch  # type: ignore[no-redef]
    import codex_thread_state  # type: ignore[no-redef]
    import json_contract  # type: ignore[no-redef]


OPEN_PHASES = {"prepared", "thread_created", "committed"}
REASONING_EFFORT_ORDER = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    current = value or utc_now()
    return current.isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json_contract.read_object(path)


def resolve_path(config_path: Path, value: str) -> Path:
    return autopilot_dispatch.resolve_path(config_path, value)


def rotation_state_path(
    config_path: Path,
    config: dict[str, Any] | None = None,
) -> Path:
    effective = config or read_json(config_path)
    return resolve_path(
        config_path,
        str(
            effective.get(
                "autopilot_owner_rotation_state_file",
                "var/browser-owner-rotation.json",
            )
        ),
    )


def health_path(config_path: Path, config: dict[str, Any]) -> Path:
    return resolve_path(
        config_path,
        str(config.get("autopilot_health_file", "var/autopilot-health.json")),
    )


def codex_home(config: dict[str, Any]) -> Path:
    configured = str(config.get("codex_home", "")).strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def automation_path(config: dict[str, Any], automation_id: str) -> Path:
    return codex_home(config) / "automations" / automation_id / "automation.toml"


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "generation": 0,
        "transaction": None,
        "last_rotation": None,
        "last_failure": None,
    }


def load_state(path: Path) -> dict[str, Any]:
    state = default_state()
    state.update(read_json(path))
    transaction = state.get("transaction")
    if transaction is not None and not isinstance(transaction, dict):
        raise ValueError("rotation transaction must be an object or null")
    return state


def _owner_contract(contract: dict[str, Any]) -> dict[str, Any]:
    owner = contract.get("threads", {}).get("browser_owner")
    if not isinstance(owner, dict):
        raise ValueError("browser_owner contract is required")
    return owner


def _active_relay_contract(contract: dict[str, Any]) -> dict[str, Any]:
    relay = contract.get("automations", {}).get("active_relay")
    if not isinstance(relay, dict):
        raise ValueError("active_relay contract is required")
    return relay


def _current_owner(config: dict[str, Any]) -> str:
    thread_id = str(config.get("browser_owner_thread_id", "")).strip()
    if not thread_id:
        raise ValueError("browser_owner_thread_id is required")
    return thread_id


def _rotation_due(config_path: Path, config: dict[str, Any]) -> tuple[bool, int]:
    health = read_json(health_path(config_path, config))
    handoff_count = int(health.get("handoff_count", health.get("completed_runs", 0)))
    threshold = int(config.get("autopilot_owner_rotation_after_runs", 20))
    if threshold <= 0:
        raise ValueError("autopilot_owner_rotation_after_runs must be positive")
    return handoff_count >= threshold, handoff_count


def rotation_marker(rotation_token: str) -> str:
    return f"X_OWNER_ROTATION_TOKEN={rotation_token}"


def _reasoning_meets_minimum(actual: Any, minimum: Any) -> bool:
    try:
        return REASONING_EFFORT_ORDER.index(
            str(actual)
        ) >= REASONING_EFFORT_ORDER.index(str(minimum))
    except ValueError:
        return False


def _validate_new_owner(
    config: dict[str, Any],
    contract: dict[str, Any],
    thread_id: str,
) -> dict[str, Any]:
    owner = _owner_contract(contract)
    row = codex_thread_state.thread_row(codex_home(config), thread_id)
    if row is None:
        raise ValueError("new owner thread is missing from Codex state")
    mismatches: list[str] = []
    if int(row.get("archived", 0)) != 0:
        mismatches.append("archived")
    if row.get("title") != owner.get("title"):
        mismatches.append("title")
    if row.get("model") != owner.get("model"):
        mismatches.append("model")
    if not _reasoning_meets_minimum(
        row.get("reasoning_effort"),
        owner.get("minimum_reasoning_effort"),
    ):
        mismatches.append("reasoning_effort")
    expected_cwd = Path(str(owner.get("cwd", ""))).expanduser().resolve()
    actual_cwd = Path(str(row.get("cwd", ""))).expanduser().resolve()
    if actual_cwd != expected_cwd:
        mismatches.append("cwd")
    if mismatches:
        raise ValueError(
            "new owner thread differs from contract: " + ", ".join(mismatches)
        )
    return row


def _old_owner_is_archived(config: dict[str, Any], thread_id: str) -> bool:
    row = codex_thread_state.thread_row(codex_home(config), thread_id)
    return row is not None and int(row.get("archived", 0)) == 1


def _automation_target(config: dict[str, Any], automation_id: str) -> str:
    path = automation_path(config, automation_id)
    if not path.is_file():
        raise ValueError(f"automation is missing: {automation_id}")
    payload = automation_target_health.parse_automation(path)
    return str(payload.get("target_thread_id", "")).strip()


def _next_action(
    config: dict[str, Any],
    contract: dict[str, Any],
    transaction: dict[str, Any],
) -> str:
    phase = str(transaction.get("phase", ""))
    if phase == "prepared":
        return "create_thread"
    if phase == "thread_created":
        relay = _active_relay_contract(contract)
        automation_id = str(relay["id"])
        target = _automation_target(config, automation_id)
        old_thread_id = str(transaction["old_thread_id"])
        new_thread_id = str(transaction["new_thread_id"])
        if target == old_thread_id:
            return "update_automation"
        if target == new_thread_id:
            return "commit"
        raise ValueError("active relay targets neither old nor new owner")
    if phase == "committed":
        return "archive_old"
    raise ValueError(f"unsupported rotation phase: {phase}")


def _result(
    config: dict[str, Any],
    contract: dict[str, Any],
    transaction: dict[str, Any],
) -> dict[str, Any]:
    owner = _owner_contract(contract)
    prompt_source = str(owner.get("initial_prompt_source", "")).strip()
    if not prompt_source:
        raise ValueError("browser_owner initial_prompt_source is required")
    root = Path(str(contract["canonical_root"])).expanduser().resolve()
    prompt_template = (root / prompt_source).read_text(encoding="utf-8").rstrip("\n")
    marker = rotation_marker(str(transaction["rotation_token"]))
    initial_prompt = f"{marker}\n\n{prompt_template}"
    project_id = str(config.get("codex_project_id", "")).strip()
    if not project_id:
        raise ValueError("codex_project_id is required for owner rotation")
    return {
        "status": "owner_rotation_required",
        "dispatch": True,
        "route": "rotation",
        "action": _next_action(config, contract, transaction),
        "rotation_token": str(transaction["rotation_token"]),
        "old_thread_id": str(transaction["old_thread_id"]),
        "new_thread_id": transaction.get("new_thread_id"),
        "project_id": project_id,
        "owner_title": str(owner.get("title", "X: Browser owner")),
        "owner_model": str(owner["model"]),
        "owner_thinking": str(owner["minimum_reasoning_effort"]),
        "owner_rotation_marker": marker,
        "initial_prompt": initial_prompt,
    }


def reserve(
    config_path: Path,
    contract: dict[str, Any],
    *,
    allow_new: bool,
    owner_busy: bool,
    outbound_busy: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return or create one recoverable rotation transaction."""

    config_path = config_path.expanduser().resolve()
    config = read_json(config_path)
    path = rotation_state_path(config_path, config)
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        transaction = state.get("transaction")
        if isinstance(transaction, dict):
            if str(transaction.get("phase")) not in OPEN_PHASES:
                raise ValueError("rotation transaction has an invalid phase")
            return _result(config, contract, transaction)

        due, handoff_count = _rotation_due(config_path, config)
        if not due:
            return {
                "status": "owner_rotation_not_due",
                "dispatch": False,
                "route": "rotation",
                "handoff_count": handoff_count,
            }
        if not allow_new or owner_busy or outbound_busy:
            return {
                "status": "owner_rotation_waiting",
                "dispatch": False,
                "route": "rotation",
                "handoff_count": handoff_count,
                "repair_pending": not allow_new,
                "owner_busy": owner_busy,
                "outbound_busy": outbound_busy,
            }

        owner_thread_id = _current_owner(config)
        relay = _active_relay_contract(contract)
        automation_id = str(relay["id"])
        target = _automation_target(config, automation_id)
        if target != owner_thread_id:
            raise ValueError("active relay target differs from browser owner")
        current = now or utc_now()
        transaction = {
            "rotation_token": str(uuid.uuid4()),
            "phase": "prepared",
            "old_thread_id": owner_thread_id,
            "new_thread_id": None,
            "started_at": isoformat(current),
            "updated_at": isoformat(current),
            "handoff_count": handoff_count,
            "last_error": None,
        }
        state["transaction"] = transaction
        state["updated_at"] = isoformat(current)
        autopilot_dispatch.atomic_write_json(path, state)
        return _result(config, contract, transaction)


def pending(
    config_path: Path,
    contract: dict[str, Any],
) -> dict[str, Any] | None:
    """Return an open transaction without creating a new one."""

    config_path = config_path.expanduser().resolve()
    config = read_json(config_path)
    path = rotation_state_path(config_path, config)
    with autopilot_dispatch.locked_state(path):
        transaction = load_state(path).get("transaction")
        if not isinstance(transaction, dict):
            return None
        if str(transaction.get("phase")) not in OPEN_PHASES:
            raise ValueError("rotation transaction has an invalid phase")
        return _result(config, contract, transaction)


def _require_transaction(
    state: dict[str, Any],
    rotation_token: str,
) -> dict[str, Any]:
    transaction = state.get("transaction")
    if not isinstance(transaction, dict):
        raise ValueError("no active owner rotation")
    if transaction.get("rotation_token") != rotation_token:
        raise ValueError("rotation token mismatch")
    return transaction


def record_created(
    config_path: Path,
    *,
    rotation_token: str,
    thread_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    config_path = config_path.expanduser().resolve()
    config = read_json(config_path)
    path = rotation_state_path(config_path, config)
    current = now or utc_now()
    new_thread_id = thread_id.strip()
    if not new_thread_id:
        raise ValueError("new owner thread id is required")
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        transaction = _require_transaction(state, rotation_token)
        old_thread_id = str(transaction["old_thread_id"])
        if new_thread_id == old_thread_id:
            raise ValueError("new owner must differ from old owner")
        existing = transaction.get("new_thread_id")
        if existing not in (None, new_thread_id):
            raise ValueError("rotation already references another new owner")
        transaction["new_thread_id"] = new_thread_id
        transaction["phase"] = "thread_created"
        transaction["updated_at"] = isoformat(current)
        state["updated_at"] = isoformat(current)
        autopilot_dispatch.atomic_write_json(path, state)
        return {
            "status": "owner_thread_recorded",
            "rotation_token": rotation_token,
            "old_thread_id": old_thread_id,
            "new_thread_id": new_thread_id,
        }


def _validate_relay(
    root: Path,
    config: dict[str, Any],
    contract: dict[str, Any],
    new_thread_id: str,
) -> dict[str, Any]:
    expected = _active_relay_contract(contract)
    path = automation_path(config, str(expected["id"]))
    actual = automation_target_health.parse_automation(path)
    for key, value in expected.items():
        if key in {"prompt_source", "target_thread_role"}:
            continue
        if actual.get(key) != value:
            raise ValueError(f"active relay field mismatch: {key}")
    if actual.get("target_thread_id") != new_thread_id:
        raise ValueError("active relay does not target the new owner")
    prompt_source = str(expected.get("prompt_source", "")).strip()
    if prompt_source:
        expected_prompt = (root / prompt_source).read_text(encoding="utf-8").rstrip("\n")
        if actual.get("prompt") != expected_prompt:
            raise ValueError("active relay prompt differs from tracked source")
    return actual


def _reset_health(
    config_path: Path,
    config: dict[str, Any],
    *,
    old_thread_id: str,
    new_thread_id: str,
    now: datetime,
) -> None:
    path = health_path(config_path, config)
    with autopilot_dispatch.locked_state(path):
        health = read_json(path)
        health.update(
            {
                "version": 2,
                "status": "owner_rotated",
                "updated_at": isoformat(now),
                "event_ids": [],
                "handoff_count": 0,
                "completed_runs": 0,
                "rotation_after_runs": int(
                    config.get("autopilot_owner_rotation_after_runs", 20)
                ),
                "rotation_recommended": False,
                "previous_owner_thread_id": old_thread_id,
                "owner_thread_id": new_thread_id,
            }
        )
        health.pop("claim_token", None)
        health.pop("error", None)
        autopilot_dispatch.atomic_write_json(path, health)


def commit(
    config_path: Path,
    contract_path: Path,
    *,
    rotation_token: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    config_path = config_path.expanduser().resolve()
    contract_path = contract_path.expanduser().resolve()
    contract = read_json(contract_path)
    root = Path(str(contract["canonical_root"])).expanduser().resolve()
    current = now or utc_now()
    config = read_json(config_path)
    path = rotation_state_path(config_path, config)
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        transaction = _require_transaction(state, rotation_token)
        old_thread_id = str(transaction["old_thread_id"])
        new_thread_id = str(transaction.get("new_thread_id") or "")
        if not new_thread_id:
            raise ValueError("new owner thread was not recorded")
        _validate_new_owner(config, contract, new_thread_id)
        _validate_relay(root, config, contract, new_thread_id)
        configured_owner = _current_owner(config)
        if configured_owner not in {old_thread_id, new_thread_id}:
            raise ValueError("config points to an unrelated owner")
        if configured_owner != new_thread_id:
            config["browser_owner_thread_id"] = new_thread_id
            autopilot_dispatch.atomic_write_json(config_path, config)
        _reset_health(
            config_path,
            config,
            old_thread_id=old_thread_id,
            new_thread_id=new_thread_id,
            now=current,
        )
        transaction["phase"] = "committed"
        transaction["committed_at"] = isoformat(current)
        transaction["updated_at"] = isoformat(current)
        state["updated_at"] = isoformat(current)
        autopilot_dispatch.atomic_write_json(path, state)
        return {
            "status": "owner_rotation_committed",
            "rotation_token": rotation_token,
            "old_thread_id": old_thread_id,
            "new_thread_id": new_thread_id,
        }


def finish(
    config_path: Path,
    contract_path: Path,
    *,
    rotation_token: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    config_path = config_path.expanduser().resolve()
    contract = read_json(contract_path.expanduser().resolve())
    root = Path(str(contract["canonical_root"])).expanduser().resolve()
    current = now or utc_now()
    config = read_json(config_path)
    path = rotation_state_path(config_path, config)
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        transaction, old_thread_id, new_thread_id = _committed_rotation(
            state,
            rotation_token,
            config=config,
            contract=contract,
            root=root,
        )
        if not _old_owner_is_archived(config, old_thread_id):
            raise ValueError("old owner is not archived yet")
        state["generation"] = int(state.get("generation", 0)) + 1
        state["last_rotation"] = {
            "old_thread_id": old_thread_id,
            "new_thread_id": new_thread_id,
            "started_at": transaction.get("started_at"),
            "completed_at": isoformat(current),
            "handoff_count": transaction.get("handoff_count"),
        }
        state["transaction"] = None
        state["last_failure"] = None
        state["updated_at"] = isoformat(current)
        autopilot_dispatch.atomic_write_json(path, state)
        return {
            "status": "owner_rotation_completed",
            "old_thread_id": old_thread_id,
            "new_thread_id": new_thread_id,
            "generation": state["generation"],
        }


def _committed_rotation(
    state: dict[str, Any],
    rotation_token: str,
    *,
    config: dict[str, Any],
    contract: dict[str, Any],
    root: Path,
) -> tuple[dict[str, Any], str, str]:
    transaction = _require_transaction(state, rotation_token)
    if transaction.get("phase") != "committed":
        raise ValueError("rotation must be committed before archive")
    old_thread_id = str(transaction["old_thread_id"])
    new_thread_id = str(transaction["new_thread_id"])
    if _current_owner(config) != new_thread_id:
        raise ValueError("config does not point to the new owner")
    _validate_new_owner(config, contract, new_thread_id)
    _validate_relay(root, config, contract, new_thread_id)
    return transaction, old_thread_id, new_thread_id


def archive_preflight(
    config_path: Path,
    contract_path: Path,
    *,
    rotation_token: str,
) -> dict[str, Any]:
    """Prove the replacement is canonical before archiving the old owner."""

    config_path = config_path.expanduser().resolve()
    contract = read_json(contract_path.expanduser().resolve())
    root = Path(str(contract["canonical_root"])).expanduser().resolve()
    config = read_json(config_path)
    path = rotation_state_path(config_path, config)
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        _, old_thread_id, new_thread_id = _committed_rotation(
            state,
            rotation_token,
            config=config,
            contract=contract,
            root=root,
        )
    return {
        "status": "owner_rotation_archive_ready",
        "rotation_token": rotation_token,
        "old_thread_id": old_thread_id,
        "new_thread_id": new_thread_id,
    }


def fail(
    config_path: Path,
    *,
    rotation_token: str,
    error: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not error.strip():
        raise ValueError("rotation failure reason is required")
    config_path = config_path.expanduser().resolve()
    config = read_json(config_path)
    path = rotation_state_path(config_path, config)
    current = now or utc_now()
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        transaction = _require_transaction(state, rotation_token)
        failure = {
            "rotation_token": rotation_token,
            "phase": transaction.get("phase"),
            "error": error.strip(),
            "failed_at": isoformat(current),
        }
        state["last_failure"] = failure
        transaction["last_error"] = error.strip()
        transaction["updated_at"] = isoformat(current)
        state["updated_at"] = isoformat(current)
        autopilot_dispatch.atomic_write_json(path, state)
        return {"status": "owner_rotation_failed", **failure}


def status(config_path: Path, contract_path: Path) -> dict[str, Any]:
    config_path = config_path.expanduser().resolve()
    contract = read_json(contract_path.expanduser().resolve())
    config = read_json(config_path)
    path = rotation_state_path(config_path, config)
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        transaction = state.get("transaction")
        if isinstance(transaction, dict):
            return _result(config, contract, transaction)
        due, handoff_count = _rotation_due(config_path, config)
        return {
            "status": "owner_rotation_due" if due else "owner_rotation_not_due",
            "dispatch": False,
            "route": "rotation",
            "handoff_count": handoff_count,
            "generation": int(state.get("generation", 0)),
            "last_rotation": state.get("last_rotation"),
            "last_failure": state.get("last_failure"),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    created = commands.add_parser("created")
    created.add_argument("--rotation-token", required=True)
    created.add_argument("--thread-id", required=True)
    committed = commands.add_parser("commit")
    committed.add_argument("--rotation-token", required=True)
    preflight = commands.add_parser("archive-preflight")
    preflight.add_argument("--rotation-token", required=True)
    finished = commands.add_parser("finished")
    finished.add_argument("--rotation-token", required=True)
    failed = commands.add_parser("failed")
    failed.add_argument("--rotation-token", required=True)
    failed.add_argument("--error", required=True)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    if arguments.command == "status":
        result = status(arguments.config, arguments.contract)
    elif arguments.command == "created":
        result = record_created(
            arguments.config,
            rotation_token=arguments.rotation_token,
            thread_id=arguments.thread_id,
        )
    elif arguments.command == "commit":
        result = commit(
            arguments.config,
            arguments.contract,
            rotation_token=arguments.rotation_token,
        )
    elif arguments.command == "archive-preflight":
        result = archive_preflight(
            arguments.config,
            arguments.contract,
            rotation_token=arguments.rotation_token,
        )
    elif arguments.command == "finished":
        result = finish(
            arguments.config,
            arguments.contract,
            rotation_token=arguments.rotation_token,
        )
    elif arguments.command == "failed":
        result = fail(
            arguments.config,
            rotation_token=arguments.rotation_token,
            error=arguments.error,
        )
    else:
        raise AssertionError(arguments.command)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
