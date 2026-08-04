from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import personality_policy


class PersonalityPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.policy = self.root / "policy.json"
        self.runtime = self.root / "runtime.json"
        self.policy.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "policy_id": "test",
                    "default_profile": {
                        "instructions": ["Говори прямо."]
                    },
                    "topic_profiles": [
                        {
                            "id": "space",
                            "priority": 10,
                            "keywords": ["протон", "космос"],
                            "instructions": ["Объясняй механизм."],
                        }
                    ],
                    "conversation_profiles": {
                        "conversation-1": {
                            "instructions": ["Добавь сухой юмор."]
                        }
                    },
                    "author_profiles": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_resolves_default_topic_and_conversation(self) -> None:
        resolved = personality_policy.resolve_event_policy(
            {
                "text": "Из чего состоит протон?",
                "conversation_id": "conversation-1",
                "username": "reader",
            },
            policy_path=self.policy,
            runtime_path=self.runtime,
        )

        self.assertEqual(resolved["matched_topics"], ["space"])
        self.assertEqual(
            resolved["instructions"],
            [
                "Говори прямо.",
                "Объясняй механизм.",
                "Добавь сухой юмор.",
            ],
        )

    def test_runtime_override_applies_immediately_to_topic(self) -> None:
        override = personality_policy.add_override(
            self.runtime,
            scope="topic",
            selector="space",
            instruction="Будь дерзче, но точнее.",
        )

        resolved = personality_policy.resolve_event_policy(
            {"text": "Поговорим про космос"},
            policy_path=self.policy,
            runtime_path=self.runtime,
        )

        self.assertIn(
            "Будь дерзче, но точнее.",
            resolved["instructions"],
        )
        self.assertEqual(
            resolved["applied_runtime_overrides"],
            [override["id"]],
        )
        self.assertEqual(resolved["policy_revision"], 1)

    def test_conversation_override_does_not_leak_to_another_branch(self) -> None:
        personality_policy.add_override(
            self.runtime,
            scope="conversation",
            selector="conversation-1",
            instruction="Отвечай короче.",
        )

        first = personality_policy.resolve_event_policy(
            {"conversation_id": "conversation-1"},
            policy_path=self.policy,
            runtime_path=self.runtime,
        )
        second = personality_policy.resolve_event_policy(
            {"conversation_id": "conversation-2"},
            policy_path=self.policy,
            runtime_path=self.runtime,
        )

        self.assertIn("Отвечай короче.", first["instructions"])
        self.assertNotIn("Отвечай короче.", second["instructions"])

    def test_conversation_override_can_be_limited_to_one_author(self) -> None:
        override = personality_policy.add_override(
            self.runtime,
            scope="conversation",
            selector="conversation-1",
            author_selector="@target_author",
            instruction="Отвечай едко.",
        )

        target = personality_policy.resolve_event_policy(
            {
                "conversation_id": "conversation-1",
                "username": "target_author",
            },
            policy_path=self.policy,
            runtime_path=self.runtime,
        )
        neighbour = personality_policy.resolve_event_policy(
            {
                "conversation_id": "conversation-1",
                "username": "other_author",
            },
            policy_path=self.policy,
            runtime_path=self.runtime,
        )
        other_branch = personality_policy.resolve_event_policy(
            {
                "conversation_id": "conversation-2",
                "username": "target_author",
            },
            policy_path=self.policy,
            runtime_path=self.runtime,
        )

        self.assertEqual(override["author_selector"], "target_author")
        self.assertIn("Отвечай едко.", target["instructions"])
        self.assertNotIn("Отвечай едко.", neighbour["instructions"])
        self.assertNotIn("Отвечай едко.", other_branch["instructions"])

    def test_disable_stops_runtime_override(self) -> None:
        override = personality_policy.add_override(
            self.runtime,
            scope="global",
            selector="",
            instruction="Будь мягче.",
        )
        personality_policy.disable_override(self.runtime, override["id"])

        resolved = personality_policy.resolve_event_policy(
            {},
            policy_path=self.policy,
            runtime_path=self.runtime,
        )

        self.assertNotIn("Будь мягче.", resolved["instructions"])

    def test_forbidden_dash_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "forbidden dash"):
            personality_policy.add_override(
                self.runtime,
                scope="global",
                selector="",
                instruction="Будь точным \u2014 всегда.",
            )


if __name__ == "__main__":
    unittest.main()
