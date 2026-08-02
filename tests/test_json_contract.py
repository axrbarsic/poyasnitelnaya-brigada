from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import (
    autopilot_dispatch,
    json_contract,
    personality_policy,
    watcher_io,
)


class JsonContractTests(unittest.TestCase):
    def test_runtime_modules_share_one_atomic_writer(self) -> None:
        self.assertIs(
            autopilot_dispatch.atomic_write_json,
            json_contract.atomic_write,
        )
        self.assertIs(watcher_io.atomic_write_json, json_contract.atomic_write)
        self.assertIs(
            personality_policy.atomic_write_json,
            json_contract.atomic_write,
        )

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

    def test_atomic_write_replaces_document_without_temp_residue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "nested" / "payload.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"stale": true}', encoding="utf-8")

            json_contract.atomic_write(
                path,
                {"message": "Привет", "nested": {"value": 3}},
            )

            self.assertEqual(
                json_contract.read_object(path),
                {"message": "Привет", "nested": {"value": 3}},
            )
            self.assertTrue(path.read_bytes().endswith(b"\n"))
            self.assertEqual(
                [candidate.name for candidate in path.parent.iterdir()],
                ["payload.json"],
            )


if __name__ == "__main__":
    unittest.main()
