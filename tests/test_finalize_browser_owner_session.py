from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import evidence_import
import xmention_watcher as watcher
from scripts import autopilot_dispatch
from scripts import finalize_browser_owner_session as finalizer


class FinalizeBrowserOwnerSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "user_id": "900",
                    "database": "var/watcher.sqlite3",
                    "wake_file": "var/wake-request.json",
                    "autopilot_state_file": "var/autopilot-dispatch.json",
                    "notifications_enabled": False,
                }
            ),
            encoding="utf-8",
        )
        self.config = watcher.load_config(self.config_path)
        self.connection = watcher.connect_database(self.config.database)
        self.claim_token = "11111111-2222-4333-8444-555555555555"
        self.event_id = "7000000000000000001"
        self.session_dir = (
            self.root
            / "var/evidence/browser-owner"
            / self.claim_token
        )
        self.event_dir = self.session_dir / self.event_id
        self.event_dir.mkdir(parents=True)
        self._insert_event(self.event_id)
        self._write_event_evidence(self.event_id)
        self._write_active_state([self.event_id])

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def _insert_event(self, event_id: str) -> None:
        now = watcher.isoformat()
        payload = {
            "id": event_id,
            "author_id": "901",
            "text": "Exact target text",
            "created_at": now,
            "conversation_id": event_id,
            "in_reply_to_user_id": self.config.user_id,
            "referenced_tweets": [
                {"type": "replied_to", "id": str(int(event_id) - 1)}
            ],
        }
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO events(
                    event_id, author_id, username, created_at,
                    conversation_id, in_reply_to_user_id, is_reply,
                    payload_json, first_seen_at, delivery_state
                ) VALUES(?, '901', 'target_user', ?, ?, ?, 1, ?, ?, 'queued')
                """,
                (
                    event_id,
                    now,
                    event_id,
                    self.config.user_id,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )

    def _history_record(self, event_id: str) -> dict[str, object]:
        return {
            "record_type": "initial_audit_event_turn",
            "chain_id": event_id,
            "conversation_root_id": event_id,
            "chain_provenance": "short",
            "parent_status_id": str(int(event_id) - 1),
            "status_id": event_id,
            "actor": "user",
            "author": "target_user",
            "url": f"https://x.com/target_user/status/{event_id}",
            "exact_text": "Exact target text",
            "provenance": "live_x_dom_initial_audit",
            "media_json": [],
            "sources": [],
        }

    def _ledger_record(self, event_id: str) -> dict[str, object]:
        return {
            "event": "initial_audit_disposition",
            "event_id": event_id,
            "conversation_id": event_id,
            "direct_reply_to_axrbarsic": True,
            "history_status": "exact_user_turn_appended",
            "watcher_disposition": "durable_skip_pending_root_resolve",
            "disposition": "skip",
            "reason": "Exact live duplicate was already handled.",
            "stance": "neutral",
            "stance_detail": "neutral_exact_duplicate",
            "confidence": "high",
            "media_meaning": None,
            "evidence": ["live exact target"],
            "reply_url": None,
        }

    def _write_event_evidence(self, event_id: str) -> None:
        event_dir = self.session_dir / event_id
        event_dir.mkdir(parents=True, exist_ok=True)
        (event_dir / finalizer.HISTORY_NAME).write_text(
            json.dumps(self._history_record(event_id), ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        (event_dir / finalizer.LEDGER_NAME).write_text(
            json.dumps(self._ledger_record(event_id), ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )

    def _write_active_state(self, event_ids: list[str]) -> None:
        _, state_file = autopilot_dispatch.load_paths(self.config_path)
        autopilot_dispatch.atomic_write_json(
            state_file,
            {
                "version": autopilot_dispatch.STATE_VERSION,
                "events": {},
                "owner": {
                    "claim_token": self.claim_token,
                    "event_ids": event_ids,
                },
            },
        )

    def _write_published_event_evidence(self) -> tuple[str, str]:
        reply_id = "7000000000000000101"
        reply_url = f"https://x.com/axrbarsic/status/{reply_id}"
        reply_text = "Exact published reply"
        reply_path = self.event_dir / "reply.txt"
        reply_path.write_text(reply_text + "\n", encoding="utf-8")
        history = {
            "chain_id": self.event_id,
            "root_status_id": self.event_id,
            "provenance": "short",
            "turns": [
                {
                    "status_id": self.event_id,
                    "parent_status_id": str(int(self.event_id) - 1),
                    "actor": "user",
                    "author": "@target_user",
                    "url": (
                        "https://x.com/target_user/status/"
                        f"{self.event_id}"
                    ),
                    "exact_text": "Exact target text",
                    "provenance": "live_x_dom_initial_audit",
                    "media": [],
                    "source_urls": [],
                },
                {
                    "status_id": reply_id,
                    "parent_status_id": self.event_id,
                    "actor": "alex",
                    "author": "@axrbarsic",
                    "url": reply_url,
                    "exact_text": reply_text,
                    "provenance": "self_authored_short_sol",
                    "media": [],
                    "source_urls": [],
                },
            ],
        }
        ledger = {
            "event": "initial_audit_disposition",
            "event_id": self.event_id,
            "conversation_id": self.event_id,
            "direct_reply_to_axrbarsic": True,
            "history_status": "exact_user_turn_appended",
            "alex_history_status": "exact_alex_turn_appended",
            "watcher_disposition": (
                "verified_publication_pending_root_resolve"
            ),
            "disposition": "published",
            "reason": "Exact publication verified.",
            "stance": "opposing",
            "stance_detail": "opposing_exact_claim",
            "confidence": "high",
            "media_meaning": None,
            "evidence": ["live exact target", "verified exact child"],
            "reply_url": reply_url,
            "source_path": str(reply_path),
            "source_sha256": evidence_import.sha256(reply_path),
            "source_code_points": len(reply_text),
        }
        (self.event_dir / finalizer.HISTORY_NAME).write_text(
            json.dumps(history, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (self.event_dir / finalizer.LEDGER_NAME).write_text(
            json.dumps(ledger, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return reply_url, reply_text

    def test_finalizes_all_claim_events_and_is_idempotent_after_claim(self) -> None:
        first = finalizer.finalize_session(
            config_path=self.config_path,
            requested_session_dir=self.session_dir,
        )
        _, state_file = autopilot_dispatch.load_paths(self.config_path)
        autopilot_dispatch.atomic_write_json(
            state_file,
            {
                "version": autopilot_dispatch.STATE_VERSION,
                "events": {},
                "owner": None,
            },
        )
        second = finalizer.finalize_session(
            config_path=self.config_path,
            requested_session_dir=self.session_dir,
        )

        resolution = self.connection.execute(
            "SELECT disposition FROM event_resolutions WHERE event_id = ?",
            (self.event_id,),
        ).fetchone()
        self.assertEqual(first["status"], "finalized")
        self.assertEqual(first["event_ids"], [self.event_id])
        self.assertEqual(second["status"], "already_finalized")
        self.assertEqual(resolution["disposition"], "skip")
        self.assertTrue((self.session_dir / "manifest.json").is_file())

    def test_finalizes_verified_publication_with_exact_source(self) -> None:
        reply_url, reply_text = self._write_published_event_evidence()

        result = finalizer.finalize_session(
            config_path=self.config_path,
            requested_session_dir=self.session_dir,
        )

        resolution = self.connection.execute(
            "SELECT disposition, reply_url FROM event_resolutions "
            "WHERE event_id = ?",
            (self.event_id,),
        ).fetchone()
        alex_turn = self.connection.execute(
            "SELECT exact_text FROM conversation_turns WHERE url = ?",
            (reply_url,),
        ).fetchone()
        self.assertEqual(result["status"], "finalized")
        self.assertEqual(resolution["disposition"], "published")
        self.assertEqual(resolution["reply_url"], reply_url)
        self.assertEqual(alex_turn["exact_text"], reply_text)

    def test_rejects_incomplete_claim_before_writing_aggregates(self) -> None:
        missing_event_id = "7000000000000000002"
        self._insert_event(missing_event_id)
        self._write_active_state([self.event_id, missing_event_id])

        with self.assertRaisesRegex(ValueError, "does not match active claim"):
            finalizer.finalize_session(
                config_path=self.config_path,
                requested_session_dir=self.session_dir,
            )

        self.assertFalse((self.session_dir / finalizer.HISTORY_NAME).exists())
        self.assertFalse((self.session_dir / finalizer.LEDGER_NAME).exists())
        self.assertFalse((self.session_dir / "manifest.json").exists())

    def test_rejects_malformed_jsonl(self) -> None:
        (self.event_dir / finalizer.HISTORY_NAME).write_text(
            "{not-json}\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "Invalid JSONL"):
            finalizer.finalize_session(
                config_path=self.config_path,
                requested_session_dir=self.session_dir,
            )

    def test_rejects_duplicate_records(self) -> None:
        path = self.event_dir / finalizer.HISTORY_NAME
        record = path.read_text(encoding="utf-8")
        path.write_text(record + record, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Duplicate history record"):
            finalizer.finalize_session(
                config_path=self.config_path,
                requested_session_dir=self.session_dir,
            )

    def test_rejects_conflicting_existing_aggregate(self) -> None:
        (self.session_dir / finalizer.HISTORY_NAME).write_text(
            json.dumps({"unexpected": True}) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "Existing aggregate differs"):
            finalizer.finalize_session(
                config_path=self.config_path,
                requested_session_dir=self.session_dir,
            )
        resolution = self.connection.execute(
            "SELECT 1 FROM event_resolutions WHERE event_id = ?",
            (self.event_id,),
        ).fetchone()
        self.assertIsNone(resolution)

    def test_sync_failure_leaves_no_aggregate_or_temporary_files(self) -> None:
        ledger_path = self.event_dir / finalizer.LEDGER_NAME
        ledger = self._ledger_record(self.event_id)
        ledger["direct_reply_to_axrbarsic"] = False
        ledger_path.write_text(
            json.dumps(ledger) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "exactly one route"):
            finalizer.finalize_session(
                config_path=self.config_path,
                requested_session_dir=self.session_dir,
            )

        self.assertFalse((self.session_dir / finalizer.HISTORY_NAME).exists())
        self.assertFalse((self.session_dir / finalizer.LEDGER_NAME).exists())
        self.assertEqual(
            [path.name for path in self.session_dir.glob(".handoff-sync.*")],
            [],
        )


if __name__ == "__main__":
    unittest.main()
