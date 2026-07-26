from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import evidence_import
import xmention_watcher as watcher


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

    def runtime_evidence(
        self,
        *,
        resolved: bool = True,
        source_sha_matches: bool = True,
        source_count_includes_terminal_lf: bool = False,
    ) -> tuple[Path, sqlite3.Connection]:
        destination = self.output / "autopilot-20260726T000000Z"
        destination.mkdir(parents=True)
        event_id = "7000000000000000001"
        reply_id = "7000000000000000002"
        reply_url = f"https://x.com/axrbarsic/status/{reply_id}"
        reply_text = "Точный опубликованный ответ"
        reply_path = destination / "reply.txt"
        reply_path.write_text(reply_text + "\n", encoding="utf-8")
        source_sha = evidence_import.sha256(reply_path)
        if not source_sha_matches:
            source_sha = "0" * 64
        evidence = ["live exact target", "verified exact child"]
        ledger = [
            {
                "event": "publication_verified",
                "event_id": event_id,
                "reply_url": reply_url,
            },
            {
                "event": "initial_audit_disposition",
                "event_id": event_id,
                "disposition": "published",
                "reason": "Exact factual reply was published once.",
                "reply_url": reply_url,
                "stance": "opposing",
                "stance_detail": "opposing_bounded_claim",
                "confidence": "high",
                "media_meaning": None,
                "evidence": evidence,
                "source_path": str(reply_path),
                "source_sha256": source_sha,
                "source_code_points": (
                    len(reply_text) + 1
                    if source_count_includes_terminal_lf
                    else len(reply_text)
                ),
            },
        ]
        (destination / "run-ledger.jsonl").write_text(
            "".join(
                json.dumps(record, ensure_ascii=False) + "\n"
                for record in ledger
            ),
            encoding="utf-8",
        )
        (destination / "conversation-history.jsonl").write_text(
            json.dumps(
                {
                    "event_id": event_id,
                    "reply_status_id": reply_id,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        connection = watcher.connect_database(self.root / "watcher.sqlite3")
        now = watcher.isoformat()
        with connection:
            connection.execute(
                """
                INSERT INTO events(
                    event_id, author_id, username, created_at,
                    conversation_id, in_reply_to_user_id, is_reply,
                    payload_json, first_seen_at, delivery_state
                ) VALUES(?, '901', 'target_user', ?, ?, '900', 1, '{}', ?,
                         'queued')
                """,
                (event_id, now, event_id, now),
            )
            connection.execute(
                """
                INSERT INTO conversation_chains(
                    chain_id, root_status_id, provenance, created_at, updated_at
                ) VALUES(?, ?, 'short', ?, ?)
                """,
                (event_id, event_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO conversation_turns(
                    status_id, chain_id, parent_status_id, actor, author, url,
                    exact_text, observed_at, provenance
                ) VALUES(?, ?, NULL, 'user', '@target_user', ?, 'Target', ?,
                         'live_x_dom')
                """,
                (
                    event_id,
                    event_id,
                    f"https://x.com/target_user/status/{event_id}",
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO conversation_turns(
                    status_id, chain_id, parent_status_id, actor, author, url,
                    exact_text, observed_at, provenance
                ) VALUES(?, ?, ?, 'alex', '@axrbarsic', ?, ?, ?,
                         'self_authored_short_sol')
                """,
                (
                    reply_id,
                    event_id,
                    event_id,
                    reply_url,
                    reply_text,
                    now,
                ),
            )
            if resolved:
                connection.execute(
                    """
                    INSERT INTO event_resolutions(
                        event_id, disposition, reason, reply_url, stance,
                        stance_detail, confidence, media_meaning, evidence_json,
                        resolved_at
                    ) VALUES(?, 'published', ?, ?, 'opposing',
                             'opposing_bounded_claim', 'high', NULL, ?, ?)
                    """,
                    (
                        event_id,
                        "Exact factual reply was published once.",
                        reply_url,
                        json.dumps(sorted(evidence), ensure_ascii=False),
                        now,
                    ),
                )
        return destination, connection

    def test_runtime_finalization_requires_durable_resolution_and_is_idempotent(
        self,
    ) -> None:
        destination, connection = self.runtime_evidence()
        try:
            first = evidence_import.finalize_runtime_evidence(
                destination=destination,
                connection=connection,
                output_root=self.output,
            )
            second = evidence_import.finalize_runtime_evidence(
                destination=destination,
                connection=connection,
                output_root=self.output,
            )
        finally:
            connection.close()

        manifest = json.loads(
            (destination / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(first["status"], "finalized")
        self.assertEqual(first["resolution_count"], 1)
        self.assertEqual(second["status"], "already_finalized")
        self.assertTrue(evidence_import.audit_evidence(destination)["complete"])
        self.assertTrue(
            manifest["resolution_proof"][0]["alex_turn_present"]
        )

    def test_runtime_finalization_rejects_unresolved_event(self) -> None:
        destination, connection = self.runtime_evidence(resolved=False)
        try:
            with self.assertRaisesRegex(ValueError, "not durably resolved"):
                evidence_import.finalize_runtime_evidence(
                    destination=destination,
                    connection=connection,
                    output_root=self.output,
                )
        finally:
            connection.close()
        self.assertFalse((destination / "manifest.json").exists())

    def test_runtime_finalization_rejects_source_sha_mismatch(self) -> None:
        destination, connection = self.runtime_evidence(
            source_sha_matches=False,
        )
        try:
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                evidence_import.finalize_runtime_evidence(
                    destination=destination,
                    connection=connection,
                    output_root=self.output,
                )
        finally:
            connection.close()
        self.assertFalse((destination / "manifest.json").exists())

    def test_runtime_finalization_rejects_blocker_code_mismatch(self) -> None:
        destination, connection = self.runtime_evidence()
        try:
            with connection:
                connection.execute(
                    """
                    UPDATE event_resolutions
                    SET blocker_code = 'target_unavailable'
                    """
                )
            with self.assertRaisesRegex(
                ValueError,
                "resolution mismatch",
            ):
                evidence_import.finalize_runtime_evidence(
                    destination=destination,
                    connection=connection,
                    output_root=self.output,
                )
        finally:
            connection.close()
        self.assertFalse((destination / "manifest.json").exists())

    def test_runtime_finalization_accepts_recorded_terminal_lf_length(
        self,
    ) -> None:
        destination, connection = self.runtime_evidence(
            source_count_includes_terminal_lf=True,
        )
        try:
            result = evidence_import.finalize_runtime_evidence(
                destination=destination,
                connection=connection,
                output_root=self.output,
            )
        finally:
            connection.close()
        self.assertEqual(result["status"], "finalized")
