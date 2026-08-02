from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import inbound_route_control


class InboundRouteControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "route-control.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_default_mode_is_normal(self) -> None:
        state = inbound_route_control.load(self.state)

        self.assertEqual(state["mode"], "normal")
        self.assertEqual(state["routes"], {})

    def test_simple_wave_prioritizes_known_ordinary_and_excludes_other_work(
        self,
    ) -> None:
        inbound_route_control.set_mode(
            self.state,
            mode="simple-wave",
            updated_at="2026-08-02T16:30:00Z",
            updated_by="Alex",
            pending_event_ids=["101", "102", "103"],
        )
        inbound_route_control.record_route(
            self.state,
            event_id="101",
            route="local-max",
            classified_at="2026-08-02T16:31:00Z",
        )
        inbound_route_control.record_route(
            self.state,
            event_id="102",
            route="short",
            classified_at="2026-08-02T16:31:01Z",
        )

        priority, excluded = inbound_route_control.selection_sets(
            inbound_route_control.load(self.state),
            ["101", "102", "103", "104"],
        )

        self.assertEqual(priority, frozenset({"102"}))
        self.assertEqual(excluded, frozenset({"101", "104"}))

    def test_wave_returns_to_normal_after_ordinary_work_is_finished(self) -> None:
        inbound_route_control.set_mode(
            self.state,
            mode="simple-wave",
            updated_at="2026-08-02T16:30:00Z",
            updated_by="Alex",
            pending_event_ids=["101", "102"],
        )
        inbound_route_control.record_route(
            self.state,
            event_id="101",
            route="local-max",
            classified_at="2026-08-02T16:31:00Z",
        )
        inbound_route_control.record_route(
            self.state,
            event_id="102",
            route="short",
            classified_at="2026-08-02T16:31:01Z",
        )

        active = inbound_route_control.reconcile_wave(
            self.state,
            pending_event_ids=["101", "102", "103"],
            updated_at="2026-08-02T16:32:00Z",
        )
        self.assertEqual(active["mode"], "simple-wave")
        self.assertEqual(active["simple_wave_event_ids"], ["102"])

        finished = inbound_route_control.reconcile_wave(
            self.state,
            pending_event_ids=["101", "103"],
            updated_at="2026-08-02T16:33:00Z",
        )
        self.assertEqual(finished["mode"], "normal")
        self.assertNotIn("simple_wave_event_ids", finished)
        priority, excluded = inbound_route_control.selection_sets(
            finished,
            ["101", "103"],
        )
        self.assertEqual(priority, frozenset())
        self.assertEqual(excluded, frozenset())

    def test_route_cannot_be_reclassified_silently(self) -> None:
        inbound_route_control.record_route(
            self.state,
            event_id="101",
            route="short",
            classified_at="2026-08-02T16:31:00Z",
        )

        with self.assertRaisesRegex(ValueError, "different live route"):
            inbound_route_control.record_route(
                self.state,
                event_id="101",
                route="local-max",
                classified_at="2026-08-02T16:32:00Z",
            )


if __name__ == "__main__":
    unittest.main()
