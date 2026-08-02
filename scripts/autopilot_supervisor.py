#!/usr/bin/env python3
"""Token-free supervisor and durable repair bridge for the X autopilot."""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from scripts import (
        automation_target_health,
        autopilot_bridge,
        autopilot_dispatch,
        autopilot_supervisor_incidents,
        autopilot_supervisor_routes,
        autopilot_state_model,
        outbound_cycle,
        system_doctor,
    )
except ModuleNotFoundError:
    import automation_target_health  # type: ignore[no-redef]
    import autopilot_bridge  # type: ignore[no-redef]
    import autopilot_dispatch  # type: ignore[no-redef]
    import autopilot_supervisor_incidents  # type: ignore[no-redef]
    import autopilot_supervisor_routes  # type: ignore[no-redef]
    import autopilot_state_model  # type: ignore[no-redef]
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


check_payload = autopilot_supervisor_incidents.check_payload
incident_can_auto_resolve = (
    autopilot_supervisor_incidents.incident_can_auto_resolve
)
failure_fingerprint = autopilot_supervisor_incidents.failure_fingerprint
poll_failure_is_repairable = (
    autopilot_supervisor_incidents.poll_failure_is_repairable
)
poll_failure_is_billing_blocked = (
    autopilot_supervisor_incidents.poll_failure_is_billing_blocked
)
archived_automation_target_is_repairable = (
    autopilot_supervisor_incidents.archived_automation_target_is_repairable
)
unarchive_automation_targets = (
    autopilot_supervisor_incidents.unarchive_automation_targets
)
kickstart_poll = autopilot_supervisor_incidents.kickstart_poll
suspend_billing_blocked_poll = (
    autopilot_supervisor_incidents.suspend_billing_blocked_poll
)
_repair_cooldown = autopilot_supervisor_incidents.repair_cooldown
_escalation_retry = autopilot_supervisor_incidents.escalation_retry


def collect_checks(
    config_path: Path,
    contract_path: Path,
) -> list[system_doctor.Check]:
    return autopilot_supervisor_incidents.collect_checks(
        config_path,
        contract_path,
    )


def new_incident(
    failures: list[system_doctor.Check],
    *,
    now: datetime,
    canary: bool = False,
) -> dict[str, Any]:
    return autopilot_supervisor_incidents.new_incident(
        failures,
        now=now,
        isoformat=isoformat,
        canary=canary,
    )


def recover_expired_coordination(
    incident: dict[str, Any],
    *,
    now: datetime,
) -> bool:
    return autopilot_supervisor_incidents.recover_expired_coordination(
        incident,
        now=now,
        parse_time=parse_time,
        isoformat=isoformat,
    )


def _incident_dependencies(
) -> autopilot_supervisor_incidents.Dependencies:
    return autopilot_supervisor_incidents.Dependencies(
        read_config=read_config,
        state_path=state_path,
        load_state=load_state,
        save_state=save_state,
        locked_state=autopilot_dispatch.locked_state,
        isoformat=isoformat,
        utc_now=utc_now,
        parse_time=parse_time,
        collect_checks=collect_checks,
        unarchive_targets=unarchive_automation_targets,
    )


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
    return autopilot_supervisor_incidents.run_once(
        config_path,
        contract_path,
        now=now,
        checks=checks,
        repair_runner=repair_runner,
        billing_block_runner=billing_block_runner,
        dependencies=_incident_dependencies(),
    )


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
        x_queue_recovery = is_x_queue_recovery_incident(incident)
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


def _route_dependencies() -> autopilot_supervisor_routes.Dependencies:
    return autopilot_supervisor_routes.Dependencies(
        read_config=read_config,
        read_contract=system_doctor.read_json,
        repair_gate=gate,
        reserve_repair_handoff=reserve_handoff,
        release_repair_handoff=release_handoff,
        active_x_owner_snapshot=active_x_owner_snapshot,
        outbound_state_path=outbound_state_path,
        outbound_interval_minutes=outbound_interval_minutes,
        bridge_reserve_handoff=autopilot_bridge.reserve_handoff,
        bridge_release_handoff=(
            autopilot_bridge.release_handoff_reservation
        ),
        outbound_active_owner=outbound_cycle.active_owner_snapshot,
        outbound_claim_due=outbound_cycle.claim_due,
        outbound_pause_slot=outbound_cycle.pause_slot,
    )


def relay_reserve_handoff(
    config_path: Path,
    contract_path: Path,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    return autopilot_supervisor_routes.relay_reserve_handoff(
        config_path,
        contract_path,
        lease_seconds=lease_seconds,
        now=now,
        dependencies=_route_dependencies(),
    )


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
        if is_x_queue_recovery_incident(incident):
            return {
                "status": str(incident.get("status")),
                "dispatch": False,
                "incident_id": incident["id"],
                "x_queue_recovery": True,
            }
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


def is_x_queue_recovery_incident(incident: dict[str, Any]) -> bool:
    failure_ids = {
        str(check.get("identifier"))
        for check in incident.get("checks", [])
        if isinstance(check, dict) and check.get("status") == "fail"
    }
    return autopilot_state_model.recovery_owner(failure_ids) == "x"


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
