from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import xmention_watcher as watcher
from scripts import autopilot_dispatch, browser_owner_evidence
from scripts import finalize_browser_owner_session as finalizer


class CommitBrowserOwnerEventTests(unittest.TestCase):
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
                    "mandatory_response_mode": True,
                    "notifications_enabled": False,
                }
            ),
            encoding="utf-8",
        )
        self.config = watcher.load_config(self.config_path)
        self.connection = watcher.connect_database(self.config.database)
        self.claim_token = "11111111-2222-4333-8444-555555555555"
        self.conversation_id = "7000000000000000000"
        self.event_id = "7000000000000000001"
        self.parent_id = self.conversation_id
        self.reply_id = "7000000000000000101"
        self.session_dir = (
            self.root / "var/evidence/browser-owner" / self.claim_token
        )
        self.event_dir = self.session_dir / self.event_id
        self.event_dir.mkdir(parents=True)
        self._insert_event()
        self._write_active_state([self.event_id])
        watcher.refresh_wake_file(self.config, self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def _insert_event(self) -> None:
        now = watcher.isoformat()
        payload = {
            "id": self.event_id,
            "author_id": "901",
            "username": "target_user",
            "text": "Exact target text",
            "created_at": now,
            "conversation_id": self.conversation_id,
            "in_reply_to_user_id": self.config.user_id,
            "referenced_tweets": [
                {"type": "replied_to", "id": self.parent_id}
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
                    self.event_id,
                    now,
                    self.conversation_id,
                    self.config.user_id,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )

    def _write_active_state(self, event_ids: list[str]) -> None:
        _, state_file = autopilot_dispatch.load_paths(self.config_path)
        autopilot_dispatch.atomic_write_json(
            state_file,
            {
                "version": autopilot_dispatch.STATE_VERSION,
                "attempts": {event_id: 1 for event_id in event_ids},
                "owner": {
                    "claim_token": self.claim_token,
                    "claimed_at": "2026-08-02T04:00:00Z",
                    "lease_expires_at": "2026-08-02T04:30:00Z",
                    "event_ids": event_ids,
                },
            },
        )

    def _insert_chain(self, provenance: str) -> None:
        now = watcher.isoformat()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO conversation_chains(
                    chain_id, root_status_id, provenance,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    self.conversation_id,
                    self.conversation_id,
                    provenance,
                    now,
                    now,
                ),
            )

    def _target(self) -> dict[str, object]:
        return {
            "status_id": self.event_id,
            "parent_status_id": self.parent_id,
            "chain_id": self.conversation_id,
            "root_status_id": self.conversation_id,
            "url": f"https://x.com/target_user/status/{self.event_id}",
            "author_handle": "target_user",
            "author_id": "901",
            "exact_text": "Exact target text",
            "posted_at": "2026-08-02T04:00:00Z",
            "provenance": "official_api_payload_and_live_x_dom",
            "media": [],
        }

    def _verification_report(
        self,
        *,
        status_id: str,
        text: str,
    ) -> dict[str, object]:
        return {
            "valid": True,
            "exact_file_match": True,
            "parent_matches": True,
            "status_id": status_id,
            "parent_status_ids": [self.event_id],
            "code_points": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }

    def _published_evidence(self) -> dict[str, object]:
        reply_text = "Exact published reply"
        (self.event_dir / "reply.txt").write_text(
            reply_text + "\n",
            encoding="utf-8",
        )
        (self.event_dir / "api-verification.json").write_text(
            json.dumps(
                self._verification_report(
                    status_id=self.reply_id,
                    text=reply_text,
                )
            ),
            encoding="utf-8",
        )
        return {
            "schema_version": 1,
            "state": "published_verified",
            "session_id": self.claim_token,
            "chain_provenance": "pro",
            "target": self._target(),
            "classification": {
                "stance": "opposing",
                "stance_detail": "exact_test_claim",
                "confidence": "high",
            },
            "generation": {
                "generation_profile": "local_sol_max",
                "generation_skill": "poyasnitelnaya-brigada-v2",
                "generation_model": "gpt-5.6-sol",
                "reasoning_effort": "max",
                "chatgpt_web_used": False,
            },
            "reply": {
                "file": "reply.txt",
                "status_id": self.reply_id,
                "parent_status_id": self.event_id,
                "url": f"https://x.com/axrbarsic/status/{self.reply_id}",
                "author_handle": "axrbarsic",
                "posted_at": "2026-08-02T04:01:00Z",
                "code_points": len(reply_text),
                "sha256_exact_text": hashlib.sha256(
                    reply_text.encode("utf-8")
                ).hexdigest(),
                "provenance": "local_sol_max_and_official_note_tweet",
            },
            "sources": ["https://example.com/primary"],
            "resolution": {
                "reason": "Exact publication verified.",
                "evidence": ["official X API"],
            },
            "verification": {
                "official_api_note_tweet_report_file": (
                    "api-verification.json"
                ),
                "verified_at": "2026-08-02T04:02:00Z",
            },
        }

    def _write_evidence(self, payload: dict[str, object]) -> None:
        (self.event_dir / "evidence.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def test_commits_published_outcome_and_is_idempotent(self) -> None:
        self._write_evidence(self._published_evidence())

        first = browser_owner_evidence.commit_event(
            config_path=self.config_path,
            claim_token=self.claim_token,
            requested_event_dir=self.event_dir,
        )
        second = browser_owner_evidence.commit_event(
            config_path=self.config_path,
            claim_token=self.claim_token,
            requested_event_dir=self.event_dir,
        )

        resolution = self.connection.execute(
            "SELECT disposition, reply_url FROM event_resolutions "
            "WHERE event_id = ?",
            (self.event_id,),
        ).fetchone()
        self.assertEqual(first["status"], "committed")
        self.assertEqual(first["history"], "created")
        self.assertEqual(first["ledger"], "created")
        self.assertEqual(second["history"], "unchanged")
        self.assertEqual(second["ledger"], "unchanged")
        self.assertEqual(resolution["disposition"], "published")
        self.assertEqual(
            resolution["reply_url"],
            f"https://x.com/axrbarsic/status/{self.reply_id}",
        )
        ledger = json.loads(
            (self.event_dir / "run-ledger.jsonl").read_text(encoding="utf-8")
        )
        self.assertIn(
            str(
                (self.event_dir / "api-verification.json").relative_to(
                    self.root
                )
            ),
            ledger["evidence"],
        )
        self.assertFalse(
            any(Path(value).is_absolute() for value in ledger["evidence"])
        )
        wake = json.loads(
            self.config.wake_file.read_text(encoding="utf-8")
        )
        self.assertEqual(wake["pending_count"], 0)

    def test_existing_chain_provenance_is_authoritative(self) -> None:
        self._insert_chain("pro")
        evidence = self._published_evidence()
        evidence["chain_provenance"] = "short"
        target = evidence["target"]
        assert isinstance(target, dict)
        target["chain_provenance"] = "short"
        self._write_evidence(evidence)

        result = browser_owner_evidence.commit_event(
            config_path=self.config_path,
            claim_token=self.claim_token,
            requested_event_dir=self.event_dir,
        )

        history = json.loads(
            (self.event_dir / "conversation-history.jsonl").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(result["status"], "committed")
        self.assertEqual(history["provenance"], "pro")

    def test_rejects_invalid_chain_provenance_before_sync(self) -> None:
        evidence = self._published_evidence()
        evidence["chain_provenance"] = "unknown"
        self._write_evidence(evidence)

        with self.assertRaisesRegex(
            ValueError,
            "Evidence chain provenance is invalid",
        ):
            browser_owner_evidence.commit_event(
                config_path=self.config_path,
                claim_token=self.claim_token,
                requested_event_dir=self.event_dir,
            )

    def test_repairs_stale_generated_records_before_manifest(self) -> None:
        self._write_evidence(self._published_evidence())
        history_path = self.event_dir / "conversation-history.jsonl"
        ledger_path = self.event_dir / "run-ledger.jsonl"
        history_path.write_text("{}\n", encoding="utf-8")
        ledger_path.write_text("{}\n", encoding="utf-8")

        result = browser_owner_evidence.commit_event(
            config_path=self.config_path,
            claim_token=self.claim_token,
            requested_event_dir=self.event_dir,
        )

        history = json.loads(history_path.read_text(encoding="utf-8"))
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(result["history"], "repaired")
        self.assertEqual(result["ledger"], "repaired")
        self.assertEqual(history["chain_id"], self.conversation_id)
        self.assertEqual(ledger["event_id"], self.event_id)
        self.assertEqual(ledger["disposition"], "published")

    def test_generated_event_records_finalize_as_one_claim(self) -> None:
        self._write_evidence(self._published_evidence())
        browser_owner_evidence.commit_event(
            config_path=self.config_path,
            claim_token=self.claim_token,
            requested_event_dir=self.event_dir,
        )

        result = finalizer.finalize_session(
            config_path=self.config_path,
            requested_session_dir=self.session_dir,
        )

        self.assertEqual(result["status"], "finalized")
        self.assertEqual(result["event_ids"], [self.event_id])
        self.assertTrue((self.session_dir / "manifest.json").is_file())

    def test_finalize_checks_aggregate_conflict_before_database_replay(
        self,
    ) -> None:
        self._write_evidence(self._published_evidence())
        browser_owner_evidence.commit_event(
            config_path=self.config_path,
            claim_token=self.claim_token,
            requested_event_dir=self.event_dir,
        )
        (self.session_dir / "conversation-history.jsonl").write_text(
            "{}\n",
            encoding="utf-8",
        )

        with (
            mock.patch.object(watcher, "sync_browser_handoffs") as sync,
            self.assertRaisesRegex(ValueError, "differs from event evidence"),
        ):
            finalizer.finalize_session(
                config_path=self.config_path,
                requested_session_dir=self.session_dir,
            )

        sync.assert_not_called()
        self.assertFalse((self.session_dir / "manifest.json").exists())

    def test_explicit_empty_resolution_never_falls_back_to_blocker(self) -> None:
        payload = self._published_evidence()
        payload["resolution"] = {}
        payload["blocker"] = {
            "reason": "This alternate object must not be selected.",
        }
        self._write_evidence(payload)

        with self.assertRaisesRegex(ValueError, "non-empty text: reason"):
            browser_owner_evidence.commit_event(
                config_path=self.config_path,
                claim_token=self.claim_token,
                requested_event_dir=self.event_dir,
            )

    def test_rejects_invalid_official_verification_before_writing(self) -> None:
        payload = self._published_evidence()
        report_path = self.event_dir / "api-verification.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["parent_status_ids"] = ["7999999999999999999"]
        report_path.write_text(json.dumps(report), encoding="utf-8")
        self._write_evidence(payload)

        with self.assertRaisesRegex(ValueError, "parent ID does not match"):
            browser_owner_evidence.commit_event(
                config_path=self.config_path,
                claim_token=self.claim_token,
                requested_event_dir=self.event_dir,
            )

        self.assertFalse((self.event_dir / "conversation-history.jsonl").exists())
        self.assertFalse((self.event_dir / "run-ledger.jsonl").exists())
        resolution = self.connection.execute(
            "SELECT 1 FROM event_resolutions WHERE event_id = ?",
            (self.event_id,),
        ).fetchone()
        self.assertIsNone(resolution)

    def test_rejects_target_text_that_differs_from_stored_event(self) -> None:
        payload = self._published_evidence()
        payload["target"]["exact_text"] = "Tampered target text"
        self._write_evidence(payload)

        with self.assertRaisesRegex(ValueError, "target text does not match"):
            browser_owner_evidence.commit_event(
                config_path=self.config_path,
                claim_token=self.claim_token,
                requested_event_dir=self.event_dir,
            )

        self.assertFalse((self.event_dir / "conversation-history.jsonl").exists())

    def test_rejects_local_max_without_v2_skill(self) -> None:
        payload = self._published_evidence()
        payload["generation"]["generation_skill"] = (
            "poyasnitelnaya-brigada"
        )
        self._write_evidence(payload)

        with self.assertRaisesRegex(ValueError, "must use poyasnitelnaya"):
            browser_owner_evidence.commit_event(
                config_path=self.config_path,
                claim_token=self.claim_token,
                requested_event_dir=self.event_dir,
            )

    def test_commits_sol_short_without_generation_skill(self) -> None:
        payload = self._published_evidence()
        payload["generation"]["generation_profile"] = "sol_short"
        payload["generation"]["generation_skill"] = None
        self._write_evidence(payload)

        result = browser_owner_evidence.commit_event(
            config_path=self.config_path,
            claim_token=self.claim_token,
            requested_event_dir=self.event_dir,
        )

        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["disposition"], "published")

    def test_commits_commenter_requested_image_with_media_proof(self) -> None:
        payload = self._published_evidence()
        media_file = self.event_dir / "generated-image.png"
        media_file.write_bytes(b"\x89PNG\r\n\x1a\nverified-image")
        media_sha = hashlib.sha256(media_file.read_bytes()).hexdigest()
        published_media = [
            {
                "media_key": "3_verified",
                "type": "photo",
                "url": "https://pbs.twimg.com/media/verified.png",
                "width": 1024,
                "height": 1024,
            }
        ]
        payload["generation"].update(
            {
                "generation_profile": "commenter_requested_image",
                "generation_skill": "imagegen",
                "media_file": media_file.name,
                "media_sha256": media_sha,
                "media_mime_type": "image/png",
                "composer_attachment_verified": True,
            }
        )
        payload["reply"]["media"] = published_media
        report_path = self.event_dir / "api-verification.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report.update(
            {
                "require_media": True,
                "media_verified": True,
                "media_count": 1,
                "published_photo_present": True,
                "published_media": published_media,
            }
        )
        report_path.write_text(json.dumps(report), encoding="utf-8")
        self._write_evidence(payload)

        result = browser_owner_evidence.commit_event(
            config_path=self.config_path,
            claim_token=self.claim_token,
            requested_event_dir=self.event_dir,
        )

        self.assertEqual(result["status"], "committed")
        stored_media = self.connection.execute(
            "SELECT media_json FROM conversation_turns WHERE status_id = ?",
            (self.reply_id,),
        ).fetchone()["media_json"]
        self.assertEqual(json.loads(stored_media), published_media)
        ledger = json.loads(
            (self.event_dir / "run-ledger.jsonl").read_text(encoding="utf-8")
        )
        self.assertTrue(
            any(value.endswith("generated-image.png") for value in ledger["evidence"])
        )

    def test_commits_local_sol_max_with_four_visuals(self) -> None:
        payload = self._published_evidence()
        media_files = []
        published_media = []
        for index in range(1, 5):
            media_file = self.event_dir / f"chronology-{index}.png"
            media_file.write_bytes(
                b"\x89PNG\r\n\x1a\nverified-image-" + str(index).encode()
            )
            media_files.append(
                {
                    "file": media_file.name,
                    "sha256": hashlib.sha256(media_file.read_bytes()).hexdigest(),
                    "mime_type": "image/png",
                    "published_media_key": f"3_verified_{index}",
                }
            )
            published_media.append(
                {
                    "media_key": f"3_verified_{index}",
                    "type": "photo",
                    "url": f"https://pbs.twimg.com/media/verified-{index}.png",
                    "width": 1024,
                    "height": 1792,
                }
            )
        payload["generation"].update(
            {
                "generation_profile": "local_sol_max_visual",
                "generation_skill": "poyasnitelnaya-brigada-v2",
                "visual_skill": "imagegen",
                "media_files": media_files,
                "composer_attachment_verified": True,
                "composer_attachment_count": 4,
            }
        )
        payload["reply"]["media"] = published_media
        report_path = self.event_dir / "api-verification.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report.update(
            {
                "require_media": True,
                "media_verified": True,
                "media_count": 4,
                "published_photo_present": True,
                "published_media": published_media,
            }
        )
        report_path.write_text(json.dumps(report), encoding="utf-8")
        self._write_evidence(payload)

        result = browser_owner_evidence.commit_event(
            config_path=self.config_path,
            claim_token=self.claim_token,
            requested_event_dir=self.event_dir,
        )

        self.assertEqual(result["status"], "committed")
        ledger = json.loads(
            (self.event_dir / "run-ledger.jsonl").read_text(encoding="utf-8")
        )
        for index in range(1, 5):
            self.assertTrue(
                any(
                    value.endswith(f"chronology-{index}.png")
                    for value in ledger["evidence"]
                )
            )

    def test_rejects_multiple_images_when_api_media_count_differs(self) -> None:
        payload = self._published_evidence()
        media_files = []
        for index in range(1, 3):
            media_file = self.event_dir / f"chronology-{index}.png"
            media_file.write_bytes(
                b"\x89PNG\r\n\x1a\nverified-image-" + str(index).encode()
            )
            media_files.append(
                {
                    "file": media_file.name,
                    "sha256": hashlib.sha256(media_file.read_bytes()).hexdigest(),
                    "mime_type": "image/png",
                    "published_media_key": f"3_verified_{index}",
                }
            )
        payload["generation"].update(
            {
                "generation_profile": "commenter_requested_image",
                "generation_skill": "imagegen",
                "media_files": media_files,
                "composer_attachment_verified": True,
                "composer_attachment_count": 2,
            }
        )
        published_media = [
            {
                "media_key": "3_verified_1",
                "type": "photo",
                "url": "https://pbs.twimg.com/media/verified-1.png",
            }
        ]
        payload["reply"]["media"] = published_media
        report_path = self.event_dir / "api-verification.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report.update(
            {
                "require_media": True,
                "media_verified": True,
                "media_count": 1,
                "published_photo_present": True,
                "published_media": published_media,
            }
        )
        report_path.write_text(json.dumps(report), encoding="utf-8")
        self._write_evidence(payload)

        with self.assertRaisesRegex(
            ValueError,
            "Official X media count does not match generated media count",
        ):
            browser_owner_evidence.commit_event(
                config_path=self.config_path,
                claim_token=self.claim_token,
                requested_event_dir=self.event_dir,
            )

    def test_rejects_local_sol_visual_without_media_array(self) -> None:
        payload = self._published_evidence()
        media_file = self.event_dir / "chronology-1.png"
        media_file.write_bytes(b"\x89PNG\r\n\x1a\nverified-image")
        payload["generation"].update(
            {
                "generation_profile": "local_sol_max_visual",
                "generation_skill": "poyasnitelnaya-brigada-v2",
                "visual_skill": "imagegen",
                "media_file": media_file.name,
                "media_sha256": hashlib.sha256(
                    media_file.read_bytes()
                ).hexdigest(),
                "media_mime_type": "image/png",
                "composer_attachment_verified": True,
            }
        )
        published_media = [
            {
                "media_key": "3_verified",
                "type": "photo",
                "url": "https://pbs.twimg.com/media/verified.png",
            }
        ]
        payload["reply"]["media"] = published_media
        report_path = self.event_dir / "api-verification.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report.update(
            {
                "require_media": True,
                "media_verified": True,
                "media_count": 1,
                "published_photo_present": True,
                "published_media": published_media,
            }
        )
        report_path.write_text(json.dumps(report), encoding="utf-8")
        self._write_evidence(payload)

        with self.assertRaisesRegex(
            ValueError,
            "local_sol_max_visual must use generation.media_files",
        ):
            browser_owner_evidence.commit_event(
                config_path=self.config_path,
                claim_token=self.claim_token,
                requested_event_dir=self.event_dir,
            )

    def test_rejects_media_array_without_exact_composer_count(self) -> None:
        payload = self._published_evidence()
        media_file = self.event_dir / "generated-image.png"
        media_file.write_bytes(b"\x89PNG\r\n\x1a\nverified-image")
        payload["generation"].update(
            {
                "generation_profile": "commenter_requested_image",
                "generation_skill": "imagegen",
                "media_files": [
                    {
                        "file": media_file.name,
                        "sha256": hashlib.sha256(
                            media_file.read_bytes()
                        ).hexdigest(),
                        "mime_type": "image/png",
                        "published_media_key": "3_verified",
                    }
                ],
                "composer_attachment_verified": True,
            }
        )
        published_media = [
            {
                "media_key": "3_verified",
                "type": "photo",
                "url": "https://pbs.twimg.com/media/verified.png",
            }
        ]
        payload["reply"]["media"] = published_media
        report_path = self.event_dir / "api-verification.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report.update(
            {
                "require_media": True,
                "media_verified": True,
                "media_count": 1,
                "published_photo_present": True,
                "published_media": published_media,
            }
        )
        report_path.write_text(json.dumps(report), encoding="utf-8")
        self._write_evidence(payload)

        with self.assertRaisesRegex(
            ValueError,
            "Composer attachment count does not match generated media",
        ):
            browser_owner_evidence.commit_event(
                config_path=self.config_path,
                claim_token=self.claim_token,
                requested_event_dir=self.event_dir,
            )

    def test_rejects_requested_image_without_official_media_proof(self) -> None:
        payload = self._published_evidence()
        media_file = self.event_dir / "generated-image.png"
        media_file.write_bytes(b"\x89PNG\r\n\x1a\nverified-image")
        payload["generation"].update(
            {
                "generation_profile": "commenter_requested_image",
                "generation_skill": "imagegen",
                "media_file": media_file.name,
                "media_sha256": hashlib.sha256(
                    media_file.read_bytes()
                ).hexdigest(),
                "media_mime_type": "image/png",
                "composer_attachment_verified": True,
            }
        )
        self._write_evidence(payload)

        with self.assertRaisesRegex(ValueError, "did not require media"):
            browser_owner_evidence.commit_event(
                config_path=self.config_path,
                claim_token=self.claim_token,
                requested_event_dir=self.event_dir,
            )

    def test_sync_failure_leaves_no_generated_event_records(self) -> None:
        self._write_evidence(self._published_evidence())

        with (
            mock.patch.object(
                watcher,
                "sync_browser_handoffs",
                side_effect=ValueError("injected sync failure"),
            ),
            self.assertRaisesRegex(ValueError, "injected sync failure"),
        ):
            browser_owner_evidence.commit_event(
                config_path=self.config_path,
                claim_token=self.claim_token,
                requested_event_dir=self.event_dir,
            )

        self.assertFalse((self.event_dir / "conversation-history.jsonl").exists())
        self.assertFalse((self.event_dir / "run-ledger.jsonl").exists())
        self.assertFalse(
            any(
                path.name.startswith(".event-commit.")
                for path in self.event_dir.iterdir()
            )
        )
        resolution = self.connection.execute(
            "SELECT 1 FROM event_resolutions WHERE event_id = ?",
            (self.event_id,),
        ).fetchone()
        self.assertIsNone(resolution)

    def test_rejects_event_outside_active_claim(self) -> None:
        self._write_evidence(self._published_evidence())
        self._write_active_state(["7000000000000000002"])

        with self.assertRaisesRegex(ValueError, "not part of the active claim"):
            browser_owner_evidence.commit_event(
                config_path=self.config_path,
                claim_token=self.claim_token,
                requested_event_dir=self.event_dir,
            )

    def test_commits_terminal_blocker(self) -> None:
        self._write_evidence(
            {
                "schema_version": 1,
                "state": "terminal_blocker_verified",
                "session_id": self.claim_token,
                "chain_provenance": "short",
                "target": self._target(),
                "classification": {
                    "stance": "opposing",
                    "confidence": "high",
                },
                "blocker": {
                    "code": "target_unavailable",
                    "reason": "Exact target is unavailable.",
                    "evidence": ["live terminal state"],
                },
                "verification": {
                    "verified_at": "2026-08-02T04:02:00Z",
                },
                "sources": [],
            }
        )

        result = browser_owner_evidence.commit_event(
            config_path=self.config_path,
            claim_token=self.claim_token,
            requested_event_dir=self.event_dir,
        )

        self.assertEqual(result["disposition"], "blocked")
        self.assertEqual(result["blocker_code"], "target_unavailable")

    def test_commits_verified_already_answered_skip(self) -> None:
        existing_text = "Existing" + chr(0x2014) + "exact Alex reply"
        existing_file = self.event_dir / "existing-alex-reply.txt"
        existing_file.write_text(existing_text + "\n", encoding="utf-8")
        verification_file = self.event_dir / "verification.json"
        verification_file.write_text(
            json.dumps(
                self._verification_report(
                    status_id=self.reply_id,
                    text=existing_text,
                )
            ),
            encoding="utf-8",
        )
        self._write_evidence(
            {
                "schema_version": 1,
                "state": "already_answered_verified",
                "session_id": self.claim_token,
                "chain_provenance": "short",
                "target": self._target(),
                "existing_reply": {
                    "file": existing_file.name,
                    "status_id": self.reply_id,
                    "parent_status_id": self.event_id,
                    "url": (
                        f"https://x.com/axrbarsic/status/{self.reply_id}"
                    ),
                    "author_handle": "axrbarsic",
                    "posted_at": "2026-08-02T04:01:00Z",
                    "provenance": "official_x_api_existing_alex_child",
                },
                "classification": {
                    "stance": "neutral",
                    "confidence": "high",
                },
                "resolution": {
                    "reason": "Exact Alex child already exists.",
                },
                "duplicate_verification": {
                    "verification_file": verification_file.name,
                },
                "verification": {
                    "verified_at": "2026-08-02T04:02:00Z",
                },
                "sources": [],
            }
        )

        result = browser_owner_evidence.commit_event(
            config_path=self.config_path,
            claim_token=self.claim_token,
            requested_event_dir=self.event_dir,
        )

        self.assertEqual(result["disposition"], "skip")
        self.assertIsNone(result["reply_url"])
        existing_turn = self.connection.execute(
            "SELECT actor, parent_status_id FROM conversation_turns "
            "WHERE status_id = ?",
            (self.reply_id,),
        ).fetchone()
        self.assertEqual(existing_turn["actor"], "alex")
        self.assertEqual(existing_turn["parent_status_id"], self.event_id)


if __name__ == "__main__":
    unittest.main()
