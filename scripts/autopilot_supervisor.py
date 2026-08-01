#!/usr/bin/env python3
"""Token-free supervisor and durable repair bridge for the X autopilot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from scripts import (
        automation_target_health,
        autopilot_bridge,
        autopilot_dispatch,
        outbound_cycle,
        system_doctor,
    )
except ModuleNotFoundError:
    import automation_target_health  # type: ignore[no-redef]
    import autopilot_bridge  # type: ignore[no-redef]
    import autopilot_dispatch  # type: ignore[no-redef]
    import outbound_cycle  # type: ignore[no-redef]
    import system_doctor  # type: ignore[no-redef]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config.json"
DEFAULT_CONTRACT = PROJECT_ROOT / "recovery" / "system-contract.json"
STATE_VERSION = 1
POLL_LABEL = "com.axrbarsic.xmention.poll"
OPEN_STATUSES = {
    "repair_observing",
    "escalation_pending",
    "handoff_pending",
    "claimed",
    "work_in_progress",
    "failed",
    "external_action_required",
}
TERMINAL_STATUSES = {"resolved"}
EXTERNAL_ACTION_STATUS = "external_action_required"
X_QUEUE_RECOVERY_FAILURES = {"runtime.queue_latency"}
REPAIRABLE_POLL_STATUSES = {
    "stale",
    "failing",
    "never_started",
    "never_succeeded",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def read_config(config_path: Path) -> dict[str, Any]:
    return autopilot_dispatch.read_json(config_path.expanduser().resolve())


def resolve_path(config_path: Path, value: str) -> Path:
    return autopilot_dispatch.resolve_path(config_path, value)


def state_path(config_path: Path) -> Path:
    config = read_config(config_path)
    return resolve_path(
        config_path,
        str(
            config.get(
                "autopilot_supervisor_state_file",
                "var/autopilot-supervisor.json",
            )
        ),
    )


def default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "last_checked_at": None,
        "last_healthy_at": None,
        "incident": None,
        "last_incident": None,
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return default_state()
    payload = autopilot_dispatch.read_json(path)
    if payload.get("version") != STATE_VERSION:
        raise ValueError("unsupported autopilot supervisor state version")
    incident = payload.get("incident")
    if incident is not None and not isinstance(incident, dict):
        raise ValueError("supervisor incident must be an object or null")
    payload.setdefault("last_checked_at", None)
    payload.setdefault("last_healthy_at", None)
    payload.setdefault("last_incident", None)
    return payload


def save_state(path: Path, state: dict[str, Any]) -> None:
    autopilot_dispatch.atomic_write_json(path, state)


def check_payload(check: system_doctor.Check) -> dict[str, Any]:
    return {
        "identifier": check.identifier,
        "status": check.status,
        "summary": check.summary,
        "repair": check.repair,
        "details": check.details,
    }


def collect_checks(
    config_path: Path,
    contract_path: Path,
) -> list[system_doctor.Check]:
    root = config_path.expanduser().resolve().parent
    contract = system_doctor.read_json(contract_path.expanduser().resolve())
    checks = system_doctor.check_contract(
        root,
        Path.home().resolve(),
        contract,
        config_path.expanduser().resolve(),
    )
    archived_targets = (
        automation_target_health.archived_active_heartbeat_targets(
            Path.home().resolve()
        )
    )
    checks.append(
        system_doctor.Check(
            "automation.active_heartbeat_targets",
            "fail" if archived_targets else "pass",
            (
                "Активная heartbeat automation указывает в архив."
                if archived_targets
                else "Все активные heartbeat automation имеют живые цели."
            ),
            "Сними архивный флаг с целевой задачи или приостанови "
            "automation через официальный automation_update.",
            {"targets": archived_targets} if archived_targets else None,
        )
    )
    return checks


def failure_fingerprint(checks: Iterable[system_doctor.Check]) -> str:
    identifiers = sorted(
        check.identifier for check in checks if check.status == "fail"
    )
    encoded = json.dumps(
        identifiers,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def new_incident(
    failures: list[system_doctor.Check],
    *,
    now: datetime,
    canary: bool = False,
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "fingerprint": failure_fingerprint(failures),
        "status": "escalation_pending",
        "canary": canary,
        "detected_at": isoformat(now),
        "last_seen_at": isoformat(now),
        "checks": [check_payload(check) for check in failures],
        "repair_attempts": [],
        "handoff": None,
        "owner": None,
        "wake_count": 0,
        "last_wake_at": None,
        "resolution": None,
    }


def poll_failure_is_repairable(
    failures: list[system_doctor.Check],
) -> bool:
    if poll_failure_is_billing_blocked(failures):
        return False
    if [check.identifier for check in failures] != ["runtime.poll_health"]:
        return False
    details = failures[0].details or {}
    age = details.get("age_seconds")
    max_age = details.get("max_age_seconds")
    stale_by_age = (
        isinstance(age, (int, float))
        and isinstance(max_age, (int, float))
        and age > max_age
    )
    return (
        str(details.get("health_status", "")) in REPAIRABLE_POLL_STATUSES
        or stale_by_age
    )


def poll_failure_is_billing_blocked(
    failures: list[system_doctor.Check],
) -> bool:
    allowed = {
        "runtime.poll_health",
        f"launchagent.{POLL_LABEL}.loaded",
    }
    if not failures or any(
        check.identifier not in allowed for check in failures
    ):
        return False
    poll = next(
        (
            check
            for check in failures
            if check.identifier == "runtime.poll_health"
        ),
        None,
    )
    if poll is None:
        return False
    details = poll.details or {}
    return (
        str(details.get("health_status", "")) == "billing_blocked"
        or str(details.get("last_error_message", "")).startswith(
            "X API HTTP 402"
        )
    )


def archived_automation_target_is_repairable(
    failures: list[system_doctor.Check],
) -> bool:
    if [
        check.identifier for check in failures
    ] != ["automation.active_heartbeat_targets"]:
        return False
    details = failures[0].details or {}
    targets = details.get("targets")
    return bool(
        isinstance(targets, list)
        and targets
        and all(
            isinstance(target, dict)
            and str(target.get("thread_id", "")).strip()
            for target in targets
        )
    )


def unarchive_automation_targets(
    config: dict[str, Any],
    failure: system_doctor.Check,
) -> dict[str, Any]:
    details = failure.details or {}
    raw_targets = details.get("targets")
    targets = raw_targets if isinstance(raw_targets, list) else []
    executable = Path(
        str(
            config.get(
                "codex_cli_path",
                "/Applications/ChatGPT.app/Contents/Resources/codex",
            )
        )
    ).expanduser()
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for target in targets:
        if not isinstance(target, dict):
            continue
        thread_id = str(target.get("thread_id", "")).strip()
        if not thread_id or thread_id in seen:
            continue
        seen.add(thread_id)
        completed = subprocess.run(
            [str(executable), "unarchive", thread_id],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        results.append(
            {
                "thread_id": thread_id,
                "returncode": completed.returncode,
                "stdout": completed.stdout.strip()[-500:],
                "stderr": completed.stderr.strip()[-500:],
            }
        )
    return {
        "action": "unarchive_active_heartbeat_targets",
        "success": bool(results)
        and all(result["returncode"] == 0 for result in results),
        "results": results,
    }


def kickstart_poll() -> dict[str, Any]:
    target = f"gui/{os.getuid()}/{POLL_LABEL}"
    completed = subprocess.run(
        ["/bin/launchctl", "kickstart", "-k", target],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return {
        "action": "kickstart_poll_launchagent",
        "target": target,
        "success": completed.returncode == 0,
        "returncode": completed.returncode,
        "stderr": completed.stderr.strip()[-500:],
    }


def suspend_billing_blocked_poll() -> dict[str, Any]:
    target = f"gui/{os.getuid()}/{POLL_LABEL}"
    probe = subprocess.run(
        ["/bin/launchctl", "print", target],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode != 0:
        return {
            "action": "suspend_billing_blocked_poll",
            "target": target,
            "success": True,
            "returncode": probe.returncode,
            "already_unloaded": True,
        }
    completed = subprocess.run(
        ["/bin/launchctl", "bootout", target],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return {
        "action": "suspend_billing_blocked_poll",
        "target": target,
        "success": completed.returncode == 0,
        "returncode": completed.returncode,
        "already_unloaded": False,
        "stderr": completed.stderr.strip()[-500:],
    }


def _repair_cooldown(config: dict[str, Any]) -> int:
    value = int(
        config.get("autopilot_supervisor_repair_cooldown_seconds", 90)
    )
    if value < 1:
        raise ValueError("supervisor repair cooldown must be positive")
    return value


def _escalation_retry(config: dict[str, Any]) -> int:
    value = int(
        config.get("autopilot_supervisor_escalation_retry_seconds", 1800)
    )
    if value < 60:
        raise ValueError(
            "supervisor escalation retry must be at least 60 seconds"
        )
    return value


def recover_expired_coordination(
    incident: dict[str, Any],
    *,
    now: datetime,
) -> bool:
    status = str(incident.get("status", ""))
    if status == "handoff_pending":
        handoff = incident.get("handoff")
        expires_at = (
            parse_time(handoff.get("expires_at"))
            if isinstance(handoff, dict)
            else None
        )
        if expires_at is None or expires_at <= now:
            incident["last_expired_handoff"] = {
                "reserved_at": (
                    handoff.get("reserved_at")
                    if isinstance(handoff, dict)
                    else None
                ),
                "expires_at": (
                    handoff.get("expires_at")
                    if isinstance(handoff, dict)
                    else None
                ),
                "recovered_at": isoformat(now),
            }
            incident["status"] = "escalation_pending"
            incident["handoff"] = None
            return True
    if status in {"claimed", "work_in_progress"}:
        owner = incident.get("owner")
        expires_at = (
            parse_time(owner.get("lease_expires_at"))
            if isinstance(owner, dict)
            else None
        )
        if expires_at is None or expires_at <= now:
            incident["last_expired_owner"] = {
                "claimed_at": (
                    owner.get("claimed_at")
                    if isinstance(owner, dict)
                    else None
                ),
                "started_at": (
                    owner.get("started_at")
                    if isinstance(owner, dict)
                    else None
                ),
                "lease_expires_at": (
                    owner.get("lease_expires_at")
                    if isinstance(owner, dict)
                    else None
                ),
                "recovered_at": isoformat(now),
            }
            incident["status"] = "escalation_pending"
            incident["handoff"] = None
            incident["owner"] = None
            return True
    return False


def run_once(
    config_path: Path,
    contract_path: Path = DEFAULT_CONTRACT,
    *,
    now: datetime | None = None,
    checks: list[system_doctor.Check] | None = None,
    repair_runner: Callable[[], dict[str, Any]] = kickstart_poll,
    billing_block_runner: Callable[
        [], dict[str, Any]
    ] = suspend_billing_blocked_poll,
) -> dict[str, Any]:
    current = now or utc_now()
    config_path = config_path.expanduser().resolve()
    config = read_config(config_path)
    path = state_path(config_path)
    observed = (
        checks
        if checks is not None
        else collect_checks(config_path, contract_path)
    )
    failures = [check for check in observed if check.status == "fail"]
    warnings = [check for check in observed if check.status == "warn"]

    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        state["last_checked_at"] = isoformat(current)
        incident = state.get("incident")
        if isinstance(incident, dict):
            recover_expired_coordination(incident, now=current)
        if not failures:
            state["last_healthy_at"] = isoformat(current)
            if (
                isinstance(incident, dict)
                and incident.get("status") in {
                    "repair_observing",
                    "escalation_pending",
                    "handoff_pending",
                    "failed",
                    EXTERNAL_ACTION_STATUS,
                }
                and not incident.get("canary")
                and not incident.get("owner")
            ):
                incident["status"] = "resolved"
                incident["resolution"] = {
                    "kind": "automatic_recovery",
                    "completed_at": isoformat(current),
                    "report": "Повторная диагностика не обнаружила FAIL.",
                }
                state["last_incident"] = incident
                state["incident"] = None
            save_state(path, state)
            return {
                "status": "healthy",
                "healthy": True,
                "model_wake_required": False,
                "failures": [],
                "warnings": [check.identifier for check in warnings],
            }

        fingerprint = failure_fingerprint(failures)
        incident_is_open = (
            isinstance(incident, dict)
            and incident.get("status") in OPEN_STATUSES
        )
        if not incident_is_open:
            if isinstance(incident, dict):
                state["last_incident"] = incident
            incident = new_incident(failures, now=current)
            state["incident"] = incident
        else:
            incident["last_seen_at"] = isoformat(current)
            incident["checks"] = [
                check_payload(check) for check in failures
            ]
            current_fingerprint = incident.get(
                "active_fingerprint",
                incident.get("fingerprint"),
            )
            if current_fingerprint != fingerprint:
                incident["active_fingerprint"] = fingerprint
                incident.setdefault("failure_history", []).append(
                    {
                        "fingerprint": fingerprint,
                        "observed_at": isoformat(current),
                        "failure_ids": [
                            check.identifier for check in failures
                        ],
                    }
                )

        attempts = incident.setdefault("repair_attempts", [])
        billing_blocked = poll_failure_is_billing_blocked(failures)
        coordination_locked = incident.get("status") in {
            "handoff_pending",
            "claimed",
            "work_in_progress",
        }
        if billing_blocked and not coordination_locked:
            prior_suspension = next(
                (
                    attempt
                    for attempt in attempts
                    if attempt.get("action")
                    == "suspend_billing_blocked_poll"
                ),
                None,
            )
            if prior_suspension is None:
                suspension = {
                    **billing_block_runner(),
                    "attempted_at": isoformat(current),
                }
                attempts.append(suspension)
            else:
                suspension = prior_suspension
            if suspension.get("success") is True:
                previous_owner = incident.pop("owner", None)
                if isinstance(previous_owner, dict):
                    incident["last_owner"] = previous_owner
                incident["status"] = EXTERNAL_ACTION_STATUS
                incident["external_action"] = {
                    "kind": "x_api_credits_depleted",
                    "required_action": (
                        "Purchase X API credits, then bootstrap and verify "
                        "the poll LaunchAgent."
                    ),
                    "recorded_at": isoformat(current),
                }
            else:
                incident["status"] = "escalation_pending"
            save_state(path, state)
            return {
                "status": str(incident["status"]),
                "healthy": False,
                "model_wake_required": (
                    incident["status"] == "escalation_pending"
                ),
                "incident_id": incident["id"],
                "failures": [check.identifier for check in failures],
                "warnings": [check.identifier for check in warnings],
                "repair_attempts": len(attempts),
                "external_action_required": (
                    incident["status"] == EXTERNAL_ACTION_STATUS
                ),
            }

        coordination_active = incident.get("status") in {
            "handoff_pending",
            "claimed",
            "work_in_progress",
            "failed",
        }
        poll_repairable = poll_failure_is_repairable(failures)
        automation_repairable = archived_automation_target_is_repairable(
            failures
        )
        if (
            (poll_repairable or automation_repairable)
            and not coordination_active
        ):
            cooldown = _repair_cooldown(config)
            last_attempt_at = (
                parse_time(attempts[-1].get("attempted_at"))
                if attempts
                else None
            )
            if not attempts:
                result = (
                    repair_runner()
                    if poll_repairable
                    else unarchive_automation_targets(config, failures[0])
                )
                attempt = {
                    **result,
                    "attempted_at": isoformat(current),
                }
                attempts.append(attempt)
                incident["status"] = (
                    "repair_observing"
                    if result.get("success") is True
                    else "escalation_pending"
                )
            elif (
                incident.get("status") == "repair_observing"
                and last_attempt_at is not None
                and (current - last_attempt_at).total_seconds() >= cooldown
            ):
                incident["status"] = "escalation_pending"
        elif (
            not poll_repairable
            and not automation_repairable
            and incident.get("status") not in {
            "handoff_pending",
            "claimed",
            "work_in_progress",
            "failed",
            }
        ):
            incident["status"] = "escalation_pending"

        save_state(path, state)
        return {
            "status": str(incident["status"]),
            "healthy": False,
            "model_wake_required": incident["status"] == "escalation_pending",
            "incident_id": incident["id"],
            "failures": [check.identifier for check in failures],
            "warnings": [check.identifier for check in warnings],
            "repair_attempts": len(attempts),
        }


def gate(
    config_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or utc_now()
    config_path = config_path.expanduser().resolve()
    config = read_config(config_path)
    path = state_path(config_path)
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        incident = state.get("incident")
        if not isinstance(incident, dict):
            return {
                "status": "idle",
                "dispatch": False,
                "repair_pending": False,
            }
        coordination_recovered = recover_expired_coordination(
            incident,
            now=current,
        )
        status = str(incident.get("status", ""))
        if status == "failed":
            owner = incident.get("owner") or {}
            failed_at = parse_time(owner.get("failed_at"))
            retry = _escalation_retry(config)
            if (
                failed_at is not None
                and (current - failed_at).total_seconds() >= retry
            ):
                incident["status"] = "escalation_pending"
                incident["owner"] = None
                status = "escalation_pending"
                save_state(path, state)
        elif coordination_recovered:
            save_state(path, state)
        external_action_required = status == EXTERNAL_ACTION_STATUS
        failure_ids = {
            str(check.get("identifier"))
            for check in incident.get("checks", [])
            if isinstance(check, dict) and check.get("status") == "fail"
        }
        x_queue_recovery = failure_ids == X_QUEUE_RECOVERY_FAILURES
        return {
            "status": status,
            "dispatch": (
                status == "escalation_pending" and not x_queue_recovery
            ),
            "repair_pending": (
                status in OPEN_STATUSES
                and not external_action_required
                and not x_queue_recovery
            ),
            "external_action_required": external_action_required,
            "incident_id": incident.get("id"),
            "failures": [
                check.get("identifier")
                for check in incident.get("checks", [])
                if isinstance(check, dict)
            ],
        }


def reserve_handoff(
    config_path: Path,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    if lease_seconds <= 0:
        raise ValueError("handoff lease must be positive")
    current = now or utc_now()
    config_path = config_path.expanduser().resolve()
    path = state_path(config_path)
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        incident = state.get("incident")
        if not isinstance(incident, dict):
            return {
                "status": "idle",
                "dispatch": False,
                "repair_pending": False,
            }
        status = str(incident.get("status", ""))
        handoff = incident.get("handoff")
        expires_at = (
            parse_time(handoff.get("expires_at"))
            if isinstance(handoff, dict)
            else None
        )
        if (
            status == "handoff_pending"
            and expires_at is not None
            and expires_at > current
        ):
            return {
                "status": "repair_handoff_reserved",
                "dispatch": False,
                "repair_pending": True,
                "incident_id": incident["id"],
            }
        if status == "handoff_pending":
            incident["status"] = "escalation_pending"
            incident["handoff"] = None
            status = "escalation_pending"
        if status != "escalation_pending":
            external_action_required = status == EXTERNAL_ACTION_STATUS
            return {
                "status": status,
                "dispatch": False,
                "repair_pending": (
                    status in OPEN_STATUSES and not external_action_required
                ),
                "external_action_required": external_action_required,
                "incident_id": incident["id"],
            }

        token = str(uuid.uuid4())
        incident["status"] = "handoff_pending"
        incident["handoff"] = {
            "reservation_token": token,
            "reserved_at": isoformat(current),
            "expires_at": isoformat(
                current + timedelta(seconds=lease_seconds)
            ),
        }
        save_state(path, state)
        return {
            "status": "repair_handoff_pending",
            "dispatch": True,
            "repair_pending": True,
            "incident_id": incident["id"],
            "reservation_token": token,
        }


def active_x_owner_snapshot(
    config_path: Path,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read the global X owner lease without opening or changing the queue."""

    if lease_seconds <= 0:
        raise ValueError("owner lease must be positive")
    _, state_file = autopilot_dispatch.load_paths(config_path)
    current = now or utc_now()
    with autopilot_dispatch.locked_state(state_file):
        state = autopilot_dispatch.load_state(state_file)
        owner = state.get("owner")
        if not isinstance(owner, dict):
            return {"owner_busy": False, "active_event_ids": []}
        claimed_at = autopilot_dispatch.parse_time(owner.get("claimed_at"))
        lease_expires_at = autopilot_dispatch.parse_time(
            owner.get("lease_expires_at")
        )
        if lease_expires_at is None and claimed_at is not None:
            lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
        owner_busy = (
            claimed_at is not None
            and lease_expires_at is not None
            and current < lease_expires_at
        )
        return {
            "owner_busy": owner_busy,
            "active_event_ids": [
                str(value) for value in owner.get("event_ids", [])
            ],
            "lease_expires_at": (
                autopilot_dispatch.isoformat(lease_expires_at)
                if lease_expires_at is not None
                else None
            ),
        }


