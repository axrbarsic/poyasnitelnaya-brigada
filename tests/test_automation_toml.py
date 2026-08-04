from __future__ import annotations

import json
import unittest

from scripts import automation_toml


class AutomationTomlTests(unittest.TestCase):
    def test_parses_generated_flat_automation(self) -> None:
        prompt = 'kind = "cron"\nstatus = "PAUSED"'
        payload = automation_toml.loads(
            "\n".join(
                [
                    "version = 1",
                    'id = "x-relay"',
                    'kind = "heartbeat"',
                    f"prompt = {json.dumps(prompt, ensure_ascii=False)}",
                    'status = "ACTIVE"',
                    'cwds = ["/tmp/project"]',
                    'target = { type = "project", project_id = "abc" }',
                    "enabled = true",
                ]
            )
        )

        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["kind"], "heartbeat")
        self.assertEqual(payload["prompt"], prompt)
        self.assertEqual(payload["status"], "ACTIVE")
        self.assertEqual(payload["cwds"], ["/tmp/project"])
        self.assertEqual(
            payload["target"],
            {"type": "project", "project_id": "abc"},
        )
        self.assertTrue(payload["enabled"])

    def test_rejects_duplicate_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate TOML key"):
            automation_toml.loads('id = "one"\nid = "two"\n')

    def test_rejects_nested_or_multiline_constructs(self) -> None:
        with self.assertRaisesRegex(ValueError, "flat assignment"):
            automation_toml.loads("[nested]\nid = 'one'\n")


if __name__ == "__main__":
    unittest.main()
