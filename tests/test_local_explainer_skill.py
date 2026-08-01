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
V2_SKILL = (
    ROOT / "skill-backup" / "poyasnitelnaya-brigada-v2" / "SKILL.md"
)
V2_INTERFACE = (
    ROOT
    / "skill-backup"
    / "poyasnitelnaya-brigada-v2"
    / "agents"
    / "openai.yaml"
)


class LocalExplainerSkillTests(unittest.TestCase):
    def test_skill_declares_strict_runtime_and_maximum_length_contract(
        self,
    ) -> None:
        skill = SKILL.read_text(encoding="utf-8")
        interface = INTERFACE.read_text(encoding="utf-8")

        self.assertIn("gpt-5.6-sol", skill)
        self.assertIn("reasoning effort `max`", skill)
        self.assertIn("не более 4000 Unicode code points", skill)
        self.assertIn("--non-empty --max 4000", skill)
        self.assertNotIn("--exact 4000", skill)
        self.assertNotIn("ровно 4000", skill)
        self.assertIn("Never open ChatGPT or the custom GPT", skill)
        self.assertIn("не превышать 4000 Unicode code points", interface)
        self.assertIn("Не увеличивай текст ради длины", interface)

    def test_x_operator_uses_draftjs_block_value_for_composer_gate(
        self,
    ) -> None:
        skill = X_OPERATOR_SKILL.read_text(encoding="utf-8")

        self.assertIn('data-block="true"', skill)
        self.assertIn("`innerText`", skill)
        self.assertIn("presentation-only extra newline", skill)

    def test_x_operator_adopts_browser_26727_without_using_history_as_state(
        self,
    ) -> None:
        skill = X_OPERATOR_SKILL.read_text(encoding="utf-8")

        self.assertIn("Browser 26.727 or newer", skill)
        self.assertIn("navigation aids only", skill)
        self.assertIn("Never use browsing history as the durable X", skill)
        self.assertIn("Full CDP Developer mode", skill)
        self.assertIn("requires explicit approval", skill)

    def test_v2_is_explicit_and_keeps_v1_available(self) -> None:
        v1 = SKILL.read_text(encoding="utf-8")
        v2 = V2_SKILL.read_text(encoding="utf-8")
        interface = V2_INTERFACE.read_text(encoding="utf-8")

        self.assertIn("name: poyasnitelnaya-brigada", v1)
        self.assertIn("name: poyasnitelnaya-brigada-v2", v2)
        self.assertIn("Use only when Alex explicitly asks", v2)
        self.assertIn("Keep v1 available", v2)
        self.assertIn("$poyasnitelnaya-brigada-v2", interface)

    def test_v2_tracks_thesis_and_verified_contradictions(self) -> None:
        skill = V2_SKILL.read_text(encoding="utf-8")

        for marker in (
            "anchor_claim",
            "anchor_quote",
            "alex_counterclaim",
            "open_question",
            "claim_ledger",
            "concession_ledger",
            "contradiction_ledger",
            "goalpost_ledger",
            "current_move",
        ):
            self.assertIn(marker, skill)
        self.assertIn("Не называть уточнение противоречием", skill)
        self.assertIn("две точные формулировки", skill)
        self.assertIn("Не объявлять молчание признанием", skill)
        self.assertIn("назвать уход и повторить исходный вопрос", skill)

    def test_v2_uses_sol_max_and_maximum_not_target_length(self) -> None:
        skill = V2_SKILL.read_text(encoding="utf-8")

        self.assertIn("gpt-5.6-sol", skill)
        self.assertIn("reasoning effort `max`", skill)
        self.assertIn("Не превышать 4000 Unicode code points", skill)
        self.assertIn("Не стремиться к лимиту", skill)
        self.assertIn("--non-empty --max 4000", skill)
        self.assertNotIn("--exact 4000", skill)
        self.assertNotIn("ровно 4000", skill)

    def test_validator_accepts_non_empty_reply_up_to_4000_code_points(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reply = Path(temporary) / "reply.txt"
            for length, expected_returncode in (
                (0, 1),
                (1, 0),
                (3999, 0),
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
                            "--non-empty",
                            "--max",
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
                        1 <= length <= 4000,
                    )


if __name__ == "__main__":
    unittest.main()
