from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import build_outbound_history


class BuildOutboundHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.target_file = self.root / "target.txt"
        self.reply_file = self.root / "reply.txt"
        self.evidence_file = self.root / "evidence.json"
        self.output_file = self.root / "conversation-history.jsonl"
        self.target_file.write_text("Точный target\n", encoding="utf-8")
        self.reply_file.write_text("р" * 4000 + "\n", encoding="utf-8")
        self.evidence_file.write_text(
            json.dumps(
                {
                    "target": {
                        "status_id": "111",
                        "url": "https://x.com/source/status/111",
                        "author_handle": "source",
                        "exact_text_file": "target.txt",
                        "posted_at": "2026-07-31T05:00:00Z",
                        "parent_status_id": None,
                        "media": [{"type": "video"}],
                    },
                    "reply": {
                        "file": "reply.txt",
                        "status_id": "222",
                        "parent_status_id": "111",
                        "url": "https://x.com/axrbarsic/status/222",
                        "author_handle": "axrbarsic",
                        "posted_at": "2026-07-31T05:01:00Z",
                    },
                    "sources": ["https://example.com/source"],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_builds_exact_two_turn_snapshot(self) -> None:
        record = build_outbound_history.build_record(self.evidence_file)

        self.assertEqual(record["chain_id"], "111")
        self.assertEqual(record["turns"][0]["exact_text"], "Точный target")
        self.assertEqual(len(record["turns"][1]["exact_text"]), 4000)
        self.assertEqual(
            record["turns"][1]["source_urls"],
            ["https://example.com/source"],
        )
        self.assertEqual(record["turns"][0]["media"], [{"type": "video"}])

    def test_can_append_reply_to_existing_chain(self) -> None:
        evidence = json.loads(self.evidence_file.read_text(encoding="utf-8"))
        evidence["target"]["chain_id"] = "999"
        evidence["target"]["root_status_id"] = "999"
        evidence["target"]["chain_provenance"] = "short"
        evidence["target"]["parent_status_id"] = "888"
        evidence["target"]["provenance"] = "official_api"
        evidence["reply"]["provenance"] = "self_authored_short"
        self.evidence_file.write_text(
            json.dumps(evidence),
            encoding="utf-8",
        )

        record = build_outbound_history.build_record(self.evidence_file)

        self.assertEqual(record["chain_id"], "999")
        self.assertEqual(record["root_status_id"], "999")
        self.assertEqual(record["provenance"], "short")
        self.assertEqual(record["turns"][0]["status_id"], "111")
        self.assertEqual(record["turns"][0]["parent_status_id"], "888")
        self.assertEqual(record["turns"][0]["provenance"], "official_api")
        self.assertEqual(record["turns"][1]["provenance"], "self_authored_short")
        self.assertNotIn("ledger_reference", record)

    def test_builds_target_only_snapshot_for_blocked_event(self) -> None:
        evidence = json.loads(self.evidence_file.read_text(encoding="utf-8"))
        evidence.pop("reply")
        evidence["target"]["chain_id"] = "999"
        evidence["target"]["root_status_id"] = "999"
        evidence["chain_provenance"] = "short"
        self.evidence_file.write_text(
            json.dumps(evidence),
            encoding="utf-8",
        )

        record = build_outbound_history.build_record(self.evidence_file)

        self.assertEqual(record["chain_id"], "999")
        self.assertEqual(record["root_status_id"], "999")
        self.assertEqual(record["provenance"], "short")
        self.assertEqual(len(record["turns"]), 1)
        self.assertEqual(record["turns"][0]["status_id"], "111")

    def test_write_is_idempotent_and_conflict_safe(self) -> None:
        record = build_outbound_history.build_record(self.evidence_file)

        first = build_outbound_history.write_record(
            self.output_file,
            record,
        )
        second = build_outbound_history.write_record(
            self.output_file,
            record,
        )
        record["turns"][1]["exact_text"] = "другой"

        self.assertEqual(first, "created")
        self.assertEqual(second, "unchanged")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            build_outbound_history.write_record(
                self.output_file,
                record,
            )

    def test_rejects_wrong_parent_empty_or_oversized_reply(self) -> None:
        evidence = json.loads(self.evidence_file.read_text(encoding="utf-8"))
        evidence["reply"]["parent_status_id"] = "333"
        self.evidence_file.write_text(
            json.dumps(evidence),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "does not match target"):
            build_outbound_history.build_record(self.evidence_file)

        evidence["reply"]["parent_status_id"] = "111"
        self.evidence_file.write_text(
            json.dumps(evidence),
            encoding="utf-8",
        )
        self.reply_file.write_text("коротко\n", encoding="utf-8")
        record = build_outbound_history.build_record(self.evidence_file)
        self.assertEqual(record["turns"][1]["exact_text"], "коротко")

        self.reply_file.write_text("р" * 4001 + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "at most 4000"):
            build_outbound_history.build_record(self.evidence_file)

        self.reply_file.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            build_outbound_history.build_record(self.evidence_file)

    def test_rejects_noncanonical_target_url_and_invalid_target_parent(
        self,
    ) -> None:
        evidence = json.loads(self.evidence_file.read_text(encoding="utf-8"))
        evidence["target"]["url"] = "https://x.com/other/status/111"
        self.evidence_file.write_text(
            json.dumps(evidence),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "target URL"):
            build_outbound_history.build_record(self.evidence_file)

        evidence["target"]["url"] = "https://x.com/source/status/111"
        evidence["target"]["parent_status_id"] = "not-numeric"
        self.evidence_file.write_text(
            json.dumps(evidence),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "target.parent_status_id"):
            build_outbound_history.build_record(self.evidence_file)


if __name__ == "__main__":
    unittest.main()
