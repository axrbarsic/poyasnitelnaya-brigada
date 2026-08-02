#!/usr/bin/env python3
"""Incident detection and repair policy for the autopilot supervisor."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from scripts import automation_target_health, system_doctor
except ModuleNotFoundError:
    import automation_target_health  # type: ignore[no-redef]
    import system_doctor  # type: ignore[no-redef]


POLL_LABEL = "com.axrbarsic.xmention.poll"
EXTERNAL_ACTION_STATUS = "external_action_required"
OPEN_STATUSES = {
    "repair_observing",
    "escalation_pending",
    "handoff_pending",
    "claimed",
    "work_in_progress",
    "failed",
    EXTERNAL_ACTION_STATUS,
}
REPAIRABLE_POLL_STATUSES = {
    "stale",
    "failing",
    "never_started",
    "never_succeeded",
}


@dataclass(frozen=True)
class Dependencies:
    read_config: Callable[[Path], dict[str, Any]]
    state_path: Callable[[Path], Path]
    load_state: Callable[[Path], dict[str, Any]]
    save_state: Callable[[Path, dict[str, Any]], None]
    locked_state: Callable[..., Any]
    isoformat: Callable[[datetime | None], str]
    utc_now: Callable[[], datetime]
    parse_time: Callable[[Any], datetime | None]
    collect_checks: Callable[[Path, Path], list[system_doctor.Check]]
    unarchive_targets: Callable[..., dict[str, Any]]


def check_payload(check: system_doctor.Check) -> dict[str, Any]:
    return {
        "identifier": check.identifier,
        "status": check.status,
        "summary": check.summary,
        "repair": check.repair,
        "details": check.details,
    }


def incident_can_auto_resolve(incident: dict[str, Any]) -> bool:
    status = str(incident.get("status", ""))
    if incident.get("canary"):
        return False
    if status == "failed":
        return True
    return status in {
        "repair_observing",
        "escalation_pending",
        "handoff_pending",
        EXTERNAL_ACTION_STATUS,
    } and not incident.get("owner")


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
    isoformat: Callable[[datetime | None], str],
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


def poll_failure_is_billing_blocked(
    failures: list[system_doctor.Check],
) -> bool:
    allowed = {"runtime.poll_health", f"launchagent.{POLL_LABEL}.loaded"}
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


def archived_automation_target_is_repairable(
    failures: list[system_doctor.Check],
) -> bool:
    if [check.identifier for check in failures] != [
        "automation.active_heartbeat_targets"
    ]:
        return False
    targets = (failures[0].details or {}).get("targets")
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
    raw_targets = (failure.details or {}).get("targets")
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


def repair_cooldown(config: dict[str, Any]) -> int:
    value = int(config.get("autopilot_supervisor_repair_cooldown_seconds", 90))
    if value < 1:
        raise ValueError("supervisor repair cooldown must be positive")
    return value


def escalation_retry(config: dict[str, Any]) -> int:
    value = int(config.get("autopilot_supervisor_escalation_retry_seconds", 1800))
    if value < 60:
        raise ValueError(
            "supervisor escalation retry must be at least 60 seconds"
        )
    return value


def recover_expired_coordination(
    incident: dict[str, Any],
    *,
    now: datetime,
    parse_time: Callable[[Any], datetime | None],
    isoformat: Callable[[datetime | None], str],
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


def _healthy_result(
    path: Path,
    state: dict[str, Any],
    incident: dict[str, Any] | None,
    warnings: list[system_doctor.Check],
    *,
    now: datetime,
    dependencies: Dependencies,
) -> dict[str, Any]:
    state["last_healthy_at"] = dependencies.isoformat(now)
    if isinstance(incident, dict) and incident_can_auto_resolve(incident):
        incident["status"] = "resolved"
        incident["resolution"] = {
            "kind": "automatic_recovery",
            "completed_at": dependencies.isoformat(now),
            "report": "Повторная диагностика не обнаружила FAIL.",
        }
        state["last_incident"] = incident
        state["incident"] = None
    dependencies.save_state(path, state)
    return {
        "status": "healthy",
        "healthy": True,
        "model_wake_required": False,
        "failures": [],
        "warnings": [check.identifier for check in warnings],
    }


def _refresh_incident(
    state: dict[str, Any],
    incident: dict[str, Any] | None,
    failures: list[system_doctor.Check],
    *,
    now: datetime,
    dependencies: Dependencies,
) -> dict[str, Any]:
    fingerprint = failure_fingerprint(failures)
    incident_is_open = (
        isinstance(incident, dict) and incident.get("status") in OPEN_STATUSES
    )
    if not incident_is_open:
        if isinstance(incident, dict):
            state["last_incident"] = incident
        created = new_incident(
            failures,
            now=now,
            isoformat=dependencies.isoformat,
        )
        state["incident"] = created
        return created
    incident["last_seen_at"] = dependencies.isoformat(now)
    incident["checks"] = [check_payload(check) for check in failures]
    current_fingerprint = incident.get(
        "active_fingerprint",
        incident.get("fingerprint"),
    )
    if current_fingerprint != fingerprint:
        incident["active_fingerprint"] = fingerprint
        incident.setdefault("failure_history", []).append(
            {
                "fingerprint": fingerprint,
                "observed_at": dependencies.isoformat(now),
                "failure_ids": [check.identifier for check in failures],
            }
        )
    return incident


def _incident_result(
    incident: dict[str, Any],
    failures: list[system_doctor.Check],
    warnings: list[system_doctor.Check],
    *,
    external_action_required: bool = False,
) -> dict[str, Any]:
    attempts = incident.setdefault("repair_attempts", [])
    return {
        "status": str(incident["status"]),
        "healthy": False,
        "model_wake_required": incident["status"] == "escalation_pending",
        "incident_id": incident["id"],
        "failures": [check.identifier for check in failures],
        "warnings": [check.identifier for check in warnings],
        "repair_attempts": len(attempts),
        **(
            {"external_action_required": external_action_required}
            if external_action_required
            else {}
        ),
    }


def _handle_billing_block(
    incident: dict[str, Any],
    failures: list[system_doctor.Check],
    *,
    now: datetime,
    billing_block_runner: Callable[[], dict[str, Any]],
    dependencies: Dependencies,
) -> bool:
    if not poll_failure_is_billing_blocked(failures):
        return False
    if incident.get("status") in {
        "handoff_pending",
        "claimed",
        "work_in_progress",
    }:
        return False
    attempts = incident.setdefault("repair_attempts", [])
    prior_suspension = next(
        (
            attempt
            for attempt in attempts
            if attempt.get("action") == "suspend_billing_blocked_poll"
        ),
        None,
    )
    if prior_suspension is None:
        suspension = {
            **billing_block_runner(),
            "attempted_at": dependencies.isoformat(now),
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
            "recorded_at": dependencies.isoformat(now),
        }
    else:
        incident["status"] = "escalation_pending"
    return True


def _apply_automatic_repair(
    config: dict[str, Any],
    incident: dict[str, Any],
    failures: list[system_doctor.Check],
    *,
    now: datetime,
    repair_runner: Callable[[], dict[str, Any]],
    dependencies: Dependencies,
) -> None:
    coordination_active = incident.get("status") in {
        "handoff_pending",
        "claimed",
        "work_in_progress",
        "failed",
    }
    poll_repairable = poll_failure_is_repairable(failures)
    automation_repairable = archived_automation_target_is_repairable(failures)
    if (poll_repairable or automation_repairable) and not coordination_active:
        attempts = incident.setdefault("repair_attempts", [])
        cooldown = repair_cooldown(config)
        last_attempt_at = (
            dependencies.parse_time(attempts[-1].get("attempted_at"))
            if attempts
            else None
        )
        if not attempts:
            result = (
                repair_runner()
                if poll_repairable
                else dependencies.unarchive_targets(config, failures[0])
            )
            attempts.append(
                {**result, "attempted_at": dependencies.isoformat(now)}
            )
            incident["status"] = (
                "repair_observing"
                if result.get("success") is True
                else "escalation_pending"
            )
        elif (
            incident.get("status") == "repair_observing"
            and last_attempt_at is not None
            and (now - last_attempt_at).total_seconds() >= cooldown
        ):
            incident["status"] = "escalation_pending"
        return
    if (
        not poll_repairable
        and not automation_repairable
        and incident.get("status")
        not in {
            "handoff_pending",
            "claimed",
            "work_in_progress",
            "failed",
        }
    ):
        incident["status"] = "escalation_pending"


def run_once(
    config_path: Path,
    contract_path: Path,
    *,
    now: datetime | None,
    checks: list[system_doctor.Check] | None,
    repair_runner: Callable[[], dict[str, Any]],
    billing_block_runner: Callable[[], dict[str, Any]],
    dependencies: Dependencies,
) -> dict[str, Any]:
    current = now or dependencies.utc_now()
    config_path = config_path.expanduser().resolve()
    config = dependencies.read_config(config_path)
    path = dependencies.state_path(config_path)
    observed = (
        checks
        if checks is not None
        else dependencies.collect_checks(config_path, contract_path)
    )
    failures = [check for check in observed if check.status == "fail"]
    warnings = [check for check in observed if check.status == "warn"]
    with dependencies.locked_state(path):
        state = dependencies.load_state(path)
        state["last_checked_at"] = dependencies.isoformat(current)
        raw_incident = state.get("incident")
        incident = raw_incident if isinstance(raw_incident, dict) else None
        if incident is not None:
            recover_expired_coordination(
                incident,
                now=current,
                parse_time=dependencies.parse_time,
                isoformat=dependencies.isoformat,
            )
        if not failures:
            return _healthy_result(
                path,
                state,
                incident,
                warnings,
                now=current,
                dependencies=dependencies,
            )
        incident = _refresh_incident(
            state,
            incident,
            failures,
            now=current,
            dependencies=dependencies,
        )
        if _handle_billing_block(
            incident,
            failures,
            now=current,
            billing_block_runner=billing_block_runner,
            dependencies=dependencies,
        ):
            dependencies.save_state(path, state)
            return _incident_result(
                incident,
                failures,
                warnings,
                external_action_required=(
                    incident["status"] == EXTERNAL_ACTION_STATUS
                ),
            )
        _apply_automatic_repair(
            config,
            incident,
            failures,
            now=current,
            repair_runner=repair_runner,
            dependencies=dependencies,
        )
        dependencies.save_state(path, state)
        return _incident_result(incident, failures, warnings)
