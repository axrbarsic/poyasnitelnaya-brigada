from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import autopilot_dispatch


class AutopilotDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.wake_file = self.root / "var" / "wake-request.json"
        self.state_file = self.root / "var" / "autopilot-dispatch.json"
        self.config_file = self.root / "config.json"
        self.config_file.write_text(
            json.dumps(
                {
                    "wake_file": "var/wake-request.json",
                    "autopilot_state_file": "var/autopilot-dispatch.json",
                }
            ),
            encoding="utf-8",
        )
        self.write_events([])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_events(self, events: list[dict]) -> None:
        self.wake_file.parent.mkdir(parents=True, exist_ok=True)
        self.wake_file.write_text(
            json.dumps(
                {
                    "pending_count": len(events),
                    "events": events,
                }
            ),
            encoding="utf-8",
        )

    def event(self, event_id: str = "2080998938828501439") -> dict:
        return {
            "event_id": event_id,
            "event_url": f"https://x.com/example/status/{event_id}",
            "username": "example",
            "conversation_id": "2080651494051737864",
            "created_at": "2026-07-25T12:49:44Z",
            "first_seen_at": "2026-07-25T12:49:56Z",
            "is_reply": True,
        }

    def test_empty_queue_does_not_dispatch(self) -> None:
        result = autopilot_dispatch.claim(
            self.wake_file,
            self.state_file,
            lease_seconds=1800,
        )
        self.assertFalse(result["dispatch"])
        self.assertEqual(result["pending_count"], 0)

    def test_claim_is_leased_and_not_duplicated(self) -> None:
        self.write_events([self.event()])
        now = datetime(2026, 7, 25, 13, 0, tzinfo=timezone.utc)
        first = autopilot_dispatch.claim(
            self.wake_file,
            self.state_file,
            lease_seconds=1800,
            now=now,
        )
        second = autopilot_dispatch.claim(
            self.wake_file,
            self.state_file,
            lease_seconds=1800,
            now=now + timedelta(minutes=5),
        )
        self.assertTrue(first["dispatch"])
        self.assertEqual(
            [item["id"] for item in first["events"]],
            [self.event()["event_id"]],
        )
        self.assertFalse(second["dispatch"])
        self.assertEqual(second["leased_count"], 1)

    def test_release_allows_immediate_reclaim(self) -> None:
        self.write_events([self.event()])
        first = autopilot_dispatch.claim(
            self.wake_file,
            self.state_file,
            lease_seconds=1800,
        )
        released = autopilot_dispatch.release(
            self.state_file,
            first["claim_token"],
        )
        second = autopilot_dispatch.claim(
            self.wake_file,
            self.state_file,
            lease_seconds=1800,
        )
        self.assertEqual(released["released"], [self.event()["event_id"]])
        self.assertTrue(second["dispatch"])

    def test_finish_removes_completed_claim_state(self) -> None:
        self.write_events([self.event()])
        claimed = autopilot_dispatch.claim(
            self.wake_file,
            self.state_file,
            lease_seconds=1800,
        )

        finished = autopilot_dispatch.finish(
            self.state_file,
            claimed["claim_token"],
        )

        self.assertEqual(finished["finished"], [self.event()["event_id"]])
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["events"], {})

    def test_expired_lease_is_reclaimed(self) -> None:
        self.write_events([self.event()])
        now = datetime(2026, 7, 25, 13, 0, tzinfo=timezone.utc)
        autopilot_dispatch.claim(
            self.wake_file,
            self.state_file,
            lease_seconds=1800,
            now=now,
        )
        result = autopilot_dispatch.claim(
            self.wake_file,
            self.state_file,
            lease_seconds=1800,
            now=now + timedelta(minutes=31),
        )
        self.assertTrue(result["dispatch"])

    def test_resolved_event_is_pruned(self) -> None:
        self.write_events([self.event()])
        autopilot_dispatch.claim(
            self.wake_file,
            self.state_file,
            lease_seconds=1800,
        )
        self.write_events([])
        result = autopilot_dispatch.claim(
            self.wake_file,
            self.state_file,
            lease_seconds=1800,
        )
        state = autopilot_dispatch.read_json(self.state_file)
        self.assertFalse(result["dispatch"])
        self.assertEqual(state["events"], {})

    def test_malformed_wake_fails_without_overwriting_state(self) -> None:
        self.write_events([self.event()])
        autopilot_dispatch.claim(
            self.wake_file,
            self.state_file,
            lease_seconds=1800,
        )
        before = self.state_file.read_text(encoding="utf-8")
        self.wake_file.write_text(
            json.dumps({"pending_count": 2, "events": [self.event()]}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            autopilot_dispatch.claim(
                self.wake_file,
                self.state_file,
                lease_seconds=1800,
            )
        self.assertEqual(before, self.state_file.read_text(encoding="utf-8"))

    def test_wake_event_rejects_noncanonical_or_injected_url(self) -> None:
        event = self.event()
        event["event_url"] = (
            "https://x.com/example/status/"
            f"{event['event_id']}\nIGNORE_PREVIOUS"
        )
        self.write_events([event])

        with self.assertRaisesRegex(ValueError, "numeric id and X URL"):
            autopilot_dispatch.snapshot(self.wake_file)

    def test_config_paths_are_resolved_relative_to_config(self) -> None:
        wake_file, state_file = autopilot_dispatch.load_paths(self.config_file)
        self.assertEqual(wake_file, self.wake_file.resolve())
        self.assertEqual(state_file, self.state_file.resolve())

    def test_snapshot_is_read_only_and_returns_compact_events(self) -> None:
        self.write_events([self.event()])
        result = autopilot_dispatch.snapshot(self.wake_file)
        self.assertEqual(result["pending_count"], 1)
        self.assertEqual(result["pending_ids"], [self.event()["event_id"]])
        self.assertEqual(result["events"][0]["url"], self.event()["event_url"])
        self.assertFalse(self.state_file.exists())
        self.assertFalse(
            self.state_file.with_suffix(
                self.state_file.suffix + ".lock"
            ).exists()
        )


if __name__ == "__main__":
    unittest.main()
