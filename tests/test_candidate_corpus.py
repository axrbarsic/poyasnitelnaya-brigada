from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import candidate_corpus
import xmention_watcher as watcher


class CandidateCorpusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        config_path = self.root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "user_id": "900",
                    "database": "var/watcher.sqlite3",
                    "health_file": "var/health.json",
                    "wake_file": "var/wake-request.json",
                    "alert_file": "var/watchdog-alert.json",
                    "notifications_enabled": False,
                }
            ),
            encoding="utf-8",
        )
        self.config = watcher.load_config(config_path)
        self.connection = watcher.connect_database(self.config.database)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def candidate_file(
        self,
        *,
        handle: str = "target_user",
        text: str | None = "Candidate text",
        record_type: str = "x_post",
    ) -> Path:
        path = self.root / "candidates.jsonl"
        record = {
            "record_type": record_type,
            "status_id": "7000000000000000001",
            "account_handle": handle,
            "created_at_utc": "2020-01-02T03:04:05Z",
            "canonical_url": (
                "https://x.com/target_user/status/7000000000000000001"
            ),
            "text_state": (
                "full_as_returned" if text is not None else "unavailable"
            ),
            "text_verbatim": text,
            "verification_state": "unresolved_model_native_search_result",
        }
        path.write_text(
            json.dumps(record, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    def insert_current_event(self) -> None:
        payload = {
            "id": "8000000000000000001",
            "author_id": "901",
            "username": "target_user",
            "text": "Current text",
            "created_at": "2026-07-25T12:00:00Z",
            "conversation_id": "8000000000000000000",
            "in_reply_to_user_id": "900",
        }
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO events(
                    event_id, author_id, username, created_at,
                    conversation_id, in_reply_to_user_id, is_reply,
                    payload_json, first_seen_at, delivery_state
                ) VALUES(
                    '8000000000000000001', '901', 'target_user',
                    '2026-07-25T12:00:00Z', '8000000000000000000',
                    '900', 1, ?, ?, 'queued'
                )
                """,
                (json.dumps(payload), watcher.isoformat()),
            )

    def test_plan_accepts_addendum_record_as_quarantined_candidate(self) -> None:
        path = self.candidate_file(
            record_type="x_post_identity_addendum",
        )

        plan = candidate_corpus.plan_candidate_import(
            [path],
            subject_user_id="901",
            expected_handle="target_user",
        )

        self.assertEqual(len(plan.posts), 1)
        self.assertFalse(plan.summary()["auto_usable_as_evidence"])
        self.assertEqual(
            plan.summary()["trust_state"],
            "unverified_candidate",
        )

    def test_plan_rejects_mismatched_handle(self) -> None:
        path = self.candidate_file(handle="another_user")

        with self.assertRaisesRegex(ValueError, "expected @target_user"):
            candidate_corpus.plan_candidate_import(
                [path],
                subject_user_id="901",
                expected_handle="target_user",
            )

    def test_import_is_idempotent_and_history_stays_unverified(self) -> None:
        path = self.candidate_file()
        plan = candidate_corpus.plan_candidate_import(
            [path],
            subject_user_id="901",
            expected_handle="target_user",
        )

        first = candidate_corpus.apply_candidate_import(
            self.connection,
            plan,
        )
        second = candidate_corpus.apply_candidate_import(
            self.connection,
            plan,
        )
        self.insert_current_event()
        history = candidate_corpus.candidate_history_for_event(
            self.connection,
            "8000000000000000001",
            limit=10,
        )

        self.assertEqual(first["status"], "imported")
        self.assertEqual(second["status"], "already_imported")
        self.assertEqual(history["total_candidates"], 1)
        self.assertFalse(history["candidates"][0]["usable_as_evidence"])
        self.assertEqual(
            history["candidates"][0]["trust_state"],
            "unverified_candidate",
        )

    def test_live_verification_promotes_exact_record_without_rewriting_candidate(
        self,
    ) -> None:
        path = self.candidate_file()
        plan = candidate_corpus.plan_candidate_import(
            [path],
            subject_user_id="901",
            expected_handle="target_user",
        )
        candidate_corpus.apply_candidate_import(self.connection, plan)
        self.insert_current_event()

        verified = candidate_corpus.record_candidate_verification(
            self.connection,
            status_id="7000000000000000001",
            exact_text="Live exact text",
            canonical_url=(
                "https://x.com/target_user/status/7000000000000000001"
            ),
            observed_at="2026-07-25T13:00:00Z",
            verification_method="live_x_dom",
            verified_by="sol_browser_owner",
        )
        repeated = candidate_corpus.record_candidate_verification(
            self.connection,
            status_id="7000000000000000001",
            exact_text="Live exact text",
            canonical_url=(
                "https://x.com/target_user/status/7000000000000000001"
            ),
            observed_at="2026-07-25T13:00:00Z",
            verification_method="live_x_dom",
            verified_by="sol_browser_owner",
        )
        history = candidate_corpus.candidate_history_for_event(
            self.connection,
            "8000000000000000001",
            limit=10,
        )

        self.assertEqual(verified["status"], "verified")
        self.assertEqual(repeated["status"], "already_verified")
        self.assertFalse(verified["candidate_text_match"])
        self.assertTrue(history["candidates"][0]["usable_as_evidence"])
        self.assertEqual(
            history["candidates"][0]["verified_exact_text"],
            "Live exact text",
        )
        stored_candidate = self.connection.execute(
            """
            SELECT exact_text, trust_state
            FROM candidate_public_posts
            WHERE status_id = '7000000000000000001'
            """
        ).fetchone()
        self.assertEqual(stored_candidate["exact_text"], "Candidate text")
        self.assertEqual(
            stored_candidate["trust_state"],
            "unverified_candidate",
        )


if __name__ == "__main__":
    unittest.main()
