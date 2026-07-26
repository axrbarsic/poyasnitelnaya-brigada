from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import xmention_watcher as watcher
import candidate_corpus
from scripts import autopilot_bridge
from scripts import resource_guard


class AutopilotBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.owner = self.root
        self.config = self.root / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "wake_file": "var/wake-request.json",
                    "autopilot_state_file": "var/autopilot-dispatch.json",
                    "autopilot_health_file": "var/autopilot-health.json",
                    "database": "var/watcher.sqlite3",
                    "browser_owner_cwd": ".",
                }
            ),
            encoding="utf-8",
        )
        self.wake = self.root / "var" / "wake-request.json"
        self.write_events([])

    def test_claim_rejects_external_browser_workspace(self) -> None:
        external = self.root / "owner"
        external.mkdir()
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload["browser_owner_cwd"] = str(external)
        self.config.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(
            ValueError,
            "must equal the canonical project root",
        ):
            autopilot_bridge.claim(self.config, lease_seconds=1800)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_events(self, events: list[dict]) -> None:
        self.wake.parent.mkdir(parents=True, exist_ok=True)
        self.wake.write_text(
            json.dumps({"pending_count": len(events), "events": events}),
            encoding="utf-8",
        )

    def event(self) -> dict:
        return {
            "event_id": "2081050838240211434",
            "event_url":
                "https://x.com/Timyr316661/status/2081050838240211434",
            "username": "Timyr316661",
            "conversation_id": "2080312847230210375",
            "created_at": "2026-07-25T16:15:58Z",
            "first_seen_at": "2026-07-25T16:19:00Z",
            "is_reply": True,
        }

    def test_empty_queue_is_token_free_idle(self) -> None:
        result = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertFalse(result["dispatch"])
        self.assertEqual(result["status"], "idle")
        health = json.loads(
            (self.root / "var" / "autopilot-health.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(health["status"], "idle")

    def test_claim_returns_exact_desktop_handoff(self) -> None:
        self.write_events([self.event()])

        result = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertTrue(result["dispatch"])
        self.assertIn(self.event()["event_id"], result["prompt"])
        self.assertIn("tracked_conversation_reply=true", result["prompt"])
        self.assertIn(
            "Следующий X API poll выполняет LaunchAgent",
            result["prompt"],
        )
        self.assertIn("Content-based skip запрещен", result["prompt"])
        self.assertIn("`satirical-media`", result["prompt"])
        self.assertIn("`377` или `Ложкин`", result["prompt"])
        self.assertIn("`commenter_memory`", result["prompt"])
        self.assertIn('"commenter_memory":', result["prompt"])
        self.assertNotIn("сделай один свежий poll", result["prompt"])
        self.assertNotIn("\u2013", result["prompt"])
        self.assertNotIn("\u2014", result["prompt"])

        self.write_events([])
        completed = autopilot_bridge.mark_completed(
            self.config,
            result["claim_token"],
        )
        self.assertEqual(completed["status"], "completed")
        health = json.loads(
            (self.root / "var" / "autopilot-health.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(health["handoff_count"], 1)
        dispatch = json.loads(
            (self.root / "var" / "autopilot-dispatch.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(dispatch["events"], {})

    def test_claim_includes_cross_thread_commenter_memory(self) -> None:
        watcher_config = watcher.load_config(self.config)
        connection = watcher.connect_database(watcher_config.database)
        current = self.event()
        prior_id = "2080000000000000001"
        with connection:
            for event_id, text, conversation_id, created_at in (
                (
                    prior_id,
                    "Prior exact public claim",
                    "2080000000000000000",
                    "2020-01-02T03:04:05Z",
                ),
                (
                    current["event_id"],
                    "Current exact public claim",
                    current["conversation_id"],
                    current["created_at"],
                ),
            ):
                payload = {
                    "id": event_id,
                    "author_id": "901",
                    "text": text,
                    "created_at": created_at,
                    "conversation_id": conversation_id,
                    "in_reply_to_user_id": "16337609",
                    "referenced_tweets": [
                        {"type": "replied_to", "id": str(int(event_id) - 1)}
                    ],
                }
                connection.execute(
                    """
                    INSERT INTO events(
                        event_id, author_id, username, created_at,
                        conversation_id, in_reply_to_user_id, is_reply,
                        payload_json, first_seen_at, delivery_state
                    ) VALUES(?, '901', 'Timyr316661', ?, ?, '16337609', 1,
                             ?, ?, 'queued')
                    """,
                    (
                        event_id,
                        created_at,
                        conversation_id,
                        json.dumps(payload, sort_keys=True),
                        watcher.isoformat(),
                    ),
                )
        watcher.import_history_snapshot(
            connection,
            {
                "chain_id": "2080000000000000000",
                "root_status_id": "2080000000000000000",
                "provenance": "short",
                "turns": [
                    {
                        "status_id": prior_id,
                        "actor": "user",
                        "author": "@Timyr316661",
                        "url": f"https://x.com/Timyr316661/status/{prior_id}",
                        "exact_text": "Prior exact public claim",
                    },
                    {
                        "status_id": "2080000000000000002",
                        "parent_status_id": prior_id,
                        "actor": "alex",
                        "author": "@axrbarsic",
                        "url": (
                            "https://x.com/axrbarsic/status/"
                            "2080000000000000002"
                        ),
                        "exact_text": "Prior exact Alex reply",
                    },
                ],
            },
        )
        connection.close()
        self.write_events([current])

        result = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertIn('"identity_kind":"x_user_id"', result["prompt"])
        self.assertIn("Prior exact public claim", result["prompt"])
        self.assertIn("Prior exact Alex reply", result["prompt"])

    def test_claim_includes_official_archive_alex_reply_memory(self) -> None:
        watcher_config = watcher.load_config(self.config)
        connection = watcher.connect_database(watcher_config.database)
        current = self.event()
        payload = {
            "id": current["event_id"],
            "author_id": "901",
            "text": "Current exact public claim",
            "created_at": current["created_at"],
            "conversation_id": current["conversation_id"],
            "in_reply_to_user_id": "16337609",
        }
        with connection:
            connection.execute(
                """
                INSERT INTO events(
                    event_id, author_id, username, created_at,
                    conversation_id, in_reply_to_user_id, is_reply,
                    payload_json, first_seen_at, delivery_state
                ) VALUES(?, '901', 'Timyr316661', ?, ?, '16337609', 1,
                         ?, ?, 'queued')
                """,
                (
                    current["event_id"],
                    current["created_at"],
                    current["conversation_id"],
                    json.dumps(payload, sort_keys=True),
                    watcher.isoformat(),
                ),
            )
            import_id = connection.execute(
                """
                INSERT INTO archive_imports(
                    archive_fingerprint, account_user_id, username,
                    source_name, imported_at, post_count, inserted_post_count
                ) VALUES(
                    'fixture-archive', '16337609', 'axrbarsic',
                    'fixture.zip', ?, 1, 1
                )
                """,
                (watcher.isoformat(),),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO archive_posts(
                    status_id, first_import_id, author_id,
                    username_at_import, posted_at, exact_text,
                    parent_status_id, counterparty_user_id,
                    counterparty_username, conversation_id,
                    canonical_url, source_member, payload_sha256
                ) VALUES(
                    '7000000000000000001', ?, '16337609', 'axrbarsic',
                    '2020-01-02T03:04:05Z',
                    'Exact old Alex archive reply',
                    '6999999999999999999', '901', 'Timyr316661', NULL,
                    'https://x.com/i/web/status/7000000000000000001',
                    'data/tweets.js', 'fixture-sha'
                )
                """,
                (import_id,),
            )
        connection.close()
        self.write_events([current])

        result = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertIn("Exact old Alex archive reply", result["prompt"])
        self.assertIn("official_x_archive_alex_reply", result["prompt"])

    def test_claim_quarantines_external_candidate_memory(self) -> None:
        watcher_config = watcher.load_config(self.config)
        connection = watcher.connect_database(watcher_config.database)
        current = self.event()
        candidate_path = self.root / "candidate.jsonl"
        candidate_path.write_text(
            json.dumps(
                {
                    "record_type": "x_post",
                    "status_id": "7000000000000000001",
                    "account_handle": "Timyr316661",
                    "created_at_utc": "2020-01-02T03:04:05Z",
                    "canonical_url": (
                        "https://x.com/Timyr316661/status/"
                        "7000000000000000001"
                    ),
                    "text_state": "full_as_returned",
                    "text_verbatim": "Unverified external candidate",
                    "verification_state": "model_native_search_result",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        payload = {
            "id": current["event_id"],
            "author_id": "901",
            "text": "Current exact public claim",
            "created_at": current["created_at"],
            "conversation_id": current["conversation_id"],
            "in_reply_to_user_id": "16337609",
        }
        with connection:
            connection.execute(
                """
                INSERT INTO events(
                    event_id, author_id, username, created_at,
                    conversation_id, in_reply_to_user_id, is_reply,
                    payload_json, first_seen_at, delivery_state
                ) VALUES(?, '901', 'Timyr316661', ?, ?, '16337609', 1,
                         ?, ?, 'queued')
                """,
                (
                    current["event_id"],
                    current["created_at"],
                    current["conversation_id"],
                    json.dumps(payload, sort_keys=True),
                    watcher.isoformat(),
                ),
            )
        plan = candidate_corpus.plan_candidate_import(
            [candidate_path],
            subject_user_id="901",
            expected_handle="Timyr316661",
        )
        candidate_corpus.apply_candidate_import(connection, plan)
        connection.close()
        self.write_events([current])

        result = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertIn("Unverified external candidate", result["prompt"])
        self.assertIn('"usable_as_evidence":false', result["prompt"])
        self.assertIn(
            "Verify the exact live X post before quoting",
            result["prompt"],
        )
        self.assertIn(
            "запрещено цитировать запись",
            result["prompt"],
        )

    def test_overlapping_claim_preserves_work_in_progress_health(self) -> None:
        self.write_events([self.event()])
        first = autopilot_bridge.claim(self.config, lease_seconds=1800)
        started = autopilot_bridge.mark_started(
            self.config,
            first["claim_token"],
        )

        second = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertEqual(started["status"], "work_in_progress")
        self.assertFalse(second["dispatch"])
        health = json.loads(
            (self.root / "var" / "autopilot-health.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(health["status"], "work_in_progress")
        self.assertEqual(health["event_ids"], [self.event()["event_id"]])
        self.assertEqual(health["claim_token"], first["claim_token"])

    def test_new_event_cannot_replace_active_owner_or_health(self) -> None:
        first_event = self.event()
        second_event = {
            **self.event(),
            "event_id": "2081050838240211435",
            "event_url":
                "https://x.com/Timyr316661/status/2081050838240211435",
        }
        self.write_events([first_event])
        first = autopilot_bridge.claim(self.config, lease_seconds=1800)
        autopilot_bridge.mark_started(self.config, first["claim_token"])
        self.write_events([first_event, second_event])

        second = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertFalse(second["dispatch"])
        self.assertTrue(second["owner_busy"])
        self.assertEqual(
            second["active_claim_token"],
            first["claim_token"],
        )
        health = json.loads(
            (self.root / "var" / "autopilot-health.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(health["status"], "work_in_progress")
        self.assertEqual(health["event_ids"], [first_event["event_id"]])

    def test_memory_guard_defers_without_claiming(self) -> None:
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload.update(
            {
                "memory_guard_enabled": True,
                "memory_guard_max_codex_rss_mb": 1000,
                "memory_guard_max_renderer_count": 8,
                "memory_guard_max_node_repl_count": 6,
                "memory_guard_max_mcp_process_count": 10,
                "memory_guard_min_free_percent": 12,
            }
        )
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        self.write_events([self.event()])
        original_collect = resource_guard.collect
        resource_guard.collect = lambda: resource_guard.ResourceSample(
            codex_rss_mb=2500,
            renderer_count=6,
            node_repl_count=4,
            mcp_process_count=6,
            free_percent=20,
        )
        try:
            result = autopilot_bridge.claim(
                self.config,
                lease_seconds=1800,
            )
        finally:
            resource_guard.collect = original_collect

        self.assertFalse(result["dispatch"])
        self.assertEqual(result["status"], "memory_deferred")
        self.assertFalse(
            (self.root / "var" / "autopilot-dispatch.json").exists()
        )
        self.assertTrue(result["resource_guard"]["defer"])

    def test_completed_with_warning_is_success_after_queue_resolves(self) -> None:
        self.write_events([self.event()])
        first = autopilot_bridge.claim(self.config, lease_seconds=1800)
        autopilot_bridge.mark_started(self.config, first["claim_token"])
        self.write_events([])

        result = autopilot_bridge.mark_completed(
            self.config,
            first["claim_token"],
            warning="postflight poll unavailable",
        )

        self.assertEqual(result["status"], "completed_with_warning")
        health = json.loads(
            (self.root / "var" / "autopilot-health.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(health["handoff_count"], 1)
        self.assertEqual(health["error"], "postflight poll unavailable")

    def test_reconcile_completed_requires_durable_resolution(self) -> None:
        database = self.root / "var" / "watcher.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE event_resolutions(
                    event_id TEXT PRIMARY KEY,
                    disposition TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO event_resolutions VALUES(?, 'skip')",
                (self.event()["event_id"],),
            )

        result = autopilot_bridge.reconcile_completed(
            self.config,
            [self.event()["event_id"]],
            warning="recovered postflight state",
        )

        self.assertEqual(result["status"], "completed_with_warning")
        self.assertEqual(result["event_ids"], [self.event()["event_id"]])

    def test_failed_handoff_releases_claim_for_immediate_retry(self) -> None:
        self.write_events([self.event()])
        first = autopilot_bridge.claim(self.config, lease_seconds=1800)

        failed = autopilot_bridge.mark_failed(
            self.config,
            first["claim_token"],
            "synthetic send failure",
        )
        second = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertEqual(failed["released"], [self.event()["event_id"]])
        self.assertTrue(second["dispatch"])


if __name__ == "__main__":
    unittest.main()
