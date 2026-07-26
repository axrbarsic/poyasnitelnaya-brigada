from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import evidence_import


class EvidenceImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "legacy-work"
        self.source.mkdir()
        (self.source / "run-ledger.jsonl").write_text(
            '{"event_id":"123"}\n',
            encoding="utf-8",
        )
        screenshots = self.source / "screenshots"
        screenshots.mkdir()
        (screenshots / "target.png").write_bytes(b"\x89PNG\r\nlegacy")
        self.output = self.root / "var/evidence/browser-owner"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_import_is_manifested_verified_and_idempotent(self) -> None:
        before = evidence_import.build_file_manifest(
            self.source,
            evidence_import.collect_source_files(self.source),
        )

        first = evidence_import.import_evidence(
            source=self.source,
            output_root=self.output,
            label="legacy-2026-07-24",
        )
        second = evidence_import.import_evidence(
            source=self.source,
            output_root=self.output,
            label="legacy-2026-07-24",
        )

        destination = Path(first["destination"])
        manifest = json.loads(
            (destination / "manifest.json").read_text(encoding="utf-8")
        )
        after = evidence_import.build_file_manifest(
            self.source,
            evidence_import.collect_source_files(self.source),
        )
        copied_files = [
            destination / record["path"]
            for record in manifest["files"]
        ]
        copied = evidence_import.build_file_manifest(
            destination,
            copied_files,
        )

        self.assertEqual(first["status"], "imported")
        self.assertEqual(second["status"], "already_imported")
        self.assertEqual(before, after)
        self.assertEqual(copied, before)
        self.assertEqual(manifest["source_fingerprint"], before["source_fingerprint"])
        self.assertTrue(evidence_import.audit_evidence(destination)["complete"])

    def test_existing_destination_with_changed_source_fails_closed(self) -> None:
        evidence_import.import_evidence(
            source=self.source,
            output_root=self.output,
            label="legacy",
        )
        (self.source / "run-ledger.jsonl").write_text(
            '{"event_id":"changed"}\n',
            encoding="utf-8",
        )

        with self.assertRaises(FileExistsError):
            evidence_import.import_evidence(
                source=self.source,
                output_root=self.output,
                label="legacy",
            )

        destination = self.output / "legacy"
        (destination / "run-ledger.jsonl").write_text(
            '{"event_id":"corrupted"}\n',
            encoding="utf-8",
        )
        audit = evidence_import.audit_evidence(destination)
        self.assertFalse(audit["complete"])
        self.assertIn("file_manifest_mismatch", audit["errors"])

    def test_rejects_sensitive_filename_and_content(self) -> None:
        (self.source / "config.json").write_text(
            '{"safe":true}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Sensitive filename"):
            evidence_import.collect_source_files(self.source)

        (self.source / "config.json").unlink()
        (self.source / "notes.txt").write_text(
            "api_key=DO_NOT_COPY_THIS_SECRET",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Potential secret"):
            evidence_import.collect_source_files(self.source)

        (self.source / "notes.txt").unlink()
        (self.source / ".env.local").write_text(
            "SAFE_LOOKING=value\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Sensitive filename"):
            evidence_import.collect_source_files(self.source)

        (self.source / ".env.local").unlink()
        (self.source / "certificate.pem").write_text(
            "SAFE_LOOKING=value\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "private-key container"):
            evidence_import.collect_source_files(self.source)

        (self.source / "certificate.pem").unlink()
        (self.source / "export.csv").write_text(
            "header,value\n"
            "authorization,Authorization: Bearer abcdefghijklmnop\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Potential secret"):
            evidence_import.collect_source_files(self.source)

    def test_cli_output_is_fixed_to_canonical_evidence_root(self) -> None:
        expected = (
            Path(evidence_import.__file__).resolve().parent
            / "var/evidence/browser-owner"
        )
        self.assertEqual(evidence_import.CANONICAL_OUTPUT_ROOT, expected)

    def test_rejects_symlink(self) -> None:
        link = self.source / "linked.txt"
        try:
            link.symlink_to(self.source / "run-ledger.jsonl")
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(ValueError, "Symlinks are not allowed"):
            evidence_import.collect_source_files(self.source)
