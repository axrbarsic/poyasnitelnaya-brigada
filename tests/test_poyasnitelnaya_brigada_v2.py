from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = (
    ROOT / "skill-backup" / "poyasnitelnaya-brigada-v2" / "SKILL.md"
)
CASES = ROOT / "tests" / "fixtures" / "poyasnitelnaya_brigada_v2_cases.json"


class PoyasnitelnayaBrigadaV2EvalContractTests(unittest.TestCase):
    def test_v2_is_the_default_and_v1_is_an_explicit_fallback(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")

        self.assertIn("Use by default for every local-max", skill)
        self.assertIn("use it only when Alex explicitly requests v1", skill)
        self.assertIn("Уже начатую транзакцию", skill)

    def test_eval_cases_cover_required_dialogue_moves(self) -> None:
        payload = json.loads(CASES.read_text(encoding="utf-8"))
        cases = payload["cases"]

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(
            {case["expected_class"] for case in cases},
            {
                "topic_diversion",
                "qualification",
                "contradiction",
                "goalpost_shift",
                "loaded_binary_frame",
                "new_evidence",
                "self_awarded_victory",
            },
        )
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertGreaterEqual(len(case["history"]), 3)
                self.assertTrue(case["must_do"])
                self.assertTrue(case["must_not"])
                status_ids = [turn["status_id"] for turn in case["history"]]
                self.assertEqual(len(status_ids), len(set(status_ids)))

    def test_eval_data_and_skill_have_no_forbidden_dashes(self) -> None:
        for path in (SKILL, CASES):
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("\u2013", text)
                self.assertNotIn("\u2014", text)

    def test_skill_contains_guards_required_by_eval_cases(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")

        for required in (
            "При уходе в сторону",
            "Не называть уточнение противоречием",
            "обе короткие точные формулировки",
            "При смене критерия",
            "При новом доказательстве",
            "neutral_question",
            "burden_ledger",
            "inference_bridge",
            "самоприсуждённая победа",
            "author_dossier",
        ):
            self.assertIn(required, skill)


if __name__ == "__main__":
    unittest.main()
