from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import json_contract


class JsonContractTests(unittest.TestCase):
    def test_rejects_duplicate_top_level_key(self) -> None:
        with self.assertRaisesRegex(
            json_contract.DuplicateKeyError,
            "Duplicate JSON key 'mode'",
        ):
            json_contract.loads('{"mode": "a", "mode": "b"}')

    def test_rejects_duplicate_nested_key(self) -> None:
        with self.assertRaisesRegex(
            json_contract.DuplicateKeyError,
            "Duplicate JSON key 'limit'",
        ):
            json_contract.loads(
                '{"runtime": {"limit": 1, "limit": 3}}',
                source="contract",
            )

    def test_reads_valid_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "payload.json"
            path.write_text('{"enabled": true}', encoding="utf-8")

            self.assertEqual(
                json_contract.read_object(path),
                {"enabled": True},
            )


if __name__ == "__main__":
    unittest.main()
