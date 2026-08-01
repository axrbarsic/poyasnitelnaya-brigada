from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import outbound_cycle


class OutboundCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "outbound-cycle.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_versioned_cron_prompt_uses_full_terminal_protocol(self) -> None:
        root = Path(__file__).resolve().parents[1]
        prompt = (root / "macos/x-15.prompt.txt").read_text(
            encoding="utf-8"
        )

        self.assertIn("gpt-5.6-sol", prompt)
        self.assertIn("reasoning effort max", prompt)
        self.assertIn("target_limit", prompt)
        self.assertIn("scripts/verify_x_note_tweet.py", prompt)
        self.assertIn("scripts/build_outbound_history.py", prompt)
        self.assertIn("history-import", prompt)
        self.assertIn("history-show <TARGET_STATUS_ID>", prompt)
        self.assertIn("--non-empty --max 4000", prompt)
        self.assertNotIn("--exact 4000", prompt)
        self.assertIn("valid=true", prompt)
        for command in (
            "claim",
            "started",
            "renew",
            "completed",
            "failed",
            "defer-slot",
        ):
            self.assertIn(
                f"scripts/outbound_cycle.py --state "
                f"var/outbound-cycle.json --lease-seconds 1800 {command}",
                prompt,
            )
        self.assertIn("--interval-minutes 10", prompt)
        self.assertIn("poyasnitelnaya-brigada-v2", prompt)
        self.assertIn("расист", prompt)
        self.assertIn("Никогда не оставляй claim без terminal", prompt)

    def test_normal_claim_serializes_one_target(self) -> None:
        first = outbound_cycle.claim(self.state, lease_seconds=1800)
        second = outbound_cycle.claim(self.state, lease_seconds=1800)

        self.assertTrue(first["dispatch"])
        self.assertEqual(first["target_limit"], 1)
        self.assertFalse(second["dispatch"])
        self.assertEqual(second["status"], "owner_busy")

    def test_catchup_adjustment_is_idempotent_and_consumed_by_extra(self) -> None:
        arguments = {
            "adjustment_id": "night-20260730",
            "count": 21,
            "reason": "missed scheduled opportunities",
            "window_start": "2026-07-30T04:41:09Z",
            "window_end": "2026-07-30T10:21:43Z",
        }
        added = outbound_cycle.add_catchup(self.state, **arguments)
        repeated = outbound_cycle.add_catchup(self.state, **arguments)
        claimed = outbound_cycle.claim(self.state, lease_seconds=1800)
        outbound_cycle.started(
            self.state,
            claim_token=claimed["claim_token"],
        )
        finished = outbound_cycle.completed(
            self.state,
            claim_token=claimed["claim_token"],
            publications=[
                outbound_cycle.validate_publication(
                    "111=https://x.com/axrbarsic/status/222"
                ),
                outbound_cycle.validate_publication(
                    "333=https://x.com/axrbarsic/status/444"
                ),
            ],
        )

        self.assertEqual(added["catchup_remaining"], 21)
        self.assertEqual(repeated["status"], "already_applied")
        self.assertEqual(claimed["target_limit"], 2)
        self.assertEqual(finished["catchup_consumed"], 1)
        self.assertEqual(finished["catchup_remaining"], 20)

    def test_one_publication_does_not_consume_catchup(self) -> None:
        outbound_cycle.add_catchup(
            self.state,
            adjustment_id="night-20260730",
            count=2,
            reason="missed scheduled opportunities",
            window_start="2026-07-30T04:41:09Z",
            window_end="2026-07-30T10:21:43Z",
        )
        claimed = outbound_cycle.claim(self.state, lease_seconds=1800)
        outbound_cycle.started(
            self.state,
            claim_token=claimed["claim_token"],
        )
        finished = outbound_cycle.completed(
            self.state,
            claim_token=claimed["claim_token"],
            publications=[
                outbound_cycle.validate_publication(
                    "111=https://x.com/axrbarsic/status/222"
                )
            ],
        )

        self.assertEqual(finished["catchup_consumed"], 0)
        self.assertEqual(finished["catchup_remaining"], 2)

    def test_failure_releases_owner_without_consuming_catchup(self) -> None:
        outbound_cycle.add_catchup(
            self.state,
            adjustment_id="night-20260730",
            count=1,
            reason="missed scheduled opportunity",
            window_start="2026-07-30T04:41:09Z",
            window_end="2026-07-30T10:21:43Z",
        )
        claimed = outbound_cycle.claim(self.state, lease_seconds=1800)
        outbound_cycle.started(
            self.state,
            claim_token=claimed["claim_token"],
        )
        failed = outbound_cycle.failed(
            self.state,
            claim_token=claimed["claim_token"],
            reason="no suitable public target",
        )
        next_claim = outbound_cycle.claim(self.state, lease_seconds=1800)

        self.assertEqual(failed["catchup_remaining"], 1)
        self.assertTrue(next_claim["dispatch"])
        self.assertNotEqual(
            next_claim["claim_token"],
            claimed["claim_token"],
        )

    def test_started_slot_defer_adds_catchup_and_releases_owner(self) -> None:
        now = datetime(2026, 8, 1, 6, 41, tzinfo=timezone.utc)
        claimed = outbound_cycle.claim(
            self.state,
            lease_seconds=1800,
            now=now,
        )
        outbound_cycle.started(
            self.state,
            claim_token=claimed["claim_token"],
        )
        deferred = outbound_cycle.defer_slot(
            self.state,
            interval_minutes=10,
            reason="inbound_writer_priority",
            claim_token=claimed["claim_token"],
            now=now,
        )
        state = outbound_cycle.load_state(self.state)

        self.assertEqual(deferred["status"], "deferred")
        self.assertEqual(deferred["catchup_remaining"], 1)
        self.assertIsNone(state["owner"])
        self.assertEqual(state["runs"][-1]["status"], "deferred")

    def test_busy_slot_defer_is_idempotent_and_preserves_owner(self) -> None:
        claimed_at = datetime(2026, 8, 1, 6, 29, tzinfo=timezone.utc)
        now = datetime(2026, 8, 1, 6, 31, tzinfo=timezone.utc)
        claimed = outbound_cycle.claim(
            self.state,
            lease_seconds=1800,
            now=claimed_at,
        )
        first = outbound_cycle.defer_slot(
            self.state,
            interval_minutes=10,
            reason="outbound_owner_busy",
            now=now,
        )
        second = outbound_cycle.defer_slot(
            self.state,
            interval_minutes=10,
            reason="outbound_owner_busy",
            now=now + timedelta(minutes=1),
        )
        state = outbound_cycle.load_state(self.state)

        self.assertEqual(first["catchup_added"], 1)
        self.assertEqual(second["status"], "already_deferred")
        self.assertEqual(state["catchup_remaining"], 1)
        self.assertEqual(
            state["owner"]["claim_token"],
            claimed["claim_token"],
        )

    def test_busy_slot_does_not_duplicate_same_window(self) -> None:
        now = datetime(2026, 8, 1, 6, 41, tzinfo=timezone.utc)
        claimed = outbound_cycle.claim(
            self.state,
            lease_seconds=1800,
            now=now,
        )
        deferred = outbound_cycle.defer_slot(
            self.state,
            interval_minutes=10,
            reason="outbound_owner_busy",
            now=now + timedelta(minutes=1),
        )
        state = outbound_cycle.load_state(self.state)

        self.assertEqual(deferred["status"], "slot_already_claimed")
        self.assertEqual(state["catchup_remaining"], 0)
        self.assertEqual(
            state["owner"]["claim_token"],
            claimed["claim_token"],
        )

    def test_renew_extends_started_owner_lease(self) -> None:
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        claimed = outbound_cycle.claim(
            self.state,
            lease_seconds=60,
            now=now,
        )
        outbound_cycle.started(
            self.state,
            claim_token=claimed["claim_token"],
        )
        renewed = outbound_cycle.renew(
            self.state,
            claim_token=claimed["claim_token"],
            lease_seconds=1800,
            now=now + timedelta(seconds=30),
        )
        overlapping = outbound_cycle.claim(
            self.state,
            lease_seconds=60,
            now=now + timedelta(seconds=90),
        )

        self.assertEqual(
            renewed["expires_at"],
            "2026-07-30T12:30:30Z",
        )
        self.assertFalse(overlapping["dispatch"])

    def test_publication_validation_rejects_noncanonical_or_duplicate_ids(
        self,
    ) -> None:
        invalid_values = (
            "1=https://twitter.com/axrbarsic/status/2",
            "1=https://x.com/axrbarsic/status/2?s=20",
            "1=https://x.com/axrbarsic/status/1",
            f"{'1' * 20}=https://x.com/axrbarsic/status/2",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    outbound_cycle.validate_publication(value)

        outbound_cycle.add_catchup(
            self.state,
            adjustment_id="night-20260730",
            count=1,
            reason="missed scheduled opportunity",
            window_start="2026-07-30T04:41:09Z",
            window_end="2026-07-30T10:21:43Z",
        )
        claimed = outbound_cycle.claim(self.state, lease_seconds=1800)
        outbound_cycle.started(
            self.state,
            claim_token=claimed["claim_token"],
        )
        with self.assertRaises(ValueError):
            outbound_cycle.completed(
                self.state,
                claim_token=claimed["claim_token"],
                publications=[
                    outbound_cycle.validate_publication(
                        "111=https://x.com/axrbarsic/status/222"
                    ),
                    outbound_cycle.validate_publication(
                        "333=https://x.com/axrbarsic/status/222"
                    ),
                ],
            )

    def test_expired_owner_is_reclaimed(self) -> None:
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        first = outbound_cycle.claim(
            self.state,
            lease_seconds=60,
            now=now,
        )
        second = outbound_cycle.claim(
            self.state,
            lease_seconds=60,
            now=now + timedelta(seconds=61),
        )
        state = outbound_cycle.load_state(self.state)

        self.assertTrue(first["dispatch"])
        self.assertTrue(second["dispatch"])
        self.assertEqual(state["runs"][-1]["status"], "expired")


if __name__ == "__main__":
    unittest.main()
