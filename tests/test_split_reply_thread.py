from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "skill-backup"
    / "x-twitter-operator"
    / "scripts"
    / "split_reply_thread.py"
)
SPEC = importlib.util.spec_from_file_location("split_reply_thread", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
split_reply_thread = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(split_reply_thread)


class SplitReplyThreadTests(unittest.TestCase):
    def test_short_payload_stays_single_and_exact(self) -> None:
        payload = "Короткий ответ\nсо второй строкой."

        parts, removed = split_reply_thread.split_payload(payload, 4000)

        self.assertEqual(parts, [payload])
        self.assertEqual(removed, 0)

    def test_overlength_payload_prefers_paragraph_boundary(self) -> None:
        payload = ("А" * 2500) + "\n\n" + ("Б" * 1800)

        parts, removed = split_reply_thread.split_payload(payload, 4000)

        self.assertEqual(parts, ["А" * 2500, "Б" * 1800])
        self.assertEqual(removed, 2)
        self.assertTrue(all(1 <= len(part) <= 4000 for part in parts))

    def test_sentence_boundary_is_used_without_changing_words(self) -> None:
        first = ("слово " * 665).rstrip() + "."
        second = " Следующее предложение."
        payload = first + second

        parts, removed = split_reply_thread.split_payload(payload, 4000)

        self.assertEqual(parts, [first, second.lstrip()])
        self.assertEqual(removed, 1)
        self.assertEqual(
            "".join(payload.split()),
            "".join("".join(parts).split()),
        )

    def test_hard_split_handles_one_oversized_token(self) -> None:
        payload = "x" * 8001

        parts, removed = split_reply_thread.split_payload(payload, 4000)

        self.assertEqual([len(part) for part in parts], [4000, 4000, 1])
        self.assertEqual("".join(parts), payload)
        self.assertEqual(removed, 0)

    def test_forbidden_character_fails_before_split(self) -> None:
        with self.assertRaisesRegex(ValueError, "U\\+2014"):
            split_reply_thread.split_payload("текст\u2014текст", 4000)

    def test_cli_writes_exact_parts_and_manifest(self) -> None:
        payload = ("А" * 3990) + ". " + ("Б" * 100)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            output = root / "parts"
            source.write_text(payload + "\n", encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--file",
                    str(source),
                    "--strip-one-final-newline",
                    "--max",
                    "4000",
                    "--output-dir",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            result = json.loads(completed.stdout)
            written = [
                Path(record["path"]).read_text(encoding="utf-8")
                for record in result["parts"]
            ]
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(result["part_count"], 2)
            self.assertEqual(written, [("А" * 3990) + ".", "Б" * 100])
            self.assertTrue(all(len(part) <= 4000 for part in written))


if __name__ == "__main__":
    unittest.main()
