from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill-backup" / "poyasnitelnaya-brigada" / "SKILL.md"
INTERFACE = (
    ROOT
    / "skill-backup"
    / "poyasnitelnaya-brigada"
    / "agents"
    / "openai.yaml"
)
VALIDATOR = (
    ROOT
    / "skill-backup"
    / "x-twitter-operator"
    / "scripts"
    / "validate_reply.py"
)
X_OPERATOR_SKILL = (
    ROOT / "skill-backup" / "x-twitter-operator" / "SKILL.md"
)


class LocalExplainerSkillTests(unittest.TestCase):
    def test_skill_declares_strict_runtime_and_exact_length_contract(
        self,
    ) -> None:
        skill = SKILL.read_text(encoding="utf-8")
        interface = INTERFACE.read_text(encoding="utf-8")

        self.assertIn("gpt-5.6-sol", skill)
        self.assertIn("reasoning effort `max`", skill)
        self.assertIn("3999, 4001", skill)
        self.assertIn("code_points=4000", skill)
        self.assertIn("--exact 4000", skill)
        self.assertIn("Never open ChatGPT or the custom GPT", skill)
        self.assertIn("ровно 4000 Unicode code points", interface)

    def test_x_operator_uses_draftjs_block_value_for_composer_gate(
        self,
    ) -> None:
        skill = X_OPERATOR_SKILL.read_text(encoding="utf-8")

        self.assertIn('data-block="true"', skill)
        self.assertIn("`innerText`", skill)
        self.assertIn("presentation-only extra newline", skill)

    def test_validator_accepts_only_exactly_4000_code_points(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reply = Path(temporary) / "reply.txt"
            for length, expected_returncode in (
                (3999, 1),
                (4000, 0),
                (4001, 1),
            ):
                with self.subTest(length=length):
                    reply.write_text("а" * length, encoding="utf-8")
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(VALIDATOR),
                            "--file",
                            str(reply),
                            "--exact",
                            "4000",
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    payload = json.loads(completed.stdout)

                    self.assertEqual(
                        completed.returncode,
                        expected_returncode,
                    )
                    self.assertEqual(payload["code_points"], length)
                    self.assertEqual(
                        payload["valid"],
                        length == 4000,
                    )


if __name__ == "__main__":
    unittest.main()
