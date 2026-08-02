#!/usr/bin/env python3
"""Mutually exclusive repair, inbound, and outbound reservation routes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


OWNER_CONTRACT_FAILURE = "thread.browser_owner"
OWNER_CONTRACT_ROTATION_REASON = "failed_owner_contract_repair"


@dataclass(frozen=True)
class Dependencies:
    read_config: Callable[[Path], dict[str, Any]]
    read_contract: Callable[[Path], dict[str, Any]]
    repair_gate: Callable[..., dict[str, Any]]
    reserve_repair_handoff: Callable[..., dict[str, Any]]
    release_repair_handoff: Callable[..., dict[str, Any]]
    active_x_owner_snapshot: Callable[..., dict[str, Any]]
    outbound_state_path: Callable[[Path], Path]
    outbound_interval_minutes: Callable[[Path], int]
    bridge_reserve_handoff: Callable[..., dict[str, Any]]
    bridge_release_handoff: Callable[..., dict[str, Any]]
    outbound_active_owner: Callable[..., dict[str, Any]]
    outbound_claim_due: Callable[..., dict[str, Any]]
    outbound_pause_slot: Callable[..., dict[str, Any]]
    owner_rotation_pending: Callable[..., dict[str, Any] | None]
    owner_rotation_reserve: Callable[..., dict[str, Any]]


def x_queue_is_exactly_idle(result: dict[str, Any]) -> bool:
    return (
        result.get("status") == "idle"
        and result.get("dispatch") is False
        and int(result.get("pending_count", 0)) == 0
        and not result.get("event_ids")
        and not result.get("owner_busy", False)
        and int(result.get("leased_count", 0)) == 0
    )


def _owner_route(
    config: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, str]:
    owner = contract["threads"]["browser_owner"]
    owner_thread_id = str(config.get("browser_owner_thread_id", "")).strip()
    if not owner_thread_id:
        raise ValueError("browser_owner_thread_id is required")
    return {
        "owner_thread_id": owner_thread_id,
        "owner_model": str(owner["model"]),
        "owner_thinking": str(owner["minimum_reasoning_effort"]),
    }


def _forced_rotation_reason(repair_state: dict[str, Any]) -> str | None:
    """Return a universal rotation reason for an unrepairable owner task."""

    if repair_state.get("status") != "failed":
        return None
    failures = {
        str(identifier)
        for identifier in repair_state.get("failures", [])
        if str(identifier).strip()
    }
    if OWNER_CONTRACT_FAILURE in failures:
        return OWNER_CONTRACT_ROTATION_REASON
    return None


def _reserve_repair(
    config_path: Path,
    repair_state: dict[str, Any],
    owner_route: dict[str, str],
    *,
    lease_seconds: int,
    now: datetime | None,
    dependencies: Dependencies,
) -> dict[str, Any]:
    x_owner = dependencies.active_x_owner_snapshot(
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
    repair = dependencies.reserve_repair_handoff(
        config_path,
        lease_seconds=lease_seconds,
        now=now,
    )
    if not repair.get("dispatch"):
        return {**repair, **owner_route, "route": "repair"}
    race_owner = dependencies.active_x_owner_snapshot(
        config_path,
        lease_seconds=lease_seconds,
        now=now,
    )
    if not race_owner["owner_busy"]:
        return {**repair, **owner_route, "route": "repair"}
    dependencies.release_repair_handoff(
        config_path,
        reservation_token=str(repair["reservation_token"]),
        reason="x_owner_became_active",
    )
    return {
        **repair_state,
        **owner_route,
        **race_owner,
        "status": "repair_waiting_for_x_owner",
        "dispatch": False,
        "route": "repair",
    }


def _reserve_inbound(
    config_path: Path,
    outbound_path: Path,
    owner_route: dict[str, str],
    *,
    lease_seconds: int,
    now: datetime | None,
    dependencies: Dependencies,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    x_result = dependencies.bridge_reserve_handoff(
        config_path,
        lease_seconds=lease_seconds,
    )
    if x_result.get("dispatch"):
        outbound_owner = dependencies.outbound_active_owner(
            outbound_path,
            now=now,
        )
        if outbound_owner.get("owner_busy"):
            dependencies.bridge_release_handoff(
                config_path,
                reservation_token=str(x_result.get("reservation_token", "")),
                reason="outbound_owner_must_pause_for_inbound",
            )
            return (
                {
                    **x_result,
                    **owner_route,
                    "status": "x_waiting_for_outbound_pause",
                    "dispatch": False,
                    "repair_pending": False,
                    "route": "x",
                    "outbound_owner": outbound_owner,
                },
                x_result,
            )
        return (
            {
                **x_result,
                **owner_route,
                "repair_pending": False,
                "route": "x",
            },
            x_result,
        )
    if not x_queue_is_exactly_idle(x_result):
        return (
            {
                **x_result,
                **owner_route,
                "repair_pending": False,
                "route": "x",
            },
            x_result,
        )
    return None, x_result


def _reserve_outbound(
    config_path: Path,
    outbound_path: Path,
    x_result: dict[str, Any],
    owner_route: dict[str, str],
    *,
    lease_seconds: int,
    now: datetime | None,
    dependencies: Dependencies,
) -> dict[str, Any]:
    x_owner = dependencies.active_x_owner_snapshot(
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
    outbound = dependencies.outbound_claim_due(
        outbound_path,
        lease_seconds=lease_seconds,
        interval_minutes=dependencies.outbound_interval_minutes(config_path),
        now=now,
    )
    if not outbound.get("dispatch"):
        return {
            **outbound,
            **owner_route,
            "repair_pending": False,
            "route": "outbound",
        }
    race_check = dependencies.bridge_reserve_handoff(
        config_path,
        lease_seconds=lease_seconds,
    )
    if x_queue_is_exactly_idle(race_check):
        return {
            **outbound,
            **owner_route,
            "repair_pending": False,
            "route": "outbound",
        }
    dependencies.outbound_pause_slot(
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


def relay_reserve_handoff(
    config_path: Path,
    contract_path: Path,
    *,
    lease_seconds: int,
    now: datetime | None,
    dependencies: Dependencies,
) -> dict[str, Any]:
    config = dependencies.read_config(config_path)
    contract = dependencies.read_contract(contract_path.expanduser().resolve())
    owner_route = _owner_route(config, contract)
    repair_state = dependencies.repair_gate(config_path, now=now)
    pending_rotation = dependencies.owner_rotation_pending(config_path, contract)
    if pending_rotation is not None:
        return {
            **pending_rotation,
            **owner_route,
            "repair_pending": bool(repair_state.get("repair_pending")),
        }
    forced_rotation_reason = _forced_rotation_reason(repair_state)
    if forced_rotation_reason is not None:
        outbound_path = dependencies.outbound_state_path(config_path)
        x_owner = dependencies.active_x_owner_snapshot(
            config_path,
            lease_seconds=lease_seconds,
            now=now,
        )
        outbound_owner = dependencies.outbound_active_owner(
            outbound_path,
            now=now,
        )
        rotation = dependencies.owner_rotation_reserve(
            config_path,
            contract,
            allow_new=True,
            owner_busy=bool(x_owner.get("owner_busy")),
            outbound_busy=bool(outbound_owner.get("owner_busy")),
            force_reason=forced_rotation_reason,
            now=now,
        )
        return {
            **rotation,
            **owner_route,
            "repair_pending": True,
            "forced_rotation": True,
        }
    if repair_state.get("repair_pending"):
        return _reserve_repair(
            config_path,
            repair_state,
            owner_route,
            lease_seconds=lease_seconds,
            now=now,
            dependencies=dependencies,
        )
    outbound_path = dependencies.outbound_state_path(config_path)
    x_owner = dependencies.active_x_owner_snapshot(
        config_path,
        lease_seconds=lease_seconds,
        now=now,
    )
    outbound_owner = dependencies.outbound_active_owner(
        outbound_path,
        now=now,
    )
    rotation = dependencies.owner_rotation_reserve(
        config_path,
        contract,
        allow_new=True,
        owner_busy=bool(x_owner.get("owner_busy")),
        outbound_busy=bool(outbound_owner.get("owner_busy")),
        now=now,
    )
    if rotation.get("dispatch"):
        return {
            **rotation,
            **owner_route,
            "repair_pending": bool(repair_state.get("repair_pending")),
        }
    inbound, x_result = _reserve_inbound(
        config_path,
        outbound_path,
        owner_route,
        lease_seconds=lease_seconds,
        now=now,
        dependencies=dependencies,
    )
    if inbound is not None:
        return inbound
    return _reserve_outbound(
        config_path,
        outbound_path,
        x_result,
        owner_route,
        lease_seconds=lease_seconds,
        now=now,
        dependencies=dependencies,
    )
