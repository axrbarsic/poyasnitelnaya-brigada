from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts import autopilot_bridge


class AutopilotBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.owner = self.root / "owner"
        self.owner.mkdir()
        self.config = self.root / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "wake_file": "var/wake-request.json",
                    "autopilot_state_file": "var/autopilot-dispatch.json",
                    "autopilot_health_file": "var/autopilot-health.json",
                    "database": "var/watcher.sqlite3",
                    "browser_owner_cwd": str(self.owner),
                }
            ),
            encoding="utf-8",
        )
        self.wake = self.root / "var" / "wake-request.json"
        self.write_events([])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_events(self, events: list[dict]) -> None:
        self.wake.parent.mkdir(parents=True, exist_ok=True)
        self.wake.write_text(
            json.dumps({"pending_count": len(events), "events": events}),
            encoding="utf-8",
        )

    def event(self) -> dict:
        return {
            "event_id": "2081050838240211434",
            "event_url":
                "https://x.com/Timyr316661/status/2081050838240211434",
            "username": "Timyr316661",
            "conversation_id": "2080312847230210375",
            "created_at": "2026-07-25T16:15:58Z",
            "first_seen_at": "2026-07-25T16:19:00Z",
            "is_reply": True,
        }

    def test_empty_queue_is_token_free_idle(self) -> None:
        result = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertFalse(result["dispatch"])
        self.assertEqual(result["status"], "idle")
        health = json.loads(
            (self.root / "var" / "autopilot-health.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(health["status"], "idle")

    def test_claim_returns_exact_desktop_handoff(self) -> None:
        self.write_events([self.event()])

        result = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertTrue(result["dispatch"])
        self.assertIn(self.event()["event_id"], result["prompt"])
        self.assertIn("tracked_conversation_reply=true", result["prompt"])
        self.assertIn(
            "Следующий X API poll выполняет LaunchAgent",
            result["prompt"],
        )
        self.assertNotIn("сделай один свежий poll", result["prompt"])
        self.assertNotIn("\u2013", result["prompt"])
        self.assertNotIn("\u2014", result["prompt"])

        self.write_events([])
        completed = autopilot_bridge.mark_completed(
            self.config,
            result["claim_token"],
        )
        self.assertEqual(completed["status"], "completed")
        health = json.loads(
            (self.root / "var" / "autopilot-health.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(health["handoff_count"], 1)

    def test_overlapping_claim_preserves_work_in_progress_health(self) -> None:
        self.write_events([self.event()])
        first = autopilot_bridge.claim(self.config, lease_seconds=1800)
        started = autopilot_bridge.mark_started(
            self.config,
            first["claim_token"],
        )

        second = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertEqual(started["status"], "work_in_progress")
        self.assertFalse(second["dispatch"])
        health = json.loads(
            (self.root / "var" / "autopilot-health.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(health["status"], "work_in_progress")
        self.assertEqual(health["event_ids"], [self.event()["event_id"]])
        self.assertEqual(health["claim_token"], first["claim_token"])

    def test_completed_with_warning_is_success_after_queue_resolves(self) -> None:
        self.write_events([self.event()])
        first = autopilot_bridge.claim(self.config, lease_seconds=1800)
        autopilot_bridge.mark_started(self.config, first["claim_token"])
        self.write_events([])

        result = autopilot_bridge.mark_completed(
            self.config,
            first["claim_token"],
            warning="postflight poll unavailable",
        )

        self.assertEqual(result["status"], "completed_with_warning")
        health = json.loads(
            (self.root / "var" / "autopilot-health.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(health["handoff_count"], 1)
        self.assertEqual(health["error"], "postflight poll unavailable")

    def test_reconcile_completed_requires_durable_resolution(self) -> None:
        database = self.root / "var" / "watcher.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE event_resolutions(
                    event_id TEXT PRIMARY KEY,
                    disposition TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO event_resolutions VALUES(?, 'skip')",
                (self.event()["event_id"],),
            )

        result = autopilot_bridge.reconcile_completed(
            self.config,
            [self.event()["event_id"]],
            warning="recovered postflight state",
        )

        self.assertEqual(result["status"], "completed_with_warning")
        self.assertEqual(result["event_ids"], [self.event()["event_id"]])

    def test_failed_handoff_releases_claim_for_immediate_retry(self) -> None:
        self.write_events([self.event()])
        first = autopilot_bridge.claim(self.config, lease_seconds=1800)

        failed = autopilot_bridge.mark_failed(
            self.config,
            first["claim_token"],
            "synthetic send failure",
        )
        second = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertEqual(failed["released"], [self.event()["event_id"]])
        self.assertTrue(second["dispatch"])


if __name__ == "__main__":
    unittest.main()
