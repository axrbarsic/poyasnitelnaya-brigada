#!/usr/bin/env python3
"""Read-only relay progress and queue latency diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


VALID_STATUSES = {"pass", "warn", "fail"}
ACTIVE_REPAIR_STATUSES = {
    "repair_observing",
    "escalation_pending",
    "handoff_pending",
    "claimed",
    "work_in_progress",
}
ACTIVE_X_DELIVERY_STATUSES = {
    "desktop_launched_waiting_relay",
    "desktop_ready_waiting_relay",
}
ACTIVE_EVENT_DISPATCH_STATUSES = {
    "dispatch_requested",
    "dispatch_kicked",
}
CLAIM_COMPLETED_DISPATCH_REASON = "claim_completed_with_pending_queue"


@dataclass(frozen=True)
class Check:
    identifier: str
    status: str
    summary: str
    repair: str = ""
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"invalid check status: {self.status}")


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def completion_dispatch_progress(
    state: Any,
    *,
    current: datetime,
    grace_seconds: int,
    required_event_id: str | None,
) -> dict[str, Any]:
    delivery = state if isinstance(state, dict) else {}
    status = str(delivery.get("status", ""))
    reason = str(delivery.get("reason", ""))
    raw_event_ids = delivery.get("event_ids")
    event_ids = (
        {
            str(event_id)
            for event_id in raw_event_ids
            if str(event_id)
        }
        if isinstance(raw_event_ids, list)
        else set()
    )
    requested_at = parse_timestamp(delivery.get("requested_at"))
    age_seconds = (
        (current - requested_at).total_seconds()
        if requested_at is not None
        else None
    )
    covers_required = (
        required_event_id is None or required_event_id in event_ids
    )
    active = (
        grace_seconds > 0
        and status in ACTIVE_EVENT_DISPATCH_STATUSES
        and reason == CLAIM_COMPLETED_DISPATCH_REASON
        and bool(event_ids)
        and covers_required
        and age_seconds is not None
        and 0 <= age_seconds <= grace_seconds
    )
    return {
        "active": active,
        "status": status,
        "reason": reason,
        "event_ids": sorted(event_ids),
        "covers_required": covers_required,
        "age_seconds": age_seconds,
    }


def _relay_immediate_result(
    *,
    pending_count: int,
    owner: Any,
    work_kind: str,
    dispatch_status: str,
    details: dict[str, Any],
) -> Check | None:
    if pending_count <= 0:
        return Check(
            "runtime.relay_progress",
            "pass",
            "Relay не имеет ожидающей очереди.",
            "Проверь wake queue и dispatcher state.",
            details,
        )
    if isinstance(owner, dict):
        details["owner_event_ids"] = owner.get("event_ids", [])
        return Check(
            "runtime.relay_progress",
            "pass",
            "Ожидающая очередь уже принадлежит Browser owner.",
            "Проверь owner lease и terminal исход текущего claim.",
            details,
        )
    if work_kind == "repair" and dispatch_status in ACTIVE_REPAIR_STATUSES:
        return Check(
            "runtime.relay_progress",
            "pass",
            "Relay выполняет активный repair handoff.",
            "Возраст X очереди контролируется отдельным SLO.",
            details,
        )
    if dispatch_status == "repair_waiting":
        return Check(
            "runtime.relay_progress",
            "pass",
            "Relay намеренно уступил очередь активному repair owner.",
            "Проверь supervisor lease и runtime.queue_latency.",
            details,
        )
    if dispatch_status == "deferred_resources":
        return Check(
            "runtime.relay_progress",
            "pass",
            "Relay намеренно отложен resource guard.",
            "Проверь возраст очереди и освободи только безопасные ресурсы.",
            details,
        )
    return None


def _relay_completion_progress(
    event_dispatch_state: Any,
    details: dict[str, Any],
    *,
    current: datetime,
    max_wait_seconds: int,
    required_event_id: str | None,
) -> dict[str, Any]:
    progress = completion_dispatch_progress(
        event_dispatch_state,
        current=current,
        grace_seconds=max_wait_seconds,
        required_event_id=required_event_id,
    )
    if progress["status"]:
        details.update(
            {
                "event_dispatch_status": progress["status"],
                "event_dispatch_reason": progress["reason"],
                "event_dispatch_covers_oldest": progress["covers_required"],
                "event_dispatch_age_seconds": (
                    round(progress["age_seconds"], 1)
                    if progress["age_seconds"] is not None
                    else None
                ),
            }
        )
    return progress


def _relay_waiting_result(
    dispatch_state: dict[str, Any],
    details: dict[str, Any],
    *,
    dispatch_status: str,
    max_wait_seconds: int,
    current: datetime,
) -> Check:
    if dispatch_status not in ACTIVE_X_DELIVERY_STATUSES:
        return Check(
            "runtime.relay_progress",
            "fail",
            "Ожидающая очередь не получила owner claim.",
            "Проверь dispatcher, self-owned heartbeat x-relay и reservation.",
            details,
        )
    waiting_since = parse_timestamp(
        dispatch_state.get("waiting_since")
        or dispatch_state.get("checked_at")
    )
    if waiting_since is None:
        details["waiting_since"] = dispatch_state.get("waiting_since")
        return Check(
            "runtime.relay_progress",
            "fail",
            "Dispatcher не записал начало ожидания owner heartbeat.",
            "Перезапусти штатный dispatcher и проверь waiting_since.",
            details,
        )
    age_seconds = (current - waiting_since).total_seconds()
    details["waiting_since"] = waiting_since.isoformat().replace(
        "+00:00",
        "Z",
    )
    details["age_seconds"] = round(age_seconds, 1)
    healthy = 0 <= age_seconds <= max_wait_seconds
    return Check(
        "runtime.relay_progress",
        "pass" if healthy else "fail",
        (
            f"Heartbeat ожидает claim {round(age_seconds, 1)}s."
            if healthy
            else f"Heartbeat не создал claim за {round(age_seconds, 1)}s."
        ),
        (
            "Дождись ближайшего минутного heartbeat."
            if healthy
            else "Проверь self-owned heartbeat x-relay и reservation."
        ),
        details,
    )


def relay_progress_check(
    *,
    pending_count: int,
    dispatch_state: dict[str, Any],
    owner: Any,
    event_dispatch_state: Any = None,
    required_event_id: str | None = None,
    max_wait_seconds: int,
    now: datetime | None = None,
) -> Check:
    if max_wait_seconds <= 0:
        raise ValueError("relay progress max wait must be positive")
    current = now or datetime.now(timezone.utc)
    dispatch_status = str(dispatch_state.get("status", "missing"))
    work_kind = str(dispatch_state.get("work_kind", ""))
    details: dict[str, Any] = {
        "dispatch_status": dispatch_status,
        "max_wait_seconds": max_wait_seconds,
        "pending_count": pending_count,
    }
    if work_kind:
        details["work_kind"] = work_kind
    immediate = _relay_immediate_result(
        pending_count=pending_count,
        owner=owner,
        work_kind=work_kind,
        dispatch_status=dispatch_status,
        details=details,
    )
    if immediate is not None:
        return immediate
    completion = _relay_completion_progress(
        event_dispatch_state,
        details,
        current=current,
        max_wait_seconds=max_wait_seconds,
        required_event_id=required_event_id,
    )
    if completion["active"]:
        return Check(
            "runtime.relay_progress",
            "pass",
            "Следующий X-handoff уже запрошен после завершения claim.",
            "Дождись self-owned heartbeat в пределах grace; затем снова "
            "проверь очередь.",
            details,
        )
    return _relay_waiting_result(
        dispatch_state,
        details,
        dispatch_status=dispatch_status,
        max_wait_seconds=max_wait_seconds,
        current=current,
    )


def _oldest_queue_event(
    events: list[Any],
    *,
    max_age_seconds: int,
) -> tuple[Check | None, tuple[str, datetime] | None]:
    if not events:
        return (
            Check(
                "runtime.queue_latency",
                "pass",
                "Ожидающая очередь пуста.",
                "Проверь wake queue и poll health.",
                {"pending_count": 0, "max_age_seconds": max_age_seconds},
            ),
            None,
        )
    observed: list[tuple[str, datetime]] = []
    invalid_event_ids: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            invalid_event_ids.append("<invalid>")
            continue
        event_id = str(event.get("id") or event.get("event_id") or "")
        first_seen = parse_timestamp(event.get("first_seen_at"))
        if not event_id or first_seen is None:
            invalid_event_ids.append(event_id or "<missing>")
            continue
        observed.append((event_id, first_seen))
    if invalid_event_ids:
        return (
            Check(
                "runtime.queue_latency",
                "fail",
                "Возраст части ожидающей очереди нельзя измерить.",
                "Восстанови first_seen_at из live SQLite, не удаляя события.",
                {
                    "invalid_event_ids": invalid_event_ids,
                    "pending_count": len(events),
                    "max_age_seconds": max_age_seconds,
                },
            ),
            None,
        )
    return None, min(observed, key=lambda item: item[1])


def _owner_context(
    owner: Any,
    *,
    oldest_event_id: str,
    current: datetime,
) -> dict[str, Any]:
    event_ids = (
        {
            str(event_id)
            for event_id in owner.get("event_ids", [])
            if str(event_id)
        }
        if isinstance(owner, dict)
        else set()
    )
    lease_expires = (
        parse_timestamp(owner.get("lease_expires_at"))
        if isinstance(owner, dict)
        else None
    )
    return {
        "owned": oldest_event_id in event_ids,
        "active_owner": bool(event_ids)
        and (lease_expires is None or lease_expires >= current),
    }


def _active_repair(supervisor_state: Any, *, current: datetime) -> bool:
    incident = (
        supervisor_state.get("incident")
        if isinstance(supervisor_state, dict)
        else None
    )
    owner = incident.get("owner") if isinstance(incident, dict) else None
    lease_expires = (
        parse_timestamp(owner.get("lease_expires_at"))
        if isinstance(owner, dict)
        else None
    )
    return (
        isinstance(incident, dict)
        and str(incident.get("status", "")) in ACTIVE_REPAIR_STATUSES
        and isinstance(owner, dict)
        and bool(str(owner.get("claim_token", "")))
        and lease_expires is not None
        and lease_expires >= current
    )


def _delivery_context(
    dispatch_state: Any,
    event_dispatch_state: Any,
    *,
    oldest_event_id: str,
    current: datetime,
    grace_seconds: int,
) -> dict[str, Any]:
    delivery = dispatch_state if isinstance(dispatch_state, dict) else {}
    status = str(delivery.get("status", ""))
    work_kind = str(delivery.get("work_kind", ""))
    raw_event_ids = delivery.get("event_ids")
    event_ids = (
        {
            str(event_id)
            for event_id in raw_event_ids
            if str(event_id)
        }
        if isinstance(raw_event_ids, list)
        else set()
    )
    waiting_since = parse_timestamp(delivery.get("waiting_since"))
    age_seconds = (
        (current - waiting_since).total_seconds()
        if waiting_since is not None
        else None
    )
    app_active = (
        grace_seconds > 0
        and status in ACTIVE_X_DELIVERY_STATUSES
        and work_kind == "x"
        and oldest_event_id in event_ids
        and age_seconds is not None
        and 0 <= age_seconds <= grace_seconds
    )
    completion = completion_dispatch_progress(
        event_dispatch_state,
        current=current,
        grace_seconds=grace_seconds,
        required_event_id=oldest_event_id,
    )
    return {
        "status": status,
        "work_kind": work_kind,
        "event_ids": event_ids,
        "age_seconds": age_seconds,
        "completion": completion,
        "active": app_active or completion["active"],
        "source": (
            "event_dispatch"
            if completion["active"]
            else "app_server_dispatch"
            if app_active
            else None
        ),
    }


def _queue_details(
    *,
    age_seconds: float,
    max_age_seconds: int,
    oldest_event_id: str,
    oldest_seen: datetime,
    owner_context: dict[str, Any],
    active_repair: bool,
    delivery_context: dict[str, Any],
    delivery_grace_seconds: int,
    pending_count: int,
) -> dict[str, Any]:
    details = {
        "age_seconds": round(age_seconds, 1),
        "max_age_seconds": max_age_seconds,
        "oldest_event_id": oldest_event_id,
        "oldest_first_seen_at": oldest_seen.isoformat().replace(
            "+00:00",
            "Z",
        ),
        "active_owner": owner_context["active_owner"],
        "active_repair": active_repair,
        "active_x_delivery": delivery_context["active"],
        "owned": owner_context["owned"],
        "pending_count": pending_count,
    }
    if delivery_context["status"]:
        details.update(
            {
                "delivery_status": delivery_context["status"],
                "delivery_work_kind": delivery_context["work_kind"],
                "delivery_covers_oldest": (
                    oldest_event_id in delivery_context["event_ids"]
                ),
                "delivery_grace_seconds": delivery_grace_seconds,
                "delivery_age_seconds": (
                    round(delivery_context["age_seconds"], 1)
                    if delivery_context["age_seconds"] is not None
                    else None
                ),
            }
        )
    completion = delivery_context["completion"]
    if completion["status"]:
        details.update(
            {
                "event_dispatch_status": completion["status"],
                "event_dispatch_reason": completion["reason"],
                "event_dispatch_covers_oldest": completion["covers_required"],
                "event_dispatch_age_seconds": (
                    round(completion["age_seconds"], 1)
                    if completion["age_seconds"] is not None
                    else None
                ),
            }
        )
    if delivery_context["source"]:
        details["delivery_source"] = delivery_context["source"]
    return details


def _queue_message(
    *,
    healthy: bool,
    owned: bool,
    active_owner: bool,
    active_repair: bool,
    active_x_delivery: bool,
    age_seconds: float,
) -> tuple[str, str]:
    if healthy:
        return (
            f"Старейшее событие ожидает {round(age_seconds, 1)}s.",
            "Проверь relay progress при росте возраста.",
        )
    if owned and active_owner:
        return (
            "Старейшее событие превысило SLO, но уже принадлежит "
            "активному Browser owner.",
            "Проверь renew, durable history и завершение текущего claim.",
        )
    if active_owner:
        return (
            "Старейшее ожидающее событие превысило SLO, пока активный "
            "Browser owner обрабатывает предыдущую bounded batch.",
            "Проверь продвижение текущего claim и следующий автоматический "
            "handoff.",
        )
    if active_repair:
        return (
            "Старейшее ожидающее событие превысило SLO во время "
            "активного repair handoff.",
            "Заверши repair handoff, затем проверь следующий X claim.",
        )
    if active_x_delivery:
        return (
            "Старейшее ожидающее событие превысило SLO, но свежая "
            "X-доставка уже ожидает owner claim.",
            "Дождись owner claim в пределах relay grace; после grace "
            "зависшая очередь снова станет FAIL.",
        )
    return (
        "Старейшее событие превысило SLO без активного owner.",
        "Проверь dispatcher, x-relay и owner claim, очередь не удаляй.",
    )


def queue_latency_check(
    *,
    events: list[Any],
    owner: Any,
    supervisor_state: Any = None,
    dispatch_state: Any = None,
    event_dispatch_state: Any = None,
    delivery_grace_seconds: int = 0,
    max_age_seconds: int,
    now: datetime | None = None,
) -> Check:
    if max_age_seconds <= 0:
        raise ValueError("queue latency max age must be positive")
    if delivery_grace_seconds < 0:
        raise ValueError("X delivery grace must not be negative")
    early, oldest = _oldest_queue_event(
        events,
        max_age_seconds=max_age_seconds,
    )
    if early is not None:
        return early
    if oldest is None:
        raise AssertionError("oldest queue event is missing")
    oldest_event_id, oldest_seen = oldest
    current = now or datetime.now(timezone.utc)
    age_seconds = (current - oldest_seen).total_seconds()
    owner_context = _owner_context(
        owner,
        oldest_event_id=oldest_event_id,
        current=current,
    )
    active_repair = _active_repair(supervisor_state, current=current)
    delivery_context = _delivery_context(
        dispatch_state,
        event_dispatch_state,
        oldest_event_id=oldest_event_id,
        current=current,
        grace_seconds=delivery_grace_seconds,
    )
    healthy = 0 <= age_seconds <= max_age_seconds
    status = (
        "pass"
        if healthy
        else "warn"
        if (
            owner_context["active_owner"]
            or active_repair
            or delivery_context["active"]
        )
        else "fail"
    )
    details = _queue_details(
        age_seconds=age_seconds,
        max_age_seconds=max_age_seconds,
        oldest_event_id=oldest_event_id,
        oldest_seen=oldest_seen,
        owner_context=owner_context,
        active_repair=active_repair,
        delivery_context=delivery_context,
        delivery_grace_seconds=delivery_grace_seconds,
        pending_count=len(events),
    )
    summary, repair = _queue_message(
        healthy=healthy,
        owned=owner_context["owned"],
        active_owner=owner_context["active_owner"],
        active_repair=active_repair,
        active_x_delivery=delivery_context["active"],
        age_seconds=age_seconds,
    )
    return Check("runtime.queue_latency", status, summary, repair, details)
