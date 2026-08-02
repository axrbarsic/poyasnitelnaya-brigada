from __future__ import annotations

import unittest

from scripts import inbound_policy


class InboundPolicyTests(unittest.TestCase):
    def test_defaults_to_one_event_and_one_tab(self) -> None:
        policy = inbound_policy.InboundPolicy.from_config({})

        self.assertEqual(policy.claim_events, 1)
        self.assertEqual(policy.parallel_read_tabs, 1)

    def test_accepts_supported_three_event_batch(self) -> None:
        policy = inbound_policy.InboundPolicy.from_config(
            {
                "autopilot_max_claim_events": 3,
                "autopilot_max_parallel_read_tabs": 3,
            }
        )

        self.assertEqual(policy.claim_events, 3)
        self.assertEqual(policy.parallel_read_tabs, 3)

    def test_rejects_more_tabs_than_claimed_events(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            inbound_policy.InboundPolicy.from_config(
                {
                    "autopilot_max_claim_events": 1,
                    "autopilot_max_parallel_read_tabs": 2,
                }
            )

    def test_rejects_unsupported_batch_size(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1 and 3"):
            inbound_policy.InboundPolicy.from_config(
                {"autopilot_max_claim_events": 4}
            )

    def test_rejects_coercible_non_integer_limits(self) -> None:
        for value in (True, 1.0, "1"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "must be an integer"):
                    inbound_policy.InboundPolicy.from_config(
                        {"autopilot_max_claim_events": value}
                    )

    def test_resource_mode_caps_read_tabs(self) -> None:
        policy = inbound_policy.InboundPolicy(3, 3)

        self.assertEqual(
            policy.effective_read_tabs(3, resource_mode="efficiency"),
            1,
        )
        self.assertEqual(
            policy.effective_read_tabs(3, resource_mode="balanced"),
            2,
        )
        self.assertEqual(
            policy.effective_read_tabs(3, resource_mode="performance"),
            3,
        )


if __name__ == "__main__":
    unittest.main()