def outbound_state_path(config_path: Path) -> Path:
    config = read_config(config_path)
    return resolve_path(
        config_path,
        str(
            config.get(
                "outbound_cycle_state_file",
                "var/outbound-cycle.json",
            )
        ),
    )


def outbound_interval_minutes(config_path: Path) -> int:
    config = read_config(config_path)
    value = int(config.get("outbound_cycle_interval_minutes", 10))
    if value <= 0:
        raise ValueError("outbound cycle interval must be positive")
    return value


def x_queue_is_exactly_idle(result: dict[str, Any]) -> bool:
    return (
        result.get("status") == "idle"
        and result.get("dispatch") is False
        and int(result.get("pending_count", 0)) == 0
        and not result.get("event_ids")
        and not result.get("owner_busy", False)
        and int(result.get("leased_count", 0)) == 0
    )


def relay_reserve_handoff(
    config_path: Path,
    contract_path: Path,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return one repair, inbound X, or idle-only outbound decision."""

    config = read_config(config_path)
    contract = system_doctor.read_json(contract_path.expanduser().resolve())
    owner = contract["threads"]["browser_owner"]
    owner_thread_id = str(owner["id"])
    if str(config.get("browser_owner_thread_id", "")) != owner_thread_id:
        raise ValueError(
            "browser owner differs between config and system contract"
        )
    owner_route = {
        "owner_thread_id": owner_thread_id,
        "owner_model": str(owner["model"]),
        "owner_thinking": str(owner["minimum_reasoning_effort"]),
    }

    repair_state = gate(config_path, now=now)
    if repair_state.get("repair_pending"):
        x_owner = active_x_owner_snapshot(
            config_path,
            lease_seconds=lease_seconds,
            now=now,
        )
        if x_owner["owner_busy"]:
            return {
                **repair_state,
                **owner_route,
                **x_owner,
                "status": "repair_waiting_for_x_owner",
                "dispatch": False,
                "route": "repair",
            }
        repair = reserve_handoff(
            config_path,
            lease_seconds=lease_seconds,
            now=now,
        )
        if repair.get("dispatch"):
            x_owner = active_x_owner_snapshot(
                config_path,
                lease_seconds=lease_seconds,
                now=now,
            )
            if x_owner["owner_busy"]:
                release_handoff(
                    config_path,
                    reservation_token=str(repair["reservation_token"]),
                    reason="x_owner_became_active",
                )
                return {
                    **repair_state,
                    **owner_route,
                    **x_owner,
                    "status": "repair_waiting_for_x_owner",
                    "dispatch": False,
                    "route": "repair",
                }
        return {**repair, **owner_route, "route": "repair"}
    outbound_path = outbound_state_path(config_path)
    x_result = autopilot_bridge.reserve_handoff(
        config_path,
        lease_seconds=lease_seconds,
    )
    if x_result.get("dispatch"):
        outbound_owner = outbound_cycle.active_owner_snapshot(
            outbound_path,
            now=now,
        )
        if outbound_owner.get("owner_busy"):
            reservation_token = str(
                x_result.get("reservation_token", "")
            )
            autopilot_bridge.release_handoff_reservation(
                config_path,
                reservation_token=reservation_token,
                reason="outbound_owner_must_pause_for_inbound",
            )
            return {
                **x_result,
                **owner_route,
                "status": "x_waiting_for_outbound_pause",
                "dispatch": False,
                "repair_pending": False,
                "route": "x",
                "outbound_owner": outbound_owner,
            }
        return {
            **x_result,
            **owner_route,
            "repair_pending": False,
            "route": "x",
        }
    if not x_queue_is_exactly_idle(x_result):
        return {
            **x_result,
            **owner_route,
            "repair_pending": False,
            "route": "x",
        }

    x_owner = active_x_owner_snapshot(
        config_path,
        lease_seconds=lease_seconds,
        now=now,
    )
    if x_owner.get("owner_busy"):
        return {
            **x_result,
            **owner_route,
            **x_owner,
            "status": "outbound_waiting_for_x_owner",
            "dispatch": False,
            "repair_pending": False,
            "route": "outbound",
        }

    outbound = outbound_cycle.claim_due(
        outbound_path,
        lease_seconds=lease_seconds,
        interval_minutes=outbound_interval_minutes(config_path),
        now=now,
    )
    if not outbound.get("dispatch"):
        return {
            **outbound,
            **owner_route,
            "repair_pending": False,
            "route": "outbound",
        }

    race_check = autopilot_bridge.reserve_handoff(
        config_path,
        lease_seconds=lease_seconds,
    )
    if not x_queue_is_exactly_idle(race_check):
        outbound_cycle.pause_slot(
            outbound_path,
            claim_token=str(outbound["claim_token"]),
            reason="inbound_arrived_during_outbound_reservation",
            now=now,
        )
        return {
            **race_check,
            **owner_route,
            "repair_pending": False,
            "route": "x",
            "outbound_preempted": True,
        }
    return {
        **outbound,
        **owner_route,
        "repair_pending": False,
        "route": "outbound",
    }


def release_handoff(
    config_path: Path,
    *,
    reservation_token: str,
    reason: str,
) -> dict[str, Any]:
    path = state_path(config_path.expanduser().resolve())
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        incident = state.get("incident")
        if not isinstance(incident, dict):
            raise ValueError("no active repair incident")
        handoff = incident.get("handoff")
        if (
            not isinstance(handoff, dict)
            or handoff.get("reservation_token") != reservation_token
        ):
            raise ValueError("repair reservation token mismatch")
        incident["status"] = "escalation_pending"
        incident["handoff"] = None
        incident["last_release_reason"] = reason
        save_state(path, state)
        return {
            "status": "repair_handoff_released",
            "incident_id": incident["id"],
        }


def claim(
    config_path: Path,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    if lease_seconds <= 0:
        raise ValueError("owner lease must be positive")
    current = now or utc_now()
    path = state_path(config_path.expanduser().resolve())
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        incident = state.get("incident")
        if not isinstance(incident, dict):
            return {"status": "idle", "dispatch": False}
        if incident.get("status") not in {
            "handoff_pending",
            "escalation_pending",
        }:
            return {
                "status": str(incident.get("status")),
                "dispatch": False,
                "incident_id": incident["id"],
            }
        claim_token = str(uuid.uuid4())
        incident["status"] = "claimed"
        incident["handoff"] = None
        incident["owner"] = {
            "claim_token": claim_token,
            "claimed_at": isoformat(current),
            "lease_expires_at": isoformat(
                current + timedelta(seconds=lease_seconds)
            ),
        }
        incident["wake_count"] = int(incident.get("wake_count", 0)) + 1
        incident["last_wake_at"] = isoformat(current)
        save_state(path, state)
        return {
            "status": "claimed",
            "dispatch": True,
            "incident_id": incident["id"],
            "claim_token": claim_token,
            "prompt": repair_prompt(incident),
        }


def repair_prompt(incident: dict[str, Any]) -> str:
    checks = "\n".join(
        (
            f"- {check.get('identifier')}: {check.get('summary')} "
            f"Рекомендация: {check.get('repair')}"
        )
        for check in incident.get("checks", [])
        if isinstance(check, dict)
    )
    attempts = "\n".join(
        (
            f"- {attempt.get('action')}: success={attempt.get('success')}, "
            f"returncode={attempt.get('returncode')}"
        )
        for attempt in incident.get("repair_attempts", [])
        if isinstance(attempt, dict)
    ) or "- автоматические попытки не выполнялись"
    return (
        "Исправь системный incident автопилота только в каноническом "
        "workspace. Не публикуй в X и не открывай Browser, если проверка "
        "прямо этого не требует.\n\n"
        f"Incident: {incident['id']}\n"
        "Проваленные проверки:\n"
        f"{checks}\n"
        "Уже выполненные безопасные попытки:\n"
        f"{attempts}\n\n"
        "Сначала выполни started с точным claim token. Найди первопричину, "
        "исправь минимально, повтори system_doctor и адресный canary. После "
        "нулевого FAIL выполни completed с точным token и кратким отчетом. "
        "Если безопасный ремонт невозможен, выполни failed с точной причиной."
    )


def _require_owner(
    incident: dict[str, Any],
    claim_token: str,
) -> dict[str, Any]:
    owner = incident.get("owner")
    if (
        not isinstance(owner, dict)
        or owner.get("claim_token") != claim_token
    ):
        raise ValueError("repair claim token mismatch")
    return owner


def started(
    config_path: Path,
    *,
    claim_token: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or utc_now()
    path = state_path(config_path.expanduser().resolve())
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        incident = state.get("incident")
        if not isinstance(incident, dict):
            raise ValueError("no active repair incident")
        owner = _require_owner(incident, claim_token)
        if incident.get("status") != "claimed":
            raise ValueError("repair incident is not claimed")
        incident["status"] = "work_in_progress"
        owner["started_at"] = isoformat(current)
        save_state(path, state)
        return {
            "status": "work_in_progress",
            "incident_id": incident["id"],
        }


def completed(
    config_path: Path,
    contract_path: Path,
    *,
    claim_token: str,
    report: str,
    now: datetime | None = None,
    checks: list[system_doctor.Check] | None = None,
) -> dict[str, Any]:
    if not report.strip():
        raise ValueError("repair completion report is required")
    current = now or utc_now()
    config_path = config_path.expanduser().resolve()
    path = state_path(config_path)
    observed = (
        checks
        if checks is not None
        else collect_checks(config_path, contract_path)
    )
    failures = [check for check in observed if check.status == "fail"]
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        incident = state.get("incident")
        if not isinstance(incident, dict):
            raise ValueError("no active repair incident")
        _require_owner(incident, claim_token)
        if incident.get("status") != "work_in_progress":
            raise ValueError("repair incident has not been started")
        if failures and not incident.get("canary"):
            raise ValueError(
                "system doctor still has FAIL: "
                + ", ".join(check.identifier for check in failures)
            )
        incident["status"] = "resolved"
        incident["resolution"] = {
            "kind": (
                "canary_completed"
                if incident.get("canary")
                else "sol_repair_completed"
            ),
            "completed_at": isoformat(current),
            "report": report.strip(),
            "remaining_warnings": [
                check.identifier
                for check in observed
                if check.status == "warn"
            ],
        }
        incident["owner"]["completed_at"] = isoformat(current)
        state["last_incident"] = incident
        state["incident"] = None
        state["last_healthy_at"] = isoformat(current)
        state["last_checked_at"] = isoformat(current)
        save_state(path, state)
        return {
            "status": "completed",
            "incident_id": incident["id"],
            "report": report.strip(),
        }


def failed(
    config_path: Path,
    *,
    claim_token: str,
    error: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not error.strip():
        raise ValueError("repair failure reason is required")
    current = now or utc_now()
    path = state_path(config_path.expanduser().resolve())
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        incident = state.get("incident")
        if not isinstance(incident, dict):
            raise ValueError("no active repair incident")
        owner = _require_owner(incident, claim_token)
        incident["status"] = "failed"
        owner["failed_at"] = isoformat(current)
        owner["error"] = error.strip()
        save_state(path, state)
        return {
            "status": "failed",
            "incident_id": incident["id"],
            "error": error.strip(),
        }


def create_canary(
    config_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or utc_now()
    path = state_path(config_path.expanduser().resolve())
    canary_check = system_doctor.Check(
        "canary.supervisor_handoff",
        "fail",
        "Контролируемая проверка Sol handoff ожидает закрытия.",
        "Claim, started и completed должны пройти в существующей Sol сессии.",
        {"canary": True},
    )
    with autopilot_dispatch.locked_state(path):
        state = load_state(path)
        incident = state.get("incident")
        if (
            isinstance(incident, dict)
            and incident.get("status") in OPEN_STATUSES
        ):
            raise ValueError("cannot start canary while an incident is open")
        created = new_incident([canary_check], now=current, canary=True)
        state["incident"] = created
        state["last_checked_at"] = isoformat(current)
        save_state(path, state)
        return {
            "status": "escalation_pending",
            "incident_id": created["id"],
            "canary": True,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--lease-seconds", type=int, default=1800)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run")
    commands.add_parser("gate")
    commands.add_parser("reserve-handoff")
    commands.add_parser("relay-reserve-handoff")
    release = commands.add_parser("release-handoff")
    release.add_argument("--reservation-token", required=True)
    release.add_argument("--reason", required=True)
    commands.add_parser("claim")
    begin = commands.add_parser("started")
    begin.add_argument("--claim-token", required=True)
    done = commands.add_parser("completed")
    done.add_argument("--claim-token", required=True)
    done.add_argument("--report", required=True)
    fail = commands.add_parser("failed")
    fail.add_argument("--claim-token", required=True)
    fail.add_argument("--error", required=True)
    commands.add_parser("canary")
    commands.add_parser("status")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = args.config.expanduser().resolve()
    contract = args.contract.expanduser().resolve()
    if args.command == "run":
        result = run_once(config, contract)
    elif args.command == "gate":
        result = gate(config)
    elif args.command == "reserve-handoff":
        result = reserve_handoff(config, lease_seconds=args.lease_seconds)
    elif args.command == "relay-reserve-handoff":
        result = relay_reserve_handoff(
            config,
            contract,
            lease_seconds=args.lease_seconds,
        )
    elif args.command == "release-handoff":
        result = release_handoff(
            config,
            reservation_token=args.reservation_token,
            reason=args.reason,
        )
    elif args.command == "claim":
        result = claim(config, lease_seconds=args.lease_seconds)
    elif args.command == "started":
        result = started(config, claim_token=args.claim_token)
    elif args.command == "completed":
        result = completed(
            config,
            contract,
            claim_token=args.claim_token,
            report=args.report,
        )
    elif args.command == "failed":
        result = failed(
            config,
            claim_token=args.claim_token,
            error=args.error,
        )
    elif args.command == "canary":
        result = create_canary(config)
    elif args.command == "status":
        result = gate(config)
    else:
        raise AssertionError(args.command)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.command == "run" and result.get("healthy") is False:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
