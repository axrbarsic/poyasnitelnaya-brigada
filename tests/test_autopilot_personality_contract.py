from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import autopilot_contract


class AutopilotPersonalityContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "personality").mkdir()
        (self.root / "personality" / "policy.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "policy_id": "test-policy",
                    "default_profile": {
                        "instructions": ["Пиши прямо."]
                    },
                    "topic_profiles": [
                        {
                            "id": "science",
                            "priority": 10,
                            "keywords": ["протон"],
                            "instructions": ["Объясни механизм."],
                        }
                    ],
                    "conversation_profiles": {},
                    "author_profiles": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.config = self.root / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "browser_owner_cwd": ".",
                    "personality_policy_file": "personality/policy.json",
                    "personality_runtime_file": (
                        "var/personality-overrides.json"
                    ),
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_prompt_contains_resolved_policy_for_each_event(self) -> None:
        prompt = autopilot_contract.build_prompt(
            [
                {
                    "id": "123",
                    "text": "Что такое протон?",
                    "conversation_id": "42",
                }
            ],
            config_path=self.config,
            browser_owner_cwd=self.root,
        )

        marker = "PERSONALITY_POLICY_JSON:\n"
        encoded = prompt.split(marker, 1)[1].split("\nEVENTS_JSON:", 1)[0]
        policy = json.loads(encoded)
        event_policy = policy["events"]["123"]
        self.assertEqual(event_policy["matched_topics"], ["science"])
        self.assertEqual(
            event_policy["instructions"],
            ["Пиши прямо.", "Объясни механизм."],
        )
        self.assertIn("Стиль ниже фактов", policy["precedence"])


if __name__ == "__main__":
    unittest.main()
