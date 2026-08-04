from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill-backup" / "377" / "SKILL.md"
INTERFACE = ROOT / "skill-backup" / "377" / "agents" / "openai.yaml"


class Visual377SkillTests(unittest.TestCase):
    def test_skill_preserves_the_single_image_contract(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")

        self.assertIn('name: "377"', skill)
        self.assertIn("ОДНО ФОТОРЕАЛИСТИЧНОЕ", skill)
        self.assertIn("9:16", skill)
        self.assertIn("максимум из 3 предложений", skill)
        self.assertIn("70 слов", skill)
        self.assertIn("ровно одно изображение", skill)
        self.assertIn("Без объяснений, анализа и отчёта", skill)

    def test_skill_is_explicit_only_and_has_no_forbidden_dashes(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")
        interface = INTERFACE.read_text(encoding="utf-8")

        self.assertIn("Use only when Alex explicitly asks", skill)
        self.assertIn("$377", interface)
        self.assertIn("allow_implicit_invocation: false", interface)
        for text in (skill, interface):
            self.assertNotIn("\u2013", text)
            self.assertNotIn("\u2014", text)


if __name__ == "__main__":
    unittest.main()
