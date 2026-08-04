#!/usr/bin/env python3
"""Canonical bounded inbound claim and read-only tab policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


DEFAULT_MAX_CLAIM_EVENTS = 1
DEFAULT_MAX_PARALLEL_READ_TABS = 1
MAX_SUPPORTED_CLAIM_EVENTS = 3
MAX_SUPPORTED_PARALLEL_READ_TABS = 3
RESOURCE_MODE_READ_TAB_CAPS = {
    "efficiency": 1,
    "balanced": 2,
    "performance": 3,
}


def _bounded_integer(
    payload: Mapping[str, Any],
    key: str,
    *,
    default: int,
    maximum: int,
) -> int:
    raw = payload.get(key, default)
    if type(raw) is not int:
        raise ValueError(f"{key} must be an integer")
    value = raw
    if not 1 <= value <= maximum:
        raise ValueError(f"{key} must be between 1 and {maximum}")
    return value


@dataclass(frozen=True)
class InboundPolicy:
    claim_events: int
    parallel_read_tabs: int

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "InboundPolicy":
        policy = cls(
            claim_events=_bounded_integer(
                config,
                "autopilot_max_claim_events",
                default=DEFAULT_MAX_CLAIM_EVENTS,
                maximum=MAX_SUPPORTED_CLAIM_EVENTS,
            ),
            parallel_read_tabs=_bounded_integer(
                config,
                "autopilot_max_parallel_read_tabs",
                default=DEFAULT_MAX_PARALLEL_READ_TABS,
                maximum=MAX_SUPPORTED_PARALLEL_READ_TABS,
            ),
        )
        if policy.parallel_read_tabs > policy.claim_events:
            raise ValueError(
                "autopilot_max_parallel_read_tabs must not exceed "
                "autopilot_max_claim_events"
            )
        return policy

    @classmethod
    def from_runtime_contract(
        cls,
        runtime: Mapping[str, Any],
    ) -> "InboundPolicy":
        if "inbound_claim_max_events" not in runtime:
            raise ValueError("runtime contract misses inbound claim limit")
        if "inbound_max_parallel_read_tabs" not in runtime:
            raise ValueError("runtime contract misses inbound read tab limit")
        return cls.from_config(
            {
                "autopilot_max_claim_events": runtime[
                    "inbound_claim_max_events"
                ],
                "autopilot_max_parallel_read_tabs": runtime[
                    "inbound_max_parallel_read_tabs"
                ],
            }
        )

    def effective_read_tabs(
        self,
        event_count: int,
        *,
        resource_mode: str | None = None,
    ) -> int:
        if event_count <= 0:
            raise ValueError("event_count must be positive")
        limit = self.parallel_read_tabs
        if resource_mode:
            try:
                limit = min(
                    limit,
                    RESOURCE_MODE_READ_TAB_CAPS[resource_mode],
                )
            except KeyError as error:
                raise ValueError(
                    f"Unsupported resource mode: {resource_mode}"
                ) from error
        return min(event_count, limit)
