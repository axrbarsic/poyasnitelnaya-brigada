from __future__ import annotations

import unittest

from scripts import autopilot_emulator, autopilot_state_model


class AutopilotStateModelTests(unittest.TestCase):
    def test_recovery_owner_is_explicit_and_closed(self) -> None:
        self.assertEqual(
            autopilot_state_model.recovery_owner([]),
            "none",
        )
        self.assertEqual(
            autopilot_state_model.recovery_owner(
                ["runtime.relay_progress", "runtime.queue_latency"]
            ),
            "x",
        )
        self.assertEqual(
            autopilot_state_model.recovery_owner(
                ["runtime.queue_latency", "runtime.database"]
            ),
            "doctor",
        )

    def test_exhaustive_model_has_no_invariant_counterexample(self) -> None:
        report = autopilot_emulator.exhaustive_report()

        self.assertGreater(report["reachable_combinations"], 50_000)
        self.assertGreater(report["unreachable_combinations"], 10_000)
        self.assertEqual(report["invariant_failure_count"], 0)
        self.assertEqual(report["unsafe_probability_mass"], "0/1")

    def test_poll_failure_never_advances_cursor_or_queues(self) -> None:
        case = autopilot_state_model.Combination(
            event_count=3,
            poll="failure",
            source="mentions",
            relationship="direct",
            mandatory=True,
            author="external",
            resource="available",
            owner_state="idle",
            owner_lease="none",
            reservation="none",
            browser="publish",
            durability="ok",
        )

        outcome = autopilot_state_model.evaluate(case)

        self.assertFalse(outcome.cursor_advanced)
        self.assertEqual(outcome.queued_after, 0)
        self.assertEqual(outcome.published_visible, 0)

    def test_nonterminal_failure_paths_preserve_the_queue(self) -> None:
        for terminal in (
            "deferred",
            "owner_active",
            "handoff_reserved",
            "handoff_released",
            "retryable_failure",
            "durability_blocked",
        ):
            matching = []
            for case in autopilot_state_model.combinations():
                outcome = autopilot_state_model.evaluate(case)
                if outcome.terminal == terminal:
                    matching.append(outcome)
            self.assertTrue(matching, terminal)
            self.assertTrue(
                all(
                    outcome.queued_after == outcome.queued_before_owner
                    for outcome in matching
                ),
                terminal,
            )

    def test_seeded_trace_is_reproducible_and_drains(self) -> None:
        first, first_failures = autopilot_state_model.run_trace(41, 250)
        second, second_failures = autopilot_state_model.run_trace(41, 250)

        self.assertEqual(first, second)
        self.assertEqual(first_failures, second_failures)
        self.assertEqual(first_failures, [])
        self.assertEqual(first.queued, set())


if __name__ == "__main__":
    unittest.main()
