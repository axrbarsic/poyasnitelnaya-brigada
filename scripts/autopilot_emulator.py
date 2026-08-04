#!/usr/bin/env python3
"""Exhaustive bounded model check plus seeded generative trace testing."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

try:
    from scripts import autopilot_state_model
except ModuleNotFoundError:
    import autopilot_state_model  # type: ignore[no-redef]


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def exhaustive_report() -> dict[str, Any]:
    total = 0
    reachable = 0
    unsafe = 0
    terminal_counts: Counter[str] = Counter()
    failures: list[dict[str, Any]] = []

    for case in autopilot_state_model.combinations():
        total += 1
        outcome = autopilot_state_model.evaluate(case)
        if not outcome.reachable:
            continue
        reachable += 1
        terminal_counts[outcome.terminal] += 1
        case_failures = autopilot_state_model.invariant_failures(case, outcome)
        if case_failures:
            unsafe += 1
            if len(failures) < 20:
                failures.append(
                    {
                        "case": case.__dict__,
                        "outcome": outcome.__dict__,
                        "failures": case_failures,
                    }
                )
    unsafe_mass = Fraction(unsafe, reachable) if reachable else Fraction(0, 1)
    return {
        "factor_count": len(autopilot_state_model.FACTOR_SPACE),
        "factors": {
            name: list(values)
            for name, values in autopilot_state_model.FACTOR_SPACE.items()
        },
        "cartesian_combinations": total,
        "reachable_combinations": reachable,
        "unreachable_combinations": total - reachable,
        "terminal_counts": dict(sorted(terminal_counts.items())),
        "invariant_failure_count": unsafe,
        "counterexamples": failures,
        "probability_profile": "synthetic_uniform_over_reachable_combinations",
        "unsafe_probability_mass": (
            f"{unsafe_mass.numerator}/{unsafe_mass.denominator}"
        ),
        "probability_warning": (
            "This is exact mass inside the finite model, not an empirical "
            "estimate of production incident frequency."
        ),
    }


def fuzz_report(seed: int, traces: int, steps: int) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    failing_trace_count = 0
    total_events = 0
    total_completed_runs = 0
    for offset in range(traces):
        trace_seed = seed + offset
        state, trace_failures = autopilot_state_model.run_trace(
            trace_seed,
            steps,
        )
        total_events += state.next_event
        total_completed_runs += state.completed_runs
        if trace_failures:
            failing_trace_count += 1
            if len(failures) < 20:
                failures.append(
                    {
                        "seed": trace_seed,
                        "failures": trace_failures[:20],
                    }
                )
    return {
        "seed_start": seed,
        "trace_count": traces,
        "steps_per_trace": steps,
        "generated_steps": traces * steps,
        "generated_events": total_events,
        "completed_runs": total_completed_runs,
        "failing_trace_count": failing_trace_count,
        "counterexamples": failures,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run an offline exhaustive state-space check and seeded fault "
            "injection traces. It never connects to X or Browser."
        )
    )
    parser.add_argument("--seed", type=int, default=20260727)
    parser.add_argument("--traces", type=int, default=10000)
    parser.add_argument("--steps", type=int, default=100)
    parser.add_argument("--report", type=Path)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    if arguments.traces < 1 or arguments.steps < 1:
        raise SystemExit("--traces and --steps must be positive")
    report = {
        "schema_version": 1,
        "generated_at": iso_now(),
        "scope": "offline_autopilot_digital_twin",
        "network_access": False,
        "browser_access": False,
        "exhaustive": exhaustive_report(),
        "fuzz": fuzz_report(
            arguments.seed,
            arguments.traces,
            arguments.steps,
        ),
    }
    report["passed"] = (
        report["exhaustive"]["invariant_failure_count"] == 0
        and report["fuzz"]["failing_trace_count"] == 0
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if arguments.report is not None:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
