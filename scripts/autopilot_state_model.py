#!/usr/bin/env python3
"""Finite state model for exhaustive and generative autopilot verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from random import Random
from typing import Any, Iterable


X_DELIVERY_FAILURES = frozenset(
    {
        "runtime.dispatch_health",
        "runtime.queue_latency",
        "runtime.relay_progress",
    }
)


def recovery_owner(failure_ids: Iterable[str]) -> str:
    """Return the only subsystem allowed to recover these failures."""

    identifiers = frozenset(str(value) for value in failure_ids)
    if not identifiers:
        return "none"
    if identifiers <= X_DELIVERY_FAILURES:
        return "x"
    return "doctor"


FACTOR_SPACE: dict[str, tuple[Any, ...]] = {
    "event_count": (0, 1, 3),
    "poll": ("success", "failure"),
    "source": ("mentions", "tail"),
    "relationship": (
        "direct",
        "mentioned_other_parent",
        "tracked_nested",
        "untracked_nested",
    ),
    "mandatory": (False, True),
    "author": ("external", "self"),
    "resource": ("available", "voice_deferred", "memory_deferred"),
    "owner_state": ("idle", "not_loaded", "active", "unreadable"),
    "owner_lease": ("none", "active", "expired"),
    "reservation": ("none", "active", "expired"),
    "browser": ("publish", "already_answered", "transient_failure"),
    "durability": ("ok", "conflict"),
}


@dataclass(frozen=True)
class Combination:
    event_count: int
    poll: str
    source: str
    relationship: str
    mandatory: bool
    author: str
    resource: str
    owner_state: str
    owner_lease: str
    reservation: str
    browser: str
    durability: str


@dataclass(frozen=True)
class Outcome:
    reachable: bool
    cursor_advanced: bool
    queued_before_owner: int
    queued_after: int
    claimed: int
    published_visible: int
    resolved: int
    history_turns: int
    handoff_count: int
    terminal: str


def combinations() -> Iterable[Combination]:
    keys = tuple(FACTOR_SPACE)
    for values in product(*(FACTOR_SPACE[key] for key in keys)):
        yield Combination(**dict(zip(keys, values)))


def _reachable(case: Combination) -> bool:
    if case.source == "tail":
        return case.relationship == "tracked_nested"
    return True


def _eligible(case: Combination) -> bool:
    if case.event_count == 0 or case.author == "self":
        return False
    if case.relationship in {"direct", "tracked_nested"}:
        return True
    return case.source == "mentions" and case.mandatory


def evaluate(case: Combination) -> Outcome:
    reachable = _reachable(case)
    if not reachable:
        return Outcome(
            False, False, 0, 0, 0, 0, 0, 0, 0, "unreachable"
        )
    if case.poll == "failure":
        return Outcome(
            True, False, 0, 0, 0, 0, 0, 0, 0, "poll_failed"
        )

    queued = case.event_count if _eligible(case) else 0
    if queued == 0:
        return Outcome(
            True, True, 0, 0, 0, 0, 0, 0, 0, "idle"
        )
    if case.resource != "available":
        return Outcome(
            True, True, queued, queued, 0, 0, 0, 0, 0, "deferred"
        )
    if case.owner_lease == "active":
        return Outcome(
            True, True, queued, queued, 0, 0, 0, 0, 0, "owner_active"
        )
    if case.reservation == "active":
        return Outcome(
            True, True, queued, queued, 0, 0, 0, 0, 0,
            "handoff_reserved",
        )
    if case.owner_state in {"active", "unreadable"}:
        return Outcome(
            True, True, queued, queued, 0, 0, 0, 0, 1,
            "handoff_released",
        )

    claimed = queued
    if case.browser == "transient_failure":
        return Outcome(
            True, True, queued, queued, claimed, 0, 0, 0, 1,
            "retryable_failure",
        )
    if case.durability == "conflict":
        visible = claimed if case.browser == "publish" else 0
        return Outcome(
            True, True, queued, queued, claimed, visible, 0, 0, 1,
            "durability_blocked",
        )
    if case.browser == "already_answered":
        return Outcome(
            True, True, queued, 0, claimed, 0, claimed, claimed * 2, 1,
            "already_answered",
        )
    return Outcome(
        True, True, queued, 0, claimed, claimed, claimed, claimed * 2, 1,
        "completed",
    )


def invariant_failures(case: Combination, outcome: Outcome) -> list[str]:
    failures: list[str] = []
    if not outcome.reachable:
        return failures
    if case.poll == "failure" and outcome.cursor_advanced:
        failures.append("cursor_advanced_after_poll_failure")
    if case.author == "self" and outcome.queued_before_owner:
        failures.append("self_authored_event_queued")
    if outcome.resolved and outcome.history_turns < outcome.resolved * 2:
        failures.append("resolution_without_exact_history_pair")
    if outcome.published_visible and not outcome.claimed:
        failures.append("publication_without_owner_claim")
    if outcome.handoff_count > 1:
        failures.append("duplicate_handoff")
    if outcome.queued_after < 0:
        failures.append("negative_queue")
    if outcome.terminal in {
        "deferred",
        "owner_active",
        "handoff_reserved",
        "handoff_released",
        "retryable_failure",
        "durability_blocked",
    } and outcome.queued_after != outcome.queued_before_owner:
        failures.append("queue_lost_on_nonterminal_path")
    if outcome.terminal == "completed" and outcome.queued_after:
        failures.append("completed_with_pending_queue")
    return failures


@dataclass
class TraceState:
    next_event: int = 0
    queued: set[str] = field(default_factory=set)
    self_authored: set[str] = field(default_factory=set)
    visible_replies: set[str] = field(default_factory=set)
    publication_counts: dict[str, int] = field(default_factory=dict)
    history: set[str] = field(default_factory=set)
    resolved: set[str] = field(default_factory=set)
    reservation: bool = False
    claimed: set[str] = field(default_factory=set)
    completed_runs: int = 0

    def apply(self, action: str) -> None:
        if action == "observe_external":
            event_id = f"event-{self.next_event}"
            self.next_event += 1
            self.queued.add(event_id)
        elif action == "observe_self":
            event_id = f"event-{self.next_event}"
            self.next_event += 1
            self.self_authored.add(event_id)
        elif action == "reserve":
            if self.queued and not self.reservation and not self.claimed:
                self.reservation = True
        elif action == "release_reservation":
            self.reservation = False
        elif action == "claim":
            if self.reservation and not self.claimed:
                self.claimed = set(self.queued)
                self.reservation = False
        elif action == "publish":
            unresolved = sorted(self.claimed - self.resolved)
            if unresolved:
                event_id = unresolved[0]
                if event_id not in self.visible_replies:
                    self.visible_replies.add(event_id)
                    self.publication_counts[event_id] = (
                        self.publication_counts.get(event_id, 0) + 1
                    )
        elif action == "sync":
            candidates = self.claimed & self.visible_replies
            for event_id in candidates:
                self.history.add(event_id)
                self.resolved.add(event_id)
                self.queued.discard(event_id)
        elif action == "already_answered":
            unresolved = sorted(self.claimed - self.resolved)
            if unresolved:
                event_id = unresolved[0]
                self.visible_replies.add(event_id)
                self.history.add(event_id)
                self.resolved.add(event_id)
                self.queued.discard(event_id)
        elif action in {"fail_owner", "expire_owner"}:
            self.claimed.clear()
            self.reservation = False
        elif action == "complete":
            if self.claimed and self.claimed <= self.resolved:
                self.claimed.clear()
                self.completed_runs += 1
        elif action in {
            "poll_failure",
            "voice_defer",
            "memory_defer",
            "owner_active",
        }:
            return
        else:
            raise ValueError(f"unknown action: {action}")

    def failures(self) -> list[str]:
        failures: list[str] = []
        if self.self_authored & self.queued:
            failures.append("self_authored_event_queued")
        if self.resolved - self.history:
            failures.append("resolution_without_history")
        if self.visible_replies - (self.queued | self.resolved):
            failures.append("visible_reply_lost_from_durable_state")
        if self.claimed - (self.queued | self.resolved):
            failures.append("claim_lost_event")
        if self.reservation and self.claimed:
            failures.append("reservation_and_owner_overlap")
        if any(count > 1 for count in self.publication_counts.values()):
            failures.append("duplicate_publication")
        return failures

    def drain(self) -> None:
        self.claimed.clear()
        self.reservation = False
        while self.queued:
            self.reservation = True
            self.apply("claim")
            for event_id in sorted(self.claimed):
                if event_id not in self.visible_replies:
                    self.visible_replies.add(event_id)
                    self.publication_counts[event_id] = (
                        self.publication_counts.get(event_id, 0) + 1
                    )
                self.history.add(event_id)
                self.resolved.add(event_id)
                self.queued.discard(event_id)
            self.apply("complete")


TRACE_ACTIONS = (
    "observe_external",
    "observe_self",
    "reserve",
    "release_reservation",
    "claim",
    "publish",
    "sync",
    "already_answered",
    "fail_owner",
    "expire_owner",
    "complete",
    "poll_failure",
    "voice_defer",
    "memory_defer",
    "owner_active",
)


def run_trace(seed: int, steps: int) -> tuple[TraceState, list[str]]:
    random = Random(seed)
    state = TraceState()
    failures: list[str] = []
    for index in range(steps):
        action = random.choice(TRACE_ACTIONS)
        state.apply(action)
        for failure in state.failures():
            failures.append(f"step={index};action={action};{failure}")
    state.drain()
    for failure in state.failures():
        failures.append(f"drain;{failure}")
    if state.queued:
        failures.append("drain_left_pending_events")
    return state, failures
