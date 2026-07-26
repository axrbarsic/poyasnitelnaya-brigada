from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import xmention_watcher as watcher
from scripts import render_launchd


class WatcherTests(unittest.TestCase):
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
                    "poll_interval_seconds": 300,
                    "stale_after_seconds": 900,
                    "failure_threshold": 2,
                    "silence_review_seconds": 21600,
                    "watchdog_interval_seconds": 60,
                    "api_base": "https://api.x.com/2",
                    "notifications_enabled": False,
                }
            ),
            encoding="utf-8",
        )
        self.config = watcher.load_config(config_path)
        self.connection = watcher.connect_database(self.config.database)
        self.fixture = watcher.read_json(
            PROJECT_ROOT / "tests" / "fixtures" / "mentions_accumulated.json"
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def insert_direct_event(
        self,
        *,
        event_id: str,
        created_at: str,
        conversation_id: str,
        parent_status_id: str,
        text: str,
        attachments: dict | None = None,
        included_media: list[dict] | None = None,
    ) -> None:
        payload = {
            "id": event_id,
            "author_id": "901",
            "text": text,
            "created_at": created_at,
            "conversation_id": conversation_id,
            "in_reply_to_user_id": self.config.user_id,
            "referenced_tweets": [
                {"type": "replied_to", "id": parent_status_id}
            ],
        }
        if attachments is not None:
            payload["attachments"] = attachments
        if included_media is not None:
            payload["included_media"] = included_media
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
                    created_at,
                    conversation_id,
                    self.config.user_id,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    watcher.isoformat(),
                ),
            )

    def insert_alex_turn_for_chain(self, chain_id: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO conversation_chains(
                    chain_id, root_status_id, provenance,
                    created_at, updated_at
                ) VALUES(?, ?, 'short', ?, ?)
                """,
                (chain_id, chain_id, watcher.isoformat(), watcher.isoformat()),
            )
            self.connection.execute(
                """
                INSERT INTO conversation_turns(
                    status_id, chain_id, actor, author, url, exact_text,
                    observed_at, provenance
                ) VALUES(?, ?, 'alex', 'axrbarsic', ?, 'Prior answer', ?,
                         'live_x_dom')
                """,
                (
                    str(int(chain_id) + 1),
                    chain_id,
                    f"https://x.com/axrbarsic/status/{int(chain_id) + 1}",
                    watcher.isoformat(),
                ),
            )

    def test_replay_detects_accumulated_events_and_deduplicates(self) -> None:
        first = watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="test_fixture",
        )
        second = watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="test_fixture",
        )

        self.assertEqual(first["new_count"], 3)
        self.assertEqual(
            first["new_event_ids"],
            [
                "2080696811623190996",
                "2080775701548916837",
                "2080789442655129702",
            ],
        )
        self.assertEqual(first["since_id"], "2080789442655129702")
        self.assertEqual(second["new_count"], 0)
        self.assertEqual(second["pending_count"], 3)
        wake = watcher.read_json(self.config.wake_file)
        self.assertEqual(wake["pending_count"], 3)
        self.assertTrue(all(event["is_reply"] for event in wake["events"]))

    def test_acknowledge_rejects_direct_reply_without_resolution(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="test_fixture",
        )
        with self.assertRaisesRegex(
            ValueError,
            "require a durable resolve disposition",
        ):
            watcher.acknowledge_events(
                self.config,
                self.connection,
                ["2080696811623190996"],
            )
        state = self.connection.execute(
            "SELECT delivery_state FROM events WHERE event_id = ?",
            ("2080696811623190996",),
        ).fetchone()["delivery_state"]
        self.assertEqual(state, "queued")
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM event_resolutions WHERE event_id = ?",
                ("2080696811623190996",),
            ).fetchone()
        )

    def test_acknowledge_allows_non_direct_queued_event(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="test_fixture",
        )
        with self.connection:
            self.connection.execute(
                """
                UPDATE events
                SET in_reply_to_user_id = 'another-user',
                    delivery_state = 'queued'
                WHERE event_id = ?
                """,
                ("2080696811623190996",),
            )
        result = watcher.acknowledge_events(
            self.config,
            self.connection,
            ["2080696811623190996", "999"],
        )
        self.assertEqual(result["acknowledged"], ["2080696811623190996"])
        self.assertEqual(result["not_queued"], ["999"])

    def test_tracked_conversation_descendant_is_queued_and_protected(
        self,
    ) -> None:
        tracked_id = "2080312847230210375"
        self.insert_alex_turn_for_chain(tracked_id)
        response = {
            "data": [
                {
                    "id": "2081034932135096804",
                    "author_id": "901",
                    "text": "Nested Proton continuation",
                    "created_at": "2026-07-25T15:12:46Z",
                    "conversation_id": tracked_id,
                    "in_reply_to_user_id": "2024119407933534209",
                    "referenced_tweets": [
                        {
                            "type": "replied_to",
                            "id": "2081028119784230951",
                        }
                    ],
                }
            ],
            "includes": {
                "users": [{"id": "901", "username": "target_user"}]
            },
            "meta": {"newest_id": "2081034932135096804"},
        }

        result = watcher.ingest_response(
            self.config,
            self.connection,
            response,
            source="x_api",
        )

        self.assertEqual(result["new_event_ids"], ["2081034932135096804"])
        stored = self.connection.execute(
            "SELECT delivery_state FROM events WHERE event_id = ?",
            ("2081034932135096804",),
        ).fetchone()
        self.assertEqual(stored["delivery_state"], "queued")
        with self.assertRaisesRegex(
            ValueError,
            "require a durable resolve disposition",
        ):
            watcher.acknowledge_events(
                self.config,
                self.connection,
                ["2081034932135096804"],
            )

    def test_record_failure_and_watchdog_threshold(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="test_fixture",
        )
        watcher.record_failure(
            self.config,
            self.connection,
            RuntimeError("synthetic failure"),
            source="test",
            started_at=watcher.isoformat(),
        )
        watcher.record_failure(
            self.config,
            self.connection,
            RuntimeError("synthetic failure"),
            source="test",
            started_at=watcher.isoformat(),
        )
        result = watcher.evaluate_health(self.config)
        self.assertEqual(result["status"], "failing")
        self.assertFalse(result["healthy"])

    def test_missing_token_is_recorded_as_health_failure(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Missing X API bearer token"):
                watcher.poll_live(self.config, self.connection)
        health = watcher.read_json(self.config.health_file)
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["consecutive_failures"], 1)
        run = self.connection.execute(
            "SELECT status, error_class FROM poll_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(run["status"], "failure")
        self.assertEqual(run["error_class"], "RuntimeError")

    def test_failure_message_redacts_bearer_and_environment_token(self) -> None:
        secret = "secret-value-that-must-not-be-written"
        with mock.patch.dict(os.environ, {"X_BEARER_TOKEN": secret}, clear=True):
            watcher.record_failure(
                self.config,
                self.connection,
                RuntimeError(f"Authorization: Bearer {secret}"),
                source="test",
                started_at=watcher.isoformat(),
            )
        health = watcher.read_json(self.config.health_file)
        self.assertNotIn(secret, health["last_error_message"])
        self.assertIn("[REDACTED]", health["last_error_message"])

    def test_success_clears_previous_error(self) -> None:
        watcher.record_failure(
            self.config,
            self.connection,
            RuntimeError("synthetic failure"),
            source="test",
            started_at=watcher.isoformat(),
        )
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="test_fixture",
        )
        health = watcher.read_json(self.config.health_file)
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertIsNone(health["last_error_class"])
        self.assertIsNone(health["last_error_message"])

    def test_watchdog_detects_stale_success(self) -> None:
        stale_time = datetime.now(timezone.utc) - timedelta(seconds=901)
        watcher.atomic_write_json(
            self.config.health_file,
            {
                "last_success_at": watcher.isoformat(stale_time),
                "last_new_event_at": watcher.isoformat(stale_time),
                "consecutive_failures": 0,
                "queued_events": 0,
            },
        )
        result = watcher.evaluate_health(self.config)
        self.assertEqual(result["status"], "stale")
        self.assertFalse(result["healthy"])

    def test_watchdog_reports_depleted_x_api_credits(self) -> None:
        watcher.atomic_write_json(
            self.config.health_file,
            {
                "first_success_at": None,
                "last_success_at": None,
                "last_new_event_at": None,
                "consecutive_failures": 1,
                "queued_events": 0,
                "last_error_class": "RuntimeError",
                "last_error_message": "X API HTTP 402 (Payment Required)",
            },
        )
        result = watcher.evaluate_health(self.config)
        self.assertEqual(result["status"], "billing_blocked")
        self.assertEqual(result["reason"], "x_api_credits_depleted")
        self.assertFalse(result["healthy"])

    def test_watchdog_reviews_silence_when_no_event_has_ever_arrived(self) -> None:
        first_success = datetime.now(timezone.utc) - timedelta(seconds=21601)
        watcher.atomic_write_json(
            self.config.health_file,
            {
                "first_success_at": watcher.isoformat(first_success),
                "last_success_at": watcher.isoformat(),
                "last_new_event_at": None,
                "consecutive_failures": 0,
                "queued_events": 0,
            },
        )
        result = watcher.evaluate_health(self.config)
        self.assertEqual(result["status"], "healthy_silence_review")
        self.assertTrue(result["healthy"])

    def test_live_poll_paginates_and_rejects_api_errors(self) -> None:
        watcher.set_meta(self.connection, "first_success_at", watcher.isoformat())
        pages = [
            {
                "data": [self.fixture["data"][0]],
                "includes": {"users": [self.fixture["includes"]["users"][0]]},
                "meta": {"newest_id": "2080696811623190996", "next_token": "next"},
            },
            {
                "data": [self.fixture["data"][1]],
                "includes": {"users": [self.fixture["includes"]["users"][1]]},
                "meta": {"newest_id": "2080775701548916837"},
            },
        ]
        requested_urls: list[str] = []

        def fetch(url: str, token: str, timeout: int) -> dict:
            requested_urls.append(url)
            self.assertEqual(token, "test-token")
            self.assertEqual(timeout, 30)
            return pages[len(requested_urls) - 1]

        previous = os.environ.get("X_BEARER_TOKEN")
        os.environ["X_BEARER_TOKEN"] = "test-token"
        try:
            result = watcher.poll_live(self.config, self.connection, fetch=fetch)
        finally:
            if previous is None:
                os.environ.pop("X_BEARER_TOKEN", None)
            else:
                os.environ["X_BEARER_TOKEN"] = previous

        self.assertEqual(result["new_count"], 2)
        self.assertEqual(len(requested_urls), 2)
        self.assertIn("pagination_token=next", requested_urls[1])
        query = watcher.urllib.parse.parse_qs(
            watcher.urllib.parse.urlsplit(requested_urls[0]).query
        )
        self.assertIn("attachments", query["tweet.fields"][0])
        self.assertIn(
            "attachments.media_keys",
            query["expansions"][0],
        )
        self.assertIn("alt_text", query["media.fields"][0])

        def error_fetch(url: str, token: str, timeout: int) -> dict:
            return {"errors": [{"title": "Synthetic API error"}]}

        previous = os.environ.get("X_BEARER_TOKEN")
        os.environ["X_BEARER_TOKEN"] = "test-token"
        try:
            with self.assertRaisesRegex(RuntimeError, "X API returned errors"):
                watcher.poll_live(self.config, self.connection, fetch=error_fetch)
        finally:
            if previous is None:
                os.environ.pop("X_BEARER_TOKEN", None)
            else:
                os.environ["X_BEARER_TOKEN"] = previous

    def test_first_live_poll_queues_direct_replies_for_initial_audit(self) -> None:
        with mock.patch.dict(os.environ, {"X_BEARER_TOKEN": "test-token"}, clear=True):
            result = watcher.poll_live(
                self.config,
                self.connection,
                fetch=lambda url, token, timeout: self.fixture,
            )
        self.assertTrue(result["bootstrap"])
        self.assertEqual(result["observed_count"], 3)
        self.assertEqual(result["new_count"], 3)
        self.assertEqual(result["pending_count"], 3)
        states = {
            row["delivery_state"]
            for row in self.connection.execute(
                "SELECT delivery_state FROM events"
            ).fetchall()
        }
        self.assertEqual(states, {"queued"})
        audit = watcher.initial_audit_status(self.connection)
        self.assertIsNotNone(audit["started_at"])
        self.assertFalse(audit["complete"])
        self.assertEqual(audit["pending_events"], 3)

    def test_initial_audit_next_groups_newest_conversations_with_exact_text(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        watcher.set_meta(self.connection, "configured_user_id", self.config.user_id)
        result = watcher.initial_audit_next(
            self.connection,
            conversation_limit=2,
        )
        self.assertEqual(result["pending_events"], 3)
        self.assertEqual(len(result["groups"]), 2)
        newest = result["groups"][0]
        self.assertEqual(newest["conversation_id"], "2080789442655129702")
        self.assertEqual(newest["queued_count"], 1)
        event = newest["events"][0]
        self.assertEqual(event["event_id"], "2080789442655129702")
        self.assertEqual(event["exact_text"], "Accumulated follow-up fixture 3")
        self.assertEqual(
            event["replied_to_status_id"],
            "2080762970989048149",
        )
        self.assertEqual(event["text_urls"], [])
        self.assertEqual(event["media_keys"], [])
        self.assertEqual(event["included_media"], [])

    def test_initial_audit_next_rejects_non_positive_limit(self) -> None:
        watcher.set_meta(self.connection, "configured_user_id", self.config.user_id)
        with self.assertRaisesRegex(ValueError, "must be positive"):
            watcher.initial_audit_next(
                self.connection,
                conversation_limit=0,
            )

    def test_initial_audit_expire_applies_requested_start_window(self) -> None:
        fixed_now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
        watcher.set_meta(
            self.connection,
            "configured_user_id",
            self.config.user_id,
        )
        watcher.set_meta(
            self.connection,
            "initial_audit_started_at",
            watcher.isoformat(fixed_now),
        )
        watcher.set_meta(
            self.connection,
            "initial_audit_cycle_started_at",
            watcher.isoformat(fixed_now),
        )
        self.insert_direct_event(
            event_id="1001",
            created_at="2026-07-24T23:59:59Z",
            conversation_id="1000",
            parent_status_id="999",
            text="Exact old reply",
        )
        self.insert_direct_event(
            event_id="1002",
            created_at="2026-07-25T00:00:00Z",
            conversation_id="1000",
            parent_status_id="999",
            text="Exactly at cutoff",
        )
        self.insert_direct_event(
            event_id="1003",
            created_at="2026-07-25T11:59:59Z",
            conversation_id="1000",
            parent_status_id="999",
            text="Inside window",
        )
        self.insert_direct_event(
            event_id="1004",
            created_at="2026-07-25T12:00:01Z",
            conversation_id="1000",
            parent_status_id="999",
            text="Future timestamp",
        )
        self.connection.execute(
            """
            INSERT INTO events(
                event_id, author_id, username, created_at, conversation_id,
                in_reply_to_user_id, is_reply, payload_json, first_seen_at,
                delivery_state
            ) VALUES('1005', '901', 'target_user', 'not-a-time', '1000',
                     ?, 1, '{}', ?, 'queued')
            """,
            (self.config.user_id, watcher.isoformat()),
        )
        self.connection.commit()

        short_window = watcher.expire_initial_audit_events(
            self.config,
            self.connection,
            response_window_hours=3,
            now=fixed_now,
            dry_run=True,
        )
        self.assertEqual(short_window["cutoff"], "2026-07-25T09:00:00Z")
        self.assertEqual(short_window["expired"], 2)

        dry_run = watcher.expire_initial_audit_events(
            self.config,
            self.connection,
            response_window_hours=12,
            now=fixed_now,
            dry_run=True,
        )
        self.assertEqual(dry_run["status"], "dry_run")
        self.assertEqual(dry_run["expired"], 1)
        self.assertEqual(dry_run["within_window"], 2)
        self.assertEqual(dry_run["future_timestamp"], 1)
        self.assertEqual(dry_run["invalid_timestamp"], 1)
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM event_resolutions WHERE event_id = '1001'"
            ).fetchone()
        )

        applied = watcher.expire_initial_audit_events(
            self.config,
            self.connection,
            response_window_hours=12,
            now=fixed_now,
        )
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["expired"], 1)
        self.assertEqual(applied["pending_events"], 4)
        resolution = self.connection.execute(
            "SELECT * FROM event_resolutions WHERE event_id = '1001'"
        ).fetchone()
        self.assertEqual(resolution["disposition"], "skip")
        self.assertEqual(
            resolution["stance_detail"],
            "outside_requested_start_window_not_classified",
        )
        turn = self.connection.execute(
            "SELECT * FROM conversation_turns WHERE status_id = '1001'"
        ).fetchone()
        self.assertEqual(turn["parent_status_id"], "999")
        self.assertEqual(turn["exact_text"], "Exact old reply")
        self.assertEqual(
            turn["provenance"],
            watcher.INITIAL_AUDIT_EXPIRY_PROVENANCE,
        )

        repeated = watcher.expire_initial_audit_events(
            self.config,
            self.connection,
            response_window_hours=12,
            now=fixed_now,
        )
        self.assertEqual(repeated["expired"], 0)
        self.assertEqual(repeated["pending_events"], 4)
        revision_count = self.connection.execute(
            "SELECT COUNT(*) AS count FROM event_resolution_revisions"
        ).fetchone()["count"]
        self.assertEqual(revision_count, 0)
        with self.assertRaisesRegex(ValueError, "positive number"):
            watcher.expire_initial_audit_events(
                self.config,
                self.connection,
                response_window_hours=0,
                now=fixed_now,
            )
        with self.assertRaisesRegex(ValueError, "different fixed cutoff"):
            watcher.expire_initial_audit_events(
                self.config,
                self.connection,
                response_window_hours=3,
                now=fixed_now,
            )

    def test_initial_audit_expire_preserves_media_and_fails_closed(self) -> None:
        fixed_now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
        watcher.set_meta(
            self.connection,
            "configured_user_id",
            self.config.user_id,
        )
        watcher.set_meta(
            self.connection,
            "initial_audit_started_at",
            watcher.isoformat(fixed_now),
        )
        watcher.set_meta(
            self.connection,
            "initial_audit_cycle_started_at",
            watcher.isoformat(fixed_now),
        )
        media = [
            {
                "media_key": "3_exact",
                "type": "photo",
                "url": "https://pbs.twimg.com/media/exact.jpg",
                "alt_text": "Exact stored alt text",
            }
        ]
        self.insert_direct_event(
            event_id="2001",
            created_at="2026-07-24T23:00:00Z",
            conversation_id="2000",
            parent_status_id="1999",
            text="",
            attachments={"media_keys": ["3_exact"]},
            included_media=media,
        )
        self.insert_direct_event(
            event_id="2002",
            created_at="2026-07-24T23:00:00Z",
            conversation_id="2000",
            parent_status_id="1999",
            text="Missing expanded media",
            attachments={"media_keys": ["3_missing"]},
        )
        self.insert_direct_event(
            event_id="2003",
            created_at="2026-07-24T23:00:00Z",
            conversation_id="2000",
            parent_status_id="1999",
            text="Partially expanded media",
            attachments={"media_keys": ["3_exact", "3_missing"]},
            included_media=media,
        )

        result = watcher.expire_initial_audit_events(
            self.config,
            self.connection,
            response_window_hours=12,
            now=fixed_now,
        )
        self.assertEqual(result["expired"], 1)
        self.assertEqual(result["invalid_stored_event"], 2)
        self.assertEqual(result["pending_events"], 2)
        stored_media = self.connection.execute(
            "SELECT media_json FROM conversation_turns WHERE status_id = '2001'"
        ).fetchone()["media_json"]
        self.assertEqual(json.loads(stored_media), media)
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM conversation_turns WHERE status_id = '2002'"
            ).fetchone()
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM event_resolutions WHERE event_id = '2002'"
            ).fetchone()
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM event_resolutions WHERE event_id = '2003'"
            ).fetchone()
        )

    def test_initial_audit_expire_requires_explicit_audit_start(self) -> None:
        fixed_now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
        watcher.set_meta(
            self.connection,
            "configured_user_id",
            self.config.user_id,
        )
        self.insert_direct_event(
            event_id="3001",
            created_at="2026-07-24T23:00:00Z",
            conversation_id="3000",
            parent_status_id="2999",
            text="Must remain pending",
        )
        with self.assertRaisesRegex(ValueError, "has not started"):
            watcher.expire_initial_audit_events(
                self.config,
                self.connection,
                response_window_hours=12,
                now=fixed_now,
            )
        watcher.set_meta(
            self.connection,
            "initial_audit_started_at",
            watcher.isoformat(fixed_now),
        )
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "Response cycle"):
            watcher.expire_initial_audit_events(
                self.config,
                self.connection,
                response_window_hours=12,
                now=fixed_now,
            )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM event_resolutions WHERE event_id = '3001'"
            ).fetchone()
        )

    def test_media_expansion_is_preserved_for_audit_handoff(self) -> None:
        payload = json.loads(json.dumps(self.fixture))
        target = payload["data"][2]
        target["attachments"] = {"media_keys": ["3_test"]}
        payload["includes"]["media"] = [
            {
                "media_key": "3_test",
                "type": "photo",
                "url": "https://pbs.twimg.com/media/test.jpg",
                "alt_text": "A reaction image",
                "width": 1200,
                "height": 800,
            }
        ]
        watcher.ingest_response(
            self.config,
            self.connection,
            payload,
            source="x_api",
        )
        watcher.set_meta(self.connection, "configured_user_id", self.config.user_id)
        result = watcher.initial_audit_next(
            self.connection,
            conversation_limit=1,
        )
        event = result["groups"][0]["events"][0]
        self.assertEqual(event["media_keys"], ["3_test"])
        self.assertEqual(
            event["included_media"],
            payload["includes"]["media"],
        )

    def test_non_direct_mentions_are_stored_but_not_queued(self) -> None:
        watcher.set_meta(self.connection, "first_success_at", watcher.isoformat())
        payload = json.loads(json.dumps(self.fixture))
        payload["data"][0]["in_reply_to_user_id"] = "another-user"
        result = watcher.ingest_response(
            self.config,
            self.connection,
            payload,
            source="x_api",
        )
        self.assertEqual(result["observed_count"], 3)
        self.assertEqual(result["new_count"], 2)
        state = self.connection.execute(
            "SELECT delivery_state FROM events WHERE event_id = ?",
            ("2080696811623190996",),
        ).fetchone()["delivery_state"]
        self.assertEqual(state, "ignored")

    def test_mandatory_mode_queues_non_direct_mention_reply(self) -> None:
        watcher.set_meta(self.connection, "first_success_at", watcher.isoformat())
        payload = json.loads(json.dumps(self.fixture))
        payload["data"][0]["in_reply_to_user_id"] = "another-user"
        strict_config = replace(
            self.config,
            mandatory_response_mode=True,
        )

        result = watcher.ingest_response(
            strict_config,
            self.connection,
            payload,
            source="x_api",
        )

        self.assertEqual(result["new_count"], 3)
        state = self.connection.execute(
            "SELECT delivery_state FROM events WHERE event_id = ?",
            ("2080696811623190996",),
        ).fetchone()["delivery_state"]
        self.assertEqual(state, "queued")

    def test_mandatory_mode_never_queues_self_authored_reply(self) -> None:
        watcher.set_meta(self.connection, "first_success_at", watcher.isoformat())
        payload = json.loads(json.dumps(self.fixture))
        event = payload["data"][0]
        event_id = event["id"]
        event["author_id"] = self.config.user_id
        event["in_reply_to_user_id"] = self.config.user_id
        payload["data"] = [event]
        payload["includes"]["users"].append(
            {"id": self.config.user_id, "username": "axrbarsic"}
        )
        payload["meta"]["newest_id"] = event_id
        strict_config = replace(
            self.config,
            mandatory_response_mode=True,
        )

        result = watcher.ingest_response(
            strict_config,
            self.connection,
            payload,
            source="x_api",
        )

        self.assertEqual(result["new_count"], 0)
        self.assertEqual(result["self_authored_event_ids"], [event_id])
        self.assertEqual(result["pending_count"], 0)
        stored = self.connection.execute(
            """
            SELECT author_id, delivery_state
            FROM events
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        self.assertEqual(stored["author_id"], self.config.user_id)
        self.assertEqual(stored["delivery_state"], "self_authored")

    def test_self_authored_reconcile_repairs_legacy_queue_idempotently(
        self,
    ) -> None:
        event_id = "2081490627879997803"
        self.insert_direct_event(
            event_id=event_id,
            created_at="2026-07-26T21:23:32Z",
            conversation_id="2081434236486029792",
            parent_status_id="2081434236486029792",
            text="Manual supplement",
        )
        with self.connection:
            self.connection.execute(
                """
                UPDATE events
                SET author_id = ?, username = 'axrbarsic'
                WHERE event_id = ?
                """,
                (self.config.user_id, event_id),
            )
        watcher.refresh_wake_file(self.config, self.connection)

        first = watcher.reconcile_self_authored_events(
            self.config,
            self.connection,
        )
        second = watcher.reconcile_self_authored_events(
            self.config,
            self.connection,
        )
        audit = watcher.start_initial_audit(self.config, self.connection)

        self.assertEqual(first["reclassified_event_ids"], [event_id])
        self.assertEqual(first["pending_count"], 0)
        self.assertEqual(second["reclassified_event_ids"], [])
        self.assertEqual(audit["requeued"], 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT delivery_state FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()["delivery_state"],
            "self_authored",
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM event_resolutions WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        )

    def test_baseline_existing_queue_preserves_cursor(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="test_fixture",
        )
        previous_cursor = watcher.get_meta(self.connection, "since_id")
        result = watcher.baseline_existing_queue(self.config, self.connection)
        self.assertEqual(result["baselined_count"], 3)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["since_id"], previous_cursor)

    def test_history_import_show_and_export_are_idempotent(self) -> None:
        history = {
            "chain_id": "2080789442655129702",
            "root_status_id": "2080789442655129702",
            "provenance": "short",
            "ledger_reference": "run-ledger.jsonl",
            "turns": [
                {
                    "status_id": "2080789442655129702",
                    "actor": "user",
                    "author": "ZamirZakyeV",
                    "url": "https://x.com/ZamirZakyeV/status/2080789442655129702",
                    "exact_text": "Follow-up claim",
                    "created_at": "2026-07-24T22:14:00.000Z",
                },
                {
                    "status_id": "2080800363100115226",
                    "parent_status_id": "2080789442655129702",
                    "actor": "alex",
                    "author": "axrbarsic",
                    "url": "https://x.com/axrbarsic/status/2080800363100115226",
                    "exact_text": "Verified response\nSecond line\n",
                    "created_at": "2026-07-24T23:10:00.000Z",
                    "provenance": "short",
                    "source_urls": ["https://www.osce.org/cio/141486"],
                },
            ],
        }
        first = watcher.import_history_snapshot(self.connection, history)
        second = watcher.import_history_snapshot(self.connection, history)
        self.assertEqual(first["inserted_turns"], 2)
        self.assertEqual(first["inserted_sources"], 1)
        self.assertEqual(second["inserted_turns"], 0)
        self.assertEqual(second["inserted_sources"], 0)

        chain = watcher.history_chain_for_status(
            self.connection,
            "2080800363100115226",
        )
        self.assertEqual(chain["chain_id"], "2080789442655129702")
        self.assertEqual(len(chain["turns"]), 2)
        self.assertEqual(
            chain["turns"][1]["source_urls"],
            ["https://www.osce.org/cio/141486"],
        )
        self.assertEqual(
            chain["turns"][1]["exact_text"],
            "Verified response\nSecond line\n",
        )

        output = self.root / "history-backup.jsonl"
        result = watcher.export_history(self.connection, output)
        self.assertEqual(result["chains"], 1)
        exported = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exported["turns"], chain["turns"])

    def test_history_import_rejects_rewriting_existing_turn(self) -> None:
        history = {
            "chain_id": "2080789442655129702",
            "root_status_id": "2080789442655129702",
            "provenance": "short",
            "turns": [
                {
                    "status_id": "2080789442655129702",
                    "actor": "user",
                    "author": "ZamirZakyeV",
                    "url": "https://x.com/ZamirZakyeV/status/2080789442655129702",
                    "exact_text": "Original text",
                }
            ],
        }
        watcher.import_history_snapshot(self.connection, history)
        changed = json.loads(json.dumps(history))
        changed["turns"][0]["exact_text"] = "Rewritten text"
        with self.assertRaisesRegex(ValueError, "Append-only history conflict"):
            watcher.import_history_snapshot(self.connection, changed)
        stored = watcher.history_chain_for_status(
            self.connection,
            "2080789442655129702",
        )
        self.assertEqual(stored["turns"][0]["exact_text"], "Original text")

    def test_history_import_preserves_media_only_turn(self) -> None:
        history = {
            "chain_id": "2080651494051737864",
            "root_status_id": "2080651494051737864",
            "provenance": "short",
            "turns": [
                {
                    "status_id": "2080715451961675918",
                    "actor": "user",
                    "url": "https://x.com/Wallander_C/status/2080715451961675918",
                    "exact_text": "",
                    "media": [
                        {
                            "url": "https://pbs.twimg.com/media/example.jpg",
                            "stance": "opposing",
                            "confidence": "high",
                        }
                    ],
                }
            ],
        }
        watcher.import_history_snapshot(self.connection, history)
        stored = watcher.history_chain_for_status(
            self.connection,
            "2080715451961675918",
        )
        self.assertEqual(stored["turns"][0]["exact_text"], "")
        self.assertEqual(stored["turns"][0]["media"][0]["stance"], "opposing")

    def test_history_import_accepts_jsonl_file(self) -> None:
        records = [
            {
                "chain_id": "2080789442655129702",
                "root_status_id": "2080789442655129702",
                "provenance": "short",
                "turns": [
                    {
                        "status_id": "2080789442655129702",
                        "actor": "user",
                        "url": "https://x.com/i/status/2080789442655129702",
                        "exact_text": "One",
                    }
                ],
            },
            {
                "chain_id": "2080775701548916837",
                "root_status_id": "2080775701548916837",
                "provenance": "pro",
                "conversation_url": "https://chatgpt.com/c/example",
                "turns": [
                    {
                        "status_id": "2080775701548916837",
                        "actor": "user",
                        "url": "https://x.com/i/status/2080775701548916837",
                        "exact_text": "Two",
                    }
                ],
            },
        ]
        path = self.root / "history.jsonl"
        path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        result = watcher.import_history_file(self.connection, path)
        self.assertEqual(result["records"], 2)
        self.assertEqual(result["inserted_turns"], 2)
        self.assertEqual(watcher.history_status(self.connection)["chains"], 2)

    def test_history_import_accepts_flat_initial_audit_turn(self) -> None:
        path = self.root / "flat-history.jsonl"
        event_record = {
            "record_type": "initial_audit_event_turn",
            "chain_id": "2080651494051737864",
            "conversation_root_id": "2080651494051737864",
            "parent_status_id": "2080680475404742792",
            "status_id": "2080755430825861144",
            "actor": "user",
            "author": "@green4sky",
            "url": "https://x.com/green4sky/status/2080755430825861144",
            "exact_text": "Карма.",
            "posted_at": "2026-07-24T20:42:07.000Z",
            "provenance": "live_x_dom_initial_audit_backfill",
            "media_json": [
                {
                    "url": "https://pbs.twimg.com/media/example.jpg",
                    "stance": "opposing",
                }
            ],
            "sources": ["https://example.com/context"],
        }
        alex_record = {
            "record_type": "initial_audit_alex_turn",
            "chain_id": "2080651494051737864",
            "conversation_root_id": "2080651494051737864",
            "parent_status_id": "2080755430825861144",
            "status_id": "2080858504571605419",
            "actor": "axrbarsic",
            "author": "@axrbarsic",
            "url": "https://x.com/axrbarsic/status/2080858504571605419",
            "exact_text": "Проверенный ответ.",
            "posted_at": "2026-07-25T03:31:42.000Z",
            "provenance": "self_authored_short_sol",
            "media_json": [],
            "source_urls": ["https://example.com/primary-source"],
        }
        path.write_text(
            "\n".join(
                json.dumps(record, ensure_ascii=False)
                for record in (event_record, alex_record)
            )
            + "\n",
            encoding="utf-8",
        )
        result = watcher.import_history_file(self.connection, path)
        self.assertEqual(result["records"], 2)
        self.assertEqual(result["inserted_turns"], 2)
        self.assertEqual(result["inserted_sources"], 2)
        stored = watcher.history_chain_for_status(
            self.connection,
            "2080755430825861144",
        )
        self.assertEqual(stored["turns"][0]["exact_text"], "Карма.")
        self.assertEqual(
            stored["turns"][0]["media"][0]["stance"],
            "opposing",
        )
        self.assertEqual(stored["turns"][1]["actor"], "alex")
        self.assertEqual(
            stored["turns"][1]["parent_status_id"],
            "2080755430825861144",
        )

    def test_flat_audit_turn_inherits_existing_pro_chain_provenance(self) -> None:
        watcher.import_history_snapshot(
            self.connection,
            {
                "chain_id": "2080775701548916837",
                "root_status_id": "2080775701548916837",
                "provenance": "pro",
                "chatgpt_conversation_url": "https://chatgpt.com/c/example",
                "turns": [
                    {
                        "status_id": "2080775701548916837",
                        "actor": "user",
                        "url": (
                            "https://x.com/example/status/"
                            "2080775701548916837"
                        ),
                        "exact_text": "Original Pro target",
                    }
                ],
            },
        )
        path = self.root / "flat-pro-history.jsonl"
        path.write_text(
            json.dumps(
                {
                    "record_type": "initial_audit_event_turn",
                    "chain_id": "2080775701548916837",
                    "conversation_root_id": "2080775701548916837",
                    "parent_status_id": "2080775701548916837",
                    "status_id": "2080789442655129702",
                    "actor": "user",
                    "url": (
                        "https://x.com/example/status/"
                        "2080789442655129702"
                    ),
                    "exact_text": "Follow-up to Pro",
                    "media_json": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        watcher.import_history_file(self.connection, path)
        chain = watcher.history_chain_for_status(
            self.connection,
            "2080789442655129702",
        )
        self.assertEqual(chain["provenance"], "pro")

    def test_history_import_applies_append_only_provenance_correction(self) -> None:
        records = [
            {
                "snapshot_type": "pre_publication",
                "conversation_root_id": "2080651494051737864",
                "turns": [
                    {
                        "status_id": "2080680475404742792",
                        "actor": "axrbarsic",
                        "url": "https://x.com/axrbarsic/status/2080680475404742792",
                        "exact_text": "Self-authored reply",
                        "provenance": "pro",
                    }
                ],
            },
            {
                "snapshot_type": "provenance_correction",
                "conversation_root_id": "2080651494051737864",
                "corrected_status_id": "2080680475404742792",
                "corrected_provenance": "short",
            },
        ]
        path = self.root / "corrected-history.jsonl"
        path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        result = watcher.import_history_file(self.connection, path)
        self.assertEqual(result["records"], 1)
        self.assertEqual(result["skipped_metadata_records"], 1)
        chain = watcher.history_chain_for_status(
            self.connection,
            "2080680475404742792",
        )
        self.assertEqual(chain["provenance"], "short")
        self.assertEqual(chain["turns"][0]["provenance"], "short")

    def test_history_import_applies_chain_provenance_correction(self) -> None:
        chain_id = "2080312847230210375"
        watcher.import_history_snapshot(
            self.connection,
            {
                "chain_id": chain_id,
                "root_status_id": chain_id,
                "provenance": "short",
                "turns": [
                    {
                        "status_id": chain_id,
                        "actor": "alex",
                        "url": f"https://x.com/axrbarsic/status/{chain_id}",
                        "exact_text": "Original article",
                    }
                ],
            },
        )
        records = [
            {
                "record_type": "initial_audit_event_turn",
                "chain_id": chain_id,
                "conversation_root_id": chain_id,
                "chain_provenance": "mixed",
                "parent_status_id": chain_id,
                "status_id": "2080671383051268325",
                "actor": "user",
                "url": (
                    "https://x.com/example/status/"
                    "2080671383051268325"
                ),
                "exact_text": "Nested science reply",
                "media_json": [],
            },
            {
                "snapshot_type": "chain_provenance_correction",
                "chain_id": chain_id,
                "corrected_provenance": "short",
                "correction_reason": (
                    "Existing chain provenance is authoritative"
                ),
            },
        ]
        path = self.root / "chain-corrected-history.jsonl"
        path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        result = watcher.import_history_file(self.connection, path)
        self.assertEqual(result["records"], 1)
        self.assertEqual(result["skipped_metadata_records"], 1)
        chain = watcher.history_chain_for_status(
            self.connection,
            "2080671383051268325",
        )
        self.assertEqual(chain["provenance"], "short")
        self.assertEqual(chain["turns"][-1]["exact_text"], "Nested science reply")

    def test_history_file_import_rolls_back_every_record_on_conflict(self) -> None:
        existing = {
            "chain_id": "2080789442655129702",
            "root_status_id": "2080789442655129702",
            "provenance": "short",
            "turns": [
                {
                    "status_id": "2080789442655129702",
                    "actor": "user",
                    "url": "https://x.com/i/status/2080789442655129702",
                    "exact_text": "Original",
                }
            ],
        }
        watcher.import_history_snapshot(self.connection, existing)
        records = [
            {
                "chain_id": "2080775701548916837",
                "root_status_id": "2080775701548916837",
                "provenance": "short",
                "turns": [
                    {
                        "status_id": "2080775701548916837",
                        "actor": "user",
                        "url": "https://x.com/i/status/2080775701548916837",
                        "exact_text": "New chain",
                    }
                ],
            },
            {
                **existing,
                "turns": [
                    {
                        **existing["turns"][0],
                        "exact_text": "Conflicting rewrite",
                    }
                ],
            },
        ]
        path = self.root / "atomic-history.jsonl"
        path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Append-only history conflict"):
            watcher.import_history_file(self.connection, path)
        self.assertEqual(watcher.history_status(self.connection)["chains"], 1)

    def test_history_lookup_prefers_exact_turn_over_other_chain_id(self) -> None:
        first = {
            "chain_id": "2080789442655129702",
            "root_status_id": "2080789442655129702",
            "provenance": "short",
            "turns": [
                {
                    "status_id": "2080775701548916837",
                    "actor": "user",
                    "url": "https://x.com/i/status/2080775701548916837",
                    "exact_text": "Turn match",
                }
            ],
        }
        second = {
            "chain_id": "2080775701548916837",
            "root_status_id": "2080770003968614675",
            "provenance": "short",
            "turns": [
                {
                    "status_id": "2080770003968614675",
                    "actor": "user",
                    "url": "https://x.com/i/status/2080770003968614675",
                    "exact_text": "Other chain",
                }
            ],
        }
        watcher.import_history_snapshot(self.connection, first)
        watcher.import_history_snapshot(self.connection, second)
        result = watcher.history_chain_for_status(
            self.connection,
            "2080775701548916837",
        )
        self.assertEqual(result["chain_id"], "2080789442655129702")

    def test_foreign_keys_reject_source_without_turn(self) -> None:
        enabled = self.connection.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(enabled, 1)
        with self.assertRaises(watcher.sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO conversation_sources(status_id, source_url)
                VALUES('2080789442655129702', 'https://example.com/source')
                """
            )

    def test_initial_audit_requeues_baseline_and_requires_resolutions(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        watcher.baseline_existing_queue(self.config, self.connection)
        started = watcher.start_initial_audit(self.config, self.connection)
        self.assertEqual(started["requeued"], 3)
        self.assertIsNotNone(started["cycle_started_at"])
        self.assertIsNone(started["cycle_expiry_as_of"])
        with self.assertRaisesRegex(ValueError, "unresolved direct events"):
            watcher.complete_initial_audit(self.config, self.connection)
        event_ids = [
            "2080696811623190996",
            "2080775701548916837",
            "2080789442655129702",
        ]
        for index, event_id in enumerate(event_ids):
            event = self.connection.execute(
                "SELECT payload_json, username FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            payload = json.loads(event["payload_json"])
            watcher.import_history_snapshot(
                self.connection,
                {
                    "chain_id": str(payload["conversation_id"]),
                    "root_status_id": str(payload["conversation_id"]),
                    "provenance": "short",
                    "turns": [
                        {
                            "status_id": event_id,
                            "actor": "user",
                            "author": event["username"],
                            "url": (
                                f"https://x.com/{event['username']}/status/{event_id}"
                            ),
                            "exact_text": payload["text"],
                        }
                    ],
                },
            )
            watcher.resolve_event(
                self.config,
                self.connection,
                event_id,
                disposition="skip",
                reason="reviewed_test_fixture",
                reply_url=None,
                stance="supportive" if index == 0 else "neutral",
                stance_detail=(
                    "supportive_reaction"
                    if index == 0
                    else "neutral_reviewed_fixture"
                ),
                confidence="high",
                media_meaning="supportive reaction" if index == 0 else None,
                evidence=["live parent", "OCR"] if index == 0 else ["live parent"],
            )
        resolution = self.connection.execute(
            "SELECT * FROM event_resolutions WHERE event_id = ?",
            (event_ids[0],),
        ).fetchone()
        self.assertEqual(resolution["stance"], "supportive")
        self.assertEqual(resolution["stance_detail"], "supportive_reaction")
        self.assertEqual(resolution["confidence"], "high")
        self.assertEqual(
            json.loads(resolution["evidence_json"]),
            ["OCR", "live parent"],
        )
        completed = watcher.complete_initial_audit(self.config, self.connection)
        self.assertTrue(completed["complete"])
        self.assertEqual(completed["pending_events"], 0)
        self.assertEqual(
            watcher.read_json(self.config.health_file)["queued_events"],
            0,
        )
        output = self.root / "event-resolutions.jsonl"
        exported = watcher.export_audit_resolutions(self.connection, output)
        self.assertEqual(exported["resolutions"], 3)
        records = [
            json.loads(line)
            for line in output.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(records[0]["record_type"], "initial_audit_meta")
        self.assertEqual(records[1]["record_type"], "event_resolution")
        self.assertEqual(records[1]["stance_detail"], "supportive_reaction")
        self.assertNotIn("payload_json", records[1])

    def test_memory_audit_waits_for_valid_official_archive(self) -> None:
        completed = watcher.complete_initial_audit(
            self.config,
            self.connection,
        )
        self.assertTrue(completed["complete"])
        pending = watcher.memory_audit(self.config, self.connection)
        self.assertTrue(pending["current_memory_ok"])
        self.assertFalse(pending["archive_ready"])
        self.assertFalse(pending["final_complete"])
        self.assertEqual(pending["status"], "archive_pending")

        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO archive_imports(
                    archive_fingerprint, account_user_id, username,
                    source_name, imported_at, post_count, inserted_post_count
                ) VALUES(?, ?, 'axrbarsic', 'archive.zip', ?, 1, 1)
                """,
                (
                    "a" * 64,
                    self.config.user_id,
                    watcher.isoformat(),
                ),
            )
            import_id = int(cursor.lastrowid)
            self.connection.execute(
                """
                INSERT INTO archive_account_aliases(
                    account_user_id, username, first_import_id, last_import_id
                ) VALUES(?, 'axrbarsic', ?, ?)
                """,
                (self.config.user_id, import_id, import_id),
            )
            self.connection.execute(
                """
                INSERT INTO archive_posts(
                    status_id, first_import_id, author_id,
                    username_at_import, posted_at, exact_text,
                    canonical_url, source_member, payload_sha256
                ) VALUES(
                    '100', ?, ?, 'axrbarsic', ?, 'Archived post',
                    'https://x.com/i/web/status/100', 'data/tweets.js', ?
                )
                """,
                (
                    import_id,
                    self.config.user_id,
                    watcher.isoformat(),
                    "b" * 64,
                ),
            )

        complete = watcher.memory_audit(self.config, self.connection)
        self.assertTrue(complete["current_memory_ok"])
        self.assertTrue(complete["archive_ready"])
        self.assertTrue(complete["final_complete"])
        self.assertEqual(complete["status"], "complete")
        self.assertEqual(complete["archive"]["direct_messages_imported"], 0)

        with self.connection:
            self.connection.execute(
                """
                UPDATE archive_imports
                SET account_user_id = '999'
                WHERE id = ?
                """,
                (import_id,),
            )
        invalid = watcher.memory_audit(self.config, self.connection)
        self.assertFalse(invalid["current_memory_ok"])
        self.assertFalse(invalid["archive_ready"])
        self.assertFalse(invalid["final_complete"])
        self.assertEqual(invalid["status"], "invalid")
        self.assertIn(
            "archive_import_owner_mismatch",
            invalid["archive_errors"],
        )

    def test_resolve_requires_exact_history_turn(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        with self.assertRaisesRegex(
            ValueError,
            "Import the exact inspected event turn",
        ):
            watcher.resolve_event(
                self.config,
                self.connection,
                "2080696811623190996",
                disposition="skip",
                reason="reviewed",
                reply_url=None,
            )

    def test_mandatory_response_rejects_content_skip(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        event_id = "2080696811623190996"
        watcher.import_history_snapshot(
            self.connection,
            {
                "chain_id": event_id,
                "root_status_id": event_id,
                "provenance": "short",
                "turns": [
                    {
                        "status_id": event_id,
                        "actor": "user",
                        "url": f"https://x.com/i/status/{event_id}",
                        "exact_text": "Insult without a factual thesis",
                    }
                ],
            },
        )
        strict_config = replace(
            self.config,
            mandatory_response_mode=True,
        )

        with self.assertRaisesRegex(
            ValueError,
            "forbids content-based skip",
        ):
            watcher.resolve_event(
                strict_config,
                self.connection,
                event_id,
                disposition="skip",
                reason="content-free insult",
                reply_url=None,
            )

    def test_mandatory_response_allows_proven_already_answered(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        event_id = "2080696811623190996"
        reply_id = "2080858504571605419"
        watcher.import_history_snapshot(
            self.connection,
            {
                "chain_id": event_id,
                "root_status_id": event_id,
                "provenance": "short",
                "turns": [
                    {
                        "status_id": event_id,
                        "actor": "user",
                        "url": f"https://x.com/i/status/{event_id}",
                        "exact_text": "Target",
                    },
                    {
                        "status_id": reply_id,
                        "parent_status_id": event_id,
                        "actor": "alex",
                        "url": (
                            f"https://x.com/axrbarsic/status/{reply_id}"
                        ),
                        "exact_text": "Existing exact reply",
                    },
                ],
            },
        )
        strict_config = replace(
            self.config,
            mandatory_response_mode=True,
        )

        result = watcher.resolve_event(
            strict_config,
            self.connection,
            event_id,
            disposition="skip",
            reason="exact direct Alex child reply already exists",
            reply_url=None,
            evidence=[f"https://x.com/axrbarsic/status/{reply_id}"],
        )

        self.assertEqual(result["disposition"], "skip")

    def test_commenter_history_crosses_conversations_with_exact_provenance(
        self,
    ) -> None:
        self.insert_direct_event(
            event_id="3001",
            created_at="2020-01-02T03:04:05Z",
            conversation_id="3000",
            parent_status_id="2999",
            text="Old public claim",
        )
        self.insert_direct_event(
            event_id="4001",
            created_at="2026-07-25T20:00:00Z",
            conversation_id="4000",
            parent_status_id="3999",
            text="Current public claim",
        )
        watcher.import_history_snapshot(
            self.connection,
            {
                "chain_id": "3000",
                "root_status_id": "3000",
                "provenance": "short",
                "turns": [
                    {
                        "status_id": "3001",
                        "actor": "user",
                        "author": "@target_user",
                        "url": "https://x.com/target_user/status/3001",
                        "exact_text": "Old public claim",
                        "posted_at": "2020-01-02T03:04:05Z",
                    },
                    {
                        "status_id": "3002",
                        "parent_status_id": "3001",
                        "actor": "alex",
                        "author": "@axrbarsic",
                        "url": "https://x.com/axrbarsic/status/3002",
                        "exact_text": "Old exact Alex reply",
                        "posted_at": "2020-01-02T03:05:05Z",
                    },
                ],
            },
        )

        result = watcher.commenter_history_for_event(
            self.connection,
            "4001",
            limit=12,
        )

        self.assertEqual(result["identity_kind"], "x_user_id")
        self.assertEqual(result["author_id"], "901")
        self.assertEqual(result["total_prior_interactions"], 1)
        self.assertEqual(result["first_interaction_at"], "2020-01-02T03:04:05Z")
        self.assertEqual(result["returned_interactions"], 1)
        interaction = result["interactions"][0]
        self.assertEqual(interaction["status_id"], "3001")
        self.assertEqual(interaction["text"], "Old public claim")
        self.assertEqual(
            interaction["alex_replies"][0]["text"],
            "Old exact Alex reply",
        )
        self.assertEqual(
            interaction["alex_replies"][0]["url"],
            "https://x.com/axrbarsic/status/3002",
        )

    def test_mandatory_response_requires_terminal_blocker_code(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        event_id = "2080696811623190996"
        watcher.import_history_snapshot(
            self.connection,
            {
                "chain_id": event_id,
                "root_status_id": event_id,
                "provenance": "pro",
                "turns": [
                    {
                        "status_id": event_id,
                        "actor": "user",
                        "url": f"https://x.com/i/status/{event_id}",
                        "exact_text": "Pro follow-up",
                    }
                ],
            },
        )
        strict_config = replace(
            self.config,
            mandatory_response_mode=True,
        )

        with self.assertRaisesRegex(
            ValueError,
            "requires a terminal blocker_code",
        ):
            watcher.resolve_event(
                strict_config,
                self.connection,
                event_id,
                disposition="blocked",
                reason="temporary Browser timeout",
                reply_url=None,
            )
        result = watcher.resolve_event(
            strict_config,
            self.connection,
            event_id,
            disposition="blocked",
            reason="historical Pro conversation cannot be recovered",
            reply_url=None,
            blocker_code="missing_historical_pro_conversation",
        )
        self.assertEqual(
            result["blocker_code"],
            "missing_historical_pro_conversation",
        )

    def test_legacy_blocked_resolution_requires_audited_code_revision(
        self,
    ) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        event_id = "2080696811623190996"
        event = self.connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        watcher.import_history_snapshot(
            self.connection,
            {
                "chain_id": event["conversation_id"],
                "root_status_id": event["conversation_id"],
                "provenance": "pro",
                "turns": [
                    {
                        "status_id": event_id,
                        "actor": "user",
                        "url": f"https://x.com/i/status/{event_id}",
                        "exact_text": "Legacy blocked target",
                    }
                ],
            },
        )
        watcher.set_meta(
            self.connection,
            "configured_user_id",
            self.config.user_id,
        )
        with self.connection:
            self.connection.execute(
                """
                UPDATE events
                SET is_reply = 0
                WHERE event_id <> ?
                """,
                (event_id,),
            )
            self.connection.execute(
                """
                INSERT INTO event_resolutions(
                    event_id, disposition, reason, blocker_code, evidence_json,
                    resolved_at
                ) VALUES(?, 'blocked', ?, NULL, '[]', ?)
                """,
                (
                    event_id,
                    "Legacy blocker without a machine-readable code",
                    watcher.isoformat(),
                ),
            )
            self.connection.execute(
                """
                UPDATE events
                SET delivery_state = 'acknowledged'
                WHERE event_id = ?
                """,
                (event_id,),
            )
            watcher.set_meta(
                self.connection,
                "initial_audit_completed_at",
                watcher.isoformat(),
            )

        status = watcher.initial_audit_status(self.connection)
        self.assertFalse(status["complete"])
        self.assertEqual(status["blocked_resolutions_missing_code"], 1)
        self.assertFalse(status["blocked_contract_complete"])
        self.assertFalse(status["history_complete"])
        self.assertFalse(status["invariant_ok"])
        with self.assertRaisesRegex(
            ValueError,
            "blocked resolutions missing a valid terminal blocker_code",
        ):
            watcher.complete_initial_audit(self.config, self.connection)

        strict_config = replace(
            self.config,
            mandatory_response_mode=True,
        )
        revised = watcher.revise_event_resolution(
            strict_config,
            self.connection,
            event_id,
            disposition="blocked",
            reason="Exact historical Pro conversation cannot be recovered",
            reply_url=None,
            blocker_code="missing_historical_pro_conversation",
            revision_reason="Backfilled the mandatory terminal blocker code",
            evidence=["legacy resolution audit"],
        )
        self.assertTrue(revised["revised"])
        self.assertEqual(
            revised["blocker_code"],
            "missing_historical_pro_conversation",
        )
        revisions = self.connection.execute(
            """
            SELECT previous_json, replacement_json
            FROM event_resolution_revisions
            WHERE event_id = ?
            ORDER BY id
            """,
            (event_id,),
        ).fetchall()
        self.assertEqual(len(revisions), 1)
        self.assertIsNone(
            json.loads(revisions[0]["previous_json"])["blocker_code"]
        )
        self.assertEqual(
            json.loads(revisions[0]["replacement_json"])["blocker_code"],
            "missing_historical_pro_conversation",
        )
        status = watcher.initial_audit_status(self.connection)
        self.assertEqual(status["blocked_resolutions_missing_code"], 0)
        self.assertTrue(status["blocked_contract_complete"])
        self.assertTrue(status["history_complete"])
        self.assertTrue(status["invariant_ok"])
        self.assertTrue(status["complete"])

        with self.connection:
            self.connection.execute(
                """
                UPDATE event_resolutions
                SET blocker_code = 'legacy_unknown_code'
                WHERE event_id = ?
                """,
                (event_id,),
            )
        status = watcher.initial_audit_status(self.connection)
        self.assertEqual(status["blocked_resolutions_missing_code"], 1)
        self.assertFalse(status["complete"])
        repaired = watcher.revise_event_resolution(
            strict_config,
            self.connection,
            event_id,
            disposition="blocked",
            reason="Exact historical Pro conversation cannot be recovered",
            reply_url=None,
            blocker_code="missing_historical_pro_conversation",
            revision_reason="Replaced an unknown legacy terminal blocker code",
            evidence=["legacy resolution audit"],
        )
        self.assertTrue(repaired["revised"])
        revisions = self.connection.execute(
            """
            SELECT previous_json, replacement_json
            FROM event_resolution_revisions
            WHERE event_id = ?
            ORDER BY id
            """,
            (event_id,),
        ).fetchall()
        self.assertEqual(len(revisions), 2)
        self.assertEqual(
            json.loads(revisions[1]["previous_json"])["blocker_code"],
            "legacy_unknown_code",
        )
        self.assertEqual(
            json.loads(revisions[1]["replacement_json"])["blocker_code"],
            "missing_historical_pro_conversation",
        )
        self.assertTrue(watcher.initial_audit_status(self.connection)["complete"])

    def test_mandatory_response_requeue_is_target_agnostic(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        event_id = "2080696811623190996"
        event = self.connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        watcher.import_history_snapshot(
            self.connection,
            {
                "chain_id": event_id,
                "root_status_id": event_id,
                "provenance": "short",
                "turns": [
                    {
                        "status_id": event_id,
                        "actor": "user",
                        "url": f"https://x.com/i/status/{event_id}",
                        "exact_text": "Previously skipped insult",
                    }
                ],
            },
        )
        watcher.resolve_event(
            self.config,
            self.connection,
            event_id,
            disposition="skip",
            reason="old content-based skip",
            reply_url=None,
        )
        strict_config = replace(
            self.config,
            mandatory_response_mode=True,
        )
        as_of = watcher.parse_time(event["created_at"]) + timedelta(hours=1)

        preview = watcher.requeue_unanswered_skips(
            strict_config,
            self.connection,
            response_window_hours=12,
            now=as_of,
            dry_run=True,
        )
        applied = watcher.requeue_unanswered_skips(
            strict_config,
            self.connection,
            response_window_hours=12,
            now=as_of,
            dry_run=False,
        )

        self.assertEqual(preview["candidate_event_ids"], [event_id])
        self.assertEqual(applied["candidate_event_ids"], [event_id])
        self.assertEqual(
            self.connection.execute(
                "SELECT delivery_state FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()["delivery_state"],
            "queued",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM response_policy_requeues"
            ).fetchone()[0],
            1,
        )

    def test_mandatory_response_requeues_recent_ignored_mentions(self) -> None:
        watcher.set_meta(self.connection, "first_success_at", watcher.isoformat())
        payload = json.loads(json.dumps(self.fixture))
        event_id = "2080696811623190996"
        payload["data"][0]["in_reply_to_user_id"] = "another-user"
        watcher.ingest_response(
            self.config,
            self.connection,
            payload,
            source="x_api",
        )
        event = self.connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        self.assertEqual(event["delivery_state"], "ignored")
        strict_config = replace(
            self.config,
            mandatory_response_mode=True,
        )
        as_of = watcher.parse_time(event["created_at"]) + timedelta(hours=1)

        preview = watcher.requeue_unanswered_skips(
            strict_config,
            self.connection,
            response_window_hours=12,
            now=as_of,
            dry_run=True,
        )
        applied = watcher.requeue_unanswered_skips(
            strict_config,
            self.connection,
            response_window_hours=12,
            now=as_of,
            dry_run=False,
        )

        self.assertEqual(preview["candidate_event_ids"], [event_id])
        self.assertEqual(applied["candidate_event_ids"], [event_id])
        self.assertEqual(
            self.connection.execute(
                "SELECT delivery_state FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()["delivery_state"],
            "queued",
        )
        audit = self.connection.execute(
            """
            SELECT reason, previous_resolution_json
            FROM response_policy_requeues
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        self.assertEqual(
            audit["reason"],
            "mandatory_response_ignored_mention_reconciliation",
        )
        self.assertEqual(
            json.loads(audit["previous_resolution_json"]),
            {
                "delivery_state": "ignored",
                "disposition": None,
            },
        )

    def test_resolve_can_enrich_missing_stance_detail_once(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        event_id = "2080696811623190996"
        watcher.import_history_snapshot(
            self.connection,
            {
                "chain_id": event_id,
                "root_status_id": event_id,
                "provenance": "short",
                "turns": [
                    {
                        "status_id": event_id,
                        "actor": "user",
                        "url": f"https://x.com/i/status/{event_id}",
                        "exact_text": "Accumulated follow-up fixture 1",
                    }
                ],
            },
        )
        common = {
            "disposition": "skip",
            "reason": "reviewed",
            "reply_url": None,
            "stance": "neutral",
            "confidence": "high",
            "evidence": ["live parent"],
        }
        watcher.resolve_event(
            self.config,
            self.connection,
            event_id,
            **common,
        )
        enriched = watcher.resolve_event(
            self.config,
            self.connection,
            event_id,
            stance_detail="neutral_reviewed_fixture",
            **common,
        )
        self.assertEqual(
            enriched["stance_detail"],
            "neutral_reviewed_fixture",
        )
        stored = self.connection.execute(
            "SELECT stance_detail FROM event_resolutions WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        self.assertEqual(
            stored["stance_detail"],
            "neutral_reviewed_fixture",
        )

    def test_browser_handoff_sync_requires_history_and_is_idempotent(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        event_id = "2080696811623190996"
        event = self.connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        event_payload = json.loads(event["payload_json"])
        evidence_dir = (
            self.root
            / "var"
            / "evidence"
            / "browser-owner"
            / "test-browser-session"
        )
        evidence_dir.mkdir(parents=True)
        history_file = evidence_dir / "conversation-history.jsonl"
        ledger_file = evidence_dir / "run-ledger.jsonl"
        ledger_record = {
            "event": "initial_audit_disposition",
            "event_id": event_id,
            "conversation_id": event["conversation_id"],
            "direct_reply_to_axrbarsic": True,
            "history_status": "exact_user_turn_appended",
            "watcher_disposition": "durable_skip_pending_root_resolve",
            "disposition": "skip",
            "reason": "reviewed in live Browser",
            "stance": "neutral",
            "stance_detail": "neutral_reviewed_fixture",
            "confidence": "high",
            "media_meaning": None,
            "evidence": ["live full chain"],
            "reply_url": None,
        }
        ledger_file.write_text(
            json.dumps(ledger_record) + "\n",
            encoding="utf-8",
        )
        history_file.write_text("", encoding="utf-8")
        other_evidence_dir = evidence_dir.parent / "other-browser-session"
        other_evidence_dir.mkdir()
        other_ledger_file = other_evidence_dir / "run-ledger.jsonl"
        other_ledger_file.write_text(
            ledger_file.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ValueError,
            "must share one evidence directory",
        ):
            watcher.sync_browser_handoffs(
                self.config,
                self.connection,
                history_path=history_file,
                ledger_path=other_ledger_file,
            )

        with self.assertRaisesRegex(
            ValueError,
            "has no imported exact history turn",
        ):
            watcher.sync_browser_handoffs(
                self.config,
                self.connection,
                history_path=history_file,
                ledger_path=ledger_file,
            )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM event_resolutions WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        )

        history_file.write_text(
            json.dumps(
                {
                    "record_type": "initial_audit_event_turn",
                    "chain_id": event["conversation_id"],
                    "conversation_root_id": event["conversation_id"],
                    "chain_provenance": "short",
                    "parent_status_id": event_payload["referenced_tweets"][0]["id"],
                    "status_id": event_id,
                    "actor": "user",
                    "author": event["username"],
                    "url": f"https://x.com/{event['username']}/status/{event_id}",
                    "exact_text": event_payload["text"],
                    "provenance": "live_x_dom_initial_audit",
                    "media_json": [],
                    "sources": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        first = watcher.sync_browser_handoffs(
            self.config,
            self.connection,
            history_path=history_file,
            ledger_path=ledger_file,
        )
        self.assertEqual(first["resolved_event_ids"], [event_id])
        self.assertEqual(first["audit"]["resolved_events"], 1)
        self.assertTrue(first["audit"]["history_complete"])
        self.assertEqual(
            first["evidence_manifest"]["status"],
            "finalized",
        )
        self.assertTrue((evidence_dir / "manifest.json").is_file())

        second = watcher.sync_browser_handoffs(
            self.config,
            self.connection,
            history_path=history_file,
            ledger_path=ledger_file,
        )
        self.assertEqual(second["resolved_event_ids"], [])
        self.assertEqual(second["already_resolved_event_ids"], [event_id])
        self.assertEqual(
            second["evidence_manifest"]["status"],
            "already_finalized",
        )

    def test_browser_handoff_sync_accepts_tracked_conversation_route(
        self,
    ) -> None:
        tracked_id = "2080312847230210375"
        self.insert_alex_turn_for_chain(tracked_id)
        response = {
            "data": [
                {
                    "id": "2081034932135096804",
                    "author_id": "901",
                    "text": "Nested Proton continuation",
                    "created_at": "2026-07-25T15:12:46Z",
                    "conversation_id": tracked_id,
                    "in_reply_to_user_id": "2024119407933534209",
                    "referenced_tweets": [
                        {
                            "type": "replied_to",
                            "id": "2081028119784230951",
                        }
                    ],
                }
            ],
            "includes": {
                "users": [{"id": "901", "username": "target_user"}]
            },
            "meta": {"newest_id": "2081034932135096804"},
        }
        watcher.ingest_response(
            self.config,
            self.connection,
            response,
            source="x_api",
        )
        event_id = "2081034932135096804"
        history_file = self.root / "tracked-history.jsonl"
        ledger_file = self.root / "tracked-ledger.jsonl"
        history_file.write_text(
            json.dumps(
                {
                    "record_type": "initial_audit_event_turn",
                    "chain_id": tracked_id,
                    "conversation_root_id": tracked_id,
                    "chain_provenance": "short",
                    "parent_status_id": "2081028119784230951",
                    "status_id": event_id,
                    "actor": "user",
                    "author": "target_user",
                    "url": f"https://x.com/target_user/status/{event_id}",
                    "exact_text": "Nested Proton continuation",
                    "provenance": "live_x_dom",
                    "media_json": [],
                    "sources": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        ledger_file.write_text(
            json.dumps(
                {
                    "event": "initial_audit_disposition",
                    "event_id": event_id,
                    "conversation_id": tracked_id,
                    "direct_reply_to_axrbarsic": False,
                    "tracked_conversation_reply": True,
                    "history_status": "exact_user_turn_appended",
                    "watcher_disposition":
                        "durable_skip_pending_root_resolve",
                    "disposition": "skip",
                    "reason": "nested continuation without a new question",
                    "reply_url": None,
                    "stance": "neutral",
                    "confidence": "high",
                    "media_meaning": None,
                    "evidence": ["live full chain"],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = watcher.sync_browser_handoffs(
            self.config,
            self.connection,
            history_path=history_file,
            ledger_path=ledger_file,
        )

        self.assertEqual(result["resolved_event_ids"], [event_id])

    def test_browser_handoff_sync_accepts_explicit_mention_route(
        self,
    ) -> None:
        conversation_id = "2080312847230210375"
        event_id = "2081231683437699278"
        response = {
            "data": [
                {
                    "id": event_id,
                    "author_id": "901",
                    "text": "@other_user @axrbarsic Follow-up mention",
                    "created_at": "2026-07-26T04:14:35Z",
                    "conversation_id": conversation_id,
                    "in_reply_to_user_id": "1246293284",
                    "referenced_tweets": [
                        {
                            "type": "replied_to",
                            "id": "2081230000000000000",
                        }
                    ],
                }
            ],
            "includes": {
                "users": [{"id": "901", "username": "target_user"}]
            },
            "meta": {"newest_id": event_id},
        }
        strict_config = replace(
            self.config,
            keychain_account="axrbarsic",
            mandatory_response_mode=True,
        )
        watcher.ingest_response(
            strict_config,
            self.connection,
            response,
            source="x_api",
        )
        history_file = self.root / "mention-history.jsonl"
        ledger_file = self.root / "mention-ledger.jsonl"
        history_file.write_text(
            json.dumps(
                {
                    "record_type": "initial_audit_event_turn",
                    "chain_id": conversation_id,
                    "conversation_root_id": conversation_id,
                    "chain_provenance": "short",
                    "parent_status_id": "2081230000000000000",
                    "status_id": event_id,
                    "actor": "user",
                    "author": "target_user",
                    "url": f"https://x.com/target_user/status/{event_id}",
                    "exact_text": "@other_user @axrbarsic Follow-up mention",
                    "provenance": "live_x_dom",
                    "media_json": [],
                    "sources": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        ledger_file.write_text(
            json.dumps(
                {
                    "event": "initial_audit_disposition",
                    "event_id": event_id,
                    "conversation_id": conversation_id,
                    "direct_reply_to_axrbarsic": False,
                    "tracked_conversation_reply": False,
                    "mention_reply_to_axrbarsic": True,
                    "history_status": "exact_user_turn_appended",
                    "watcher_disposition":
                        "durable_skip_pending_root_resolve",
                    "disposition": "skip",
                    "reason": "route validation fixture",
                    "reply_url": None,
                    "stance": "neutral",
                    "confidence": "high",
                    "media_meaning": None,
                    "evidence": ["live full chain"],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = watcher.sync_browser_handoffs(
            replace(self.config, keychain_account="axrbarsic"),
            self.connection,
            history_path=history_file,
            ledger_path=ledger_file,
        )

        self.assertEqual(result["resolved_event_ids"], [event_id])

    def test_explicit_mention_route_requires_exact_handle_boundary(self) -> None:
        event_id = "2081231683437699279"
        self.insert_direct_event(
            event_id=event_id,
            created_at="2026-07-26T04:14:35Z",
            conversation_id="2080312847230210375",
            parent_status_id="2081230000000000000",
            text="@axrbarsic_extra is a different account",
        )
        event = self.connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()

        self.assertFalse(
            watcher.event_mentions_configured_account(
                replace(self.config, keychain_account="axrbarsic"),
                event,
            )
        )

    def test_browser_handoff_sync_requires_explicit_correction_marker(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        event_id = "2080696811623190996"
        event = self.connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        event_payload = json.loads(event["payload_json"])
        history_file = self.root / "conversation-history.jsonl"
        ledger_file = self.root / "run-ledger.jsonl"
        history_file.write_text(
            json.dumps(
                {
                    "record_type": "initial_audit_event_turn",
                    "chain_id": event["conversation_id"],
                    "conversation_root_id": event["conversation_id"],
                    "chain_provenance": "short",
                    "parent_status_id": event_payload["referenced_tweets"][0]["id"],
                    "status_id": event_id,
                    "actor": "user",
                    "author": event["username"],
                    "url": f"https://x.com/{event['username']}/status/{event_id}",
                    "exact_text": event_payload["text"],
                    "provenance": "live_x_dom_initial_audit",
                    "media_json": [],
                    "sources": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        common = {
            "event": "initial_audit_disposition",
            "event_id": event_id,
            "conversation_id": event["conversation_id"],
            "history_status": "exact_user_turn_appended",
            "watcher_disposition": "durable_skip_pending_root_resolve",
            "disposition": "skip",
            "reason": "reviewed in live Browser",
            "stance": "neutral",
            "stance_detail": "neutral_reviewed_fixture",
            "confidence": "high",
            "evidence": ["live full chain"],
        }
        invalid = {**common}
        corrected = {
            **common,
            "direct_reply_to_axrbarsic": True,
        }
        ledger_file.write_text(
            json.dumps(invalid) + "\n" + json.dumps(corrected) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Conflicting Browser handoffs"):
            watcher.sync_browser_handoffs(
                self.config,
                self.connection,
                history_path=history_file,
                ledger_path=ledger_file,
            )

        corrected["supersedes_invalid_handoff"] = True
        ledger_file.write_text(
            json.dumps(invalid) + "\n" + json.dumps(corrected) + "\n",
            encoding="utf-8",
        )
        result = watcher.sync_browser_handoffs(
            self.config,
            self.connection,
            history_path=history_file,
            ledger_path=ledger_file,
        )
        self.assertEqual(result["resolved_event_ids"], [event_id])

    def test_browser_handoff_sync_rejects_reply_url_on_skip(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        event_id = "2080696811623190996"
        event = self.connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        event_payload = json.loads(event["payload_json"])
        history_file = self.root / "conversation-history.jsonl"
        ledger_file = self.root / "run-ledger.jsonl"
        history_file.write_text(
            json.dumps(
                {
                    "record_type": "initial_audit_event_turn",
                    "chain_id": event["conversation_id"],
                    "conversation_root_id": event["conversation_id"],
                    "chain_provenance": "short",
                    "parent_status_id": (
                        event_payload["referenced_tweets"][0]["id"]
                    ),
                    "status_id": event_id,
                    "actor": "user",
                    "author": event["username"],
                    "url": (
                        f"https://x.com/{event['username']}/status/{event_id}"
                    ),
                    "exact_text": event_payload["text"],
                    "provenance": "live_x_dom_initial_audit",
                    "media_json": [],
                    "sources": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        common = {
            "event": "initial_audit_disposition",
            "event_id": event_id,
            "conversation_id": event["conversation_id"],
            "direct_reply_to_axrbarsic": True,
            "history_status": "exact_user_turn_appended",
            "watcher_disposition": "durable_skip_pending_root_resolve",
            "disposition": "skip",
            "reason": "already answered before this audit",
            "stance": "supportive",
            "confidence": "high",
            "evidence": [
                "existing reply https://x.com/axrbarsic/status/2080858504571605419"
            ],
        }
        invalid = {
            **common,
            "reply_url": "https://x.com/axrbarsic/status/2080858504571605419",
        }
        ledger_file.write_text(
            json.dumps(invalid) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ValueError,
            "reply_url without publication",
        ):
            watcher.sync_browser_handoffs(
                self.config,
                self.connection,
                history_path=history_file,
                ledger_path=ledger_file,
            )

        corrected = {
            **common,
            "reply_url": None,
            "supersedes_invalid_handoff": True,
        }
        ledger_file.write_text(
            json.dumps(invalid)
            + "\n"
            + json.dumps(corrected)
            + "\n",
            encoding="utf-8",
        )
        result = watcher.sync_browser_handoffs(
            self.config,
            self.connection,
            history_path=history_file,
            ledger_path=ledger_file,
        )
        self.assertEqual(result["resolved_event_ids"], [event_id])
        stored = self.connection.execute(
            "SELECT disposition, reply_url FROM event_resolutions "
            "WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        self.assertEqual(stored["disposition"], "skip")
        self.assertIsNone(stored["reply_url"])

    def test_browser_handoff_sync_requires_matching_published_alex_turn(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        event_id = "2080696811623190996"
        reply_id = "2080858504571605419"
        event = self.connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        event_payload = json.loads(event["payload_json"])
        history_file = self.root / "conversation-history.jsonl"
        ledger_file = self.root / "run-ledger.jsonl"
        event_turn = {
            "record_type": "initial_audit_event_turn",
            "chain_id": event["conversation_id"],
            "conversation_root_id": event["conversation_id"],
            "chain_provenance": "short",
            "parent_status_id": event_payload["referenced_tweets"][0]["id"],
            "status_id": event_id,
            "actor": "user",
            "author": event["username"],
            "url": f"https://x.com/{event['username']}/status/{event_id}",
            "exact_text": event_payload["text"],
            "provenance": "live_x_dom_initial_audit",
            "media_json": [],
            "sources": [],
        }
        alex_turn = {
            "record_type": "initial_audit_alex_turn",
            "chain_id": event["conversation_id"],
            "conversation_root_id": event["conversation_id"],
            "chain_provenance": "short",
            "parent_status_id": event_id,
            "status_id": reply_id,
            "actor": "axrbarsic",
            "author": "@axrbarsic",
            "url": f"https://x.com/axrbarsic/status/{reply_id}",
            "exact_text": "Проверенный ответ.",
            "provenance": "self_authored_short_sol",
            "media_json": [],
            "sources": ["https://example.com/source"],
        }
        history_file.write_text(
            json.dumps(event_turn, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        ledger_file.write_text(
            json.dumps(
                {
                    "event": "initial_audit_disposition",
                    "event_id": event_id,
                    "conversation_id": event["conversation_id"],
                    "direct_reply_to_axrbarsic": True,
                    "history_status": "exact_user_turn_appended",
                    "alex_history_status": "exact_alex_turn_appended",
                    "watcher_disposition": (
                        "verified_publication_pending_root_resolve"
                    ),
                    "disposition": "published",
                    "reason": "verified factual reply",
                    "reply_url": f"https://x.com/axrbarsic/status/{reply_id}",
                    "stance": "opposing",
                    "stance_detail": "opposing_factual_claim",
                    "confidence": "high",
                    "evidence": ["live full chain"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ValueError,
            "has no matching exact Alex turn",
        ):
            watcher.sync_browser_handoffs(
                self.config,
                self.connection,
                history_path=history_file,
                ledger_path=ledger_file,
            )

        history_file.write_text(
            "\n".join(
                json.dumps(record, ensure_ascii=False)
                for record in (event_turn, alex_turn)
            )
            + "\n",
            encoding="utf-8",
        )
        result = watcher.sync_browser_handoffs(
            self.config,
            self.connection,
            history_path=history_file,
            ledger_path=ledger_file,
        )
        self.assertEqual(result["resolved_event_ids"], [event_id])

    def test_database_open_backfills_published_alex_parent_link(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        event_id = "2080696811623190996"
        reply_id = "2080858504571605419"
        event = self.connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        watcher.import_history_snapshot(
            self.connection,
            {
                "chain_id": event["conversation_id"],
                "root_status_id": event["conversation_id"],
                "provenance": "short",
                "turns": [
                    {
                        "status_id": event_id,
                        "actor": "user",
                        "url": f"https://x.com/i/status/{event_id}",
                        "exact_text": "Target",
                    },
                    {
                        "status_id": reply_id,
                        "parent_status_id": event_id,
                        "actor": "alex",
                        "url": f"https://x.com/axrbarsic/status/{reply_id}",
                        "exact_text": "Reply",
                    },
                ],
            },
        )
        watcher.resolve_event(
            self.config,
            self.connection,
            event_id,
            disposition="published",
            reason="verified reply",
            reply_url=f"https://x.com/axrbarsic/status/{reply_id}",
            stance="opposing",
            stance_detail="opposing_factual_claim",
            confidence="high",
            evidence=["live full chain"],
        )
        with self.connection:
            self.connection.execute(
                """
                UPDATE conversation_turns
                SET parent_status_id = NULL
                WHERE status_id = ?
                """,
                (reply_id,),
            )
        self.connection.close()
        self.connection = watcher.connect_database(self.config.database)
        stored = self.connection.execute(
            """
            SELECT parent_status_id
            FROM conversation_turns
            WHERE status_id = ?
            """,
            (reply_id,),
        ).fetchone()
        self.assertEqual(stored["parent_status_id"], event_id)
        status = watcher.initial_audit_status(self.connection)
        self.assertEqual(status["published_alex_history_missing"], 0)

    def test_resolution_revision_is_audited_and_idempotent(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        event_id = "2080696811623190996"
        reply_id = "2080858504571605419"
        event = self.connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        watcher.import_history_snapshot(
            self.connection,
            {
                "chain_id": event["conversation_id"],
                "root_status_id": event["conversation_id"],
                "provenance": "short",
                "turns": [
                    {
                        "status_id": event_id,
                        "actor": "user",
                        "url": f"https://x.com/i/status/{event_id}",
                        "exact_text": "Target",
                    },
                    {
                        "status_id": reply_id,
                        "parent_status_id": event_id,
                        "actor": "alex",
                        "url": f"https://x.com/axrbarsic/status/{reply_id}",
                        "exact_text": "Reply",
                    },
                ],
            },
        )
        watcher.resolve_event(
            self.config,
            self.connection,
            event_id,
            disposition="skip",
            reason="outside prior scope",
            reply_url=None,
            stance="neutral",
            confidence="high",
            evidence=["prior live review"],
        )
        common = {
            "disposition": "published",
            "reason": "answered after explicit scope expansion",
            "reply_url": f"https://x.com/axrbarsic/status/{reply_id}",
            "revision_reason": "User explicitly expanded the review scope",
            "stance": "neutral",
            "stance_detail": "neutral_science_question",
            "confidence": "high",
            "evidence": ["live full chain", "primary source"],
        }
        first = watcher.revise_event_resolution(
            self.config,
            self.connection,
            event_id,
            **common,
        )
        self.assertTrue(first["revised"])
        stored = self.connection.execute(
            "SELECT * FROM event_resolutions WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        self.assertEqual(stored["disposition"], "published")
        self.assertEqual(stored["reply_url"], common["reply_url"])
        revisions = self.connection.execute(
            """
            SELECT *
            FROM event_resolution_revisions
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchall()
        self.assertEqual(len(revisions), 1)
        self.assertEqual(
            json.loads(revisions[0]["previous_json"])["disposition"],
            "skip",
        )

        second = watcher.revise_event_resolution(
            self.config,
            self.connection,
            event_id,
            **common,
        )
        self.assertFalse(second["revised"])
        self.assertEqual(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM event_resolution_revisions
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()[0],
            1,
        )
        export_file = self.root / "resolution-export.jsonl"
        result = watcher.export_audit_resolutions(
            self.connection,
            export_file,
        )
        self.assertEqual(result["resolution_revisions"], 1)
        records = [
            json.loads(line)
            for line in export_file.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            sum(
                record.get("record_type") == "event_resolution_revision"
                for record in records
            ),
            1,
        )

    def test_blocked_resolution_can_later_be_published(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        event_id = "2080696811623190996"
        reply_id = "2080858504571605419"
        event = self.connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        watcher.import_history_snapshot(
            self.connection,
            {
                "chain_id": event["conversation_id"],
                "root_status_id": event["conversation_id"],
                "provenance": "pro",
                "turns": [
                    {
                        "status_id": event_id,
                        "actor": "user",
                        "url": f"https://x.com/i/status/{event_id}",
                        "exact_text": "Target",
                    },
                    {
                        "status_id": reply_id,
                        "parent_status_id": event_id,
                        "actor": "alex",
                        "url": f"https://x.com/axrbarsic/status/{reply_id}",
                        "exact_text": "Reply",
                    },
                ],
            },
        )
        watcher.resolve_event(
            self.config,
            self.connection,
            event_id,
            disposition="skip",
            reason="incorrectly classified before contract recovery",
            reply_url=None,
            stance="opposing",
            confidence="high",
            evidence=["live full chain"],
        )
        blocked = watcher.revise_event_resolution(
            self.config,
            self.connection,
            event_id,
            disposition="blocked",
            reason="exact historical Pro conversation URL is unavailable",
            reply_url=None,
            revision_reason="Corrected skip to an explicit contract blocker",
            stance="opposing",
            confidence="high",
            evidence=["ledger search", "session search"],
        )
        self.assertTrue(blocked["revised"])
        self.assertEqual(
            watcher.initial_audit_status(self.connection)[
                "blocked_resolutions"
            ],
            1,
        )
        published = watcher.revise_event_resolution(
            self.config,
            self.connection,
            event_id,
            disposition="published",
            reason="historical conversation recovered and reply verified",
            reply_url=f"https://x.com/axrbarsic/status/{reply_id}",
            revision_reason="Recovered exact historical Pro conversation",
            stance="opposing",
            confidence="high",
            evidence=["recovered conversation URL"],
        )
        self.assertTrue(published["revised"])
        stored = self.connection.execute(
            "SELECT disposition FROM event_resolutions WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        self.assertEqual(stored["disposition"], "published")
        self.assertEqual(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM event_resolution_revisions
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()[0],
            2,
        )

    def test_browser_handoff_sync_revises_existing_skip_once(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        event_id = "2080696811623190996"
        reply_id = "2080858504571605419"
        event = self.connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        event_payload = json.loads(event["payload_json"])
        history_file = self.root / "conversation-history.jsonl"
        ledger_file = self.root / "run-ledger.jsonl"
        event_turn = {
            "record_type": "initial_audit_event_turn",
            "chain_id": event["conversation_id"],
            "conversation_root_id": event["conversation_id"],
            "chain_provenance": "short",
            "parent_status_id": event_payload["referenced_tweets"][0]["id"],
            "status_id": event_id,
            "actor": "user",
            "author": event["username"],
            "url": f"https://x.com/{event['username']}/status/{event_id}",
            "exact_text": event_payload["text"],
            "provenance": "live_x_dom_initial_audit",
            "media_json": [],
            "sources": [],
        }
        alex_turn = {
            "record_type": "initial_audit_alex_turn",
            "chain_id": event["conversation_id"],
            "conversation_root_id": event["conversation_id"],
            "chain_provenance": "short",
            "parent_status_id": event_id,
            "status_id": reply_id,
            "actor": "axrbarsic",
            "author": "@axrbarsic",
            "url": f"https://x.com/axrbarsic/status/{reply_id}",
            "exact_text": "Проверенный ответ.",
            "provenance": "self_authored_short_sol",
            "media_json": [],
            "sources": ["https://example.com/source"],
        }
        history_file.write_text(
            "\n".join(
                json.dumps(record, ensure_ascii=False)
                for record in (event_turn, alex_turn)
            )
            + "\n",
            encoding="utf-8",
        )
        watcher.import_history_file(self.connection, history_file)
        watcher.resolve_event(
            self.config,
            self.connection,
            event_id,
            disposition="skip",
            reason="outside prior scope",
            reply_url=None,
            stance="neutral",
            confidence="high",
            evidence=["prior live review"],
        )
        revision = {
            "event": "initial_audit_disposition",
            "event_id": event_id,
            "conversation_id": event["conversation_id"],
            "direct_reply_to_axrbarsic": True,
            "history_status": "exact_user_turn_appended",
            "alex_history_status": "exact_alex_turn_appended",
            "watcher_disposition": (
                "verified_publication_pending_root_resolve"
            ),
            "disposition": "published",
            "reason": "answered after explicit scope expansion",
            "reply_url": f"https://x.com/axrbarsic/status/{reply_id}",
            "stance": "neutral",
            "stance_detail": "neutral_science_question",
            "confidence": "high",
            "evidence": ["live full chain", "primary source"],
            "supersedes_existing_resolution": True,
            "resolution_revision_reason": (
                "User explicitly expanded the review scope"
            ),
        }
        ledger_file.write_text(
            json.dumps(revision, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        first = watcher.sync_browser_handoffs(
            self.config,
            self.connection,
            history_path=history_file,
            ledger_path=ledger_file,
        )
        self.assertEqual(first["resolved_event_ids"], [event_id])
        self.assertEqual(first["revised_event_ids"], [event_id])

        second = watcher.sync_browser_handoffs(
            self.config,
            self.connection,
            history_path=history_file,
            ledger_path=ledger_file,
        )
        self.assertEqual(second["resolved_event_ids"], [])
        self.assertEqual(second["revised_event_ids"], [])
        self.assertEqual(second["already_resolved_event_ids"], [event_id])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM event_resolution_revisions"
            ).fetchone()[0],
            1,
        )

    def test_browser_handoff_sync_revises_skip_to_blocked(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        event_id = "2080696811623190996"
        event = self.connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        event_payload = json.loads(event["payload_json"])
        history_file = self.root / "conversation-history.jsonl"
        ledger_file = self.root / "run-ledger.jsonl"
        history_file.write_text(
            json.dumps(
                {
                    "record_type": "initial_audit_event_turn",
                    "chain_id": event["conversation_id"],
                    "conversation_root_id": event["conversation_id"],
                    "chain_provenance": "pro",
                    "parent_status_id": (
                        event_payload["referenced_tweets"][0]["id"]
                    ),
                    "status_id": event_id,
                    "actor": "user",
                    "author": event["username"],
                    "url": (
                        f"https://x.com/{event['username']}/status/{event_id}"
                    ),
                    "exact_text": event_payload["text"],
                    "provenance": "live_x_dom_initial_audit",
                    "media_json": [],
                    "sources": [],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        watcher.import_history_file(self.connection, history_file)
        watcher.resolve_event(
            self.config,
            self.connection,
            event_id,
            disposition="skip",
            reason="temporarily treated as ordinary skip",
            reply_url=None,
            stance="opposing",
            confidence="high",
            evidence=["live full chain"],
        )
        ledger_file.write_text(
            json.dumps(
                {
                    "event": "initial_audit_disposition",
                    "event_id": event_id,
                    "conversation_id": event["conversation_id"],
                    "direct_reply_to_axrbarsic": True,
                    "history_status": "exact_user_turn_appended",
                    "watcher_disposition": (
                        "durable_blocked_pending_root_resolve"
                    ),
                    "disposition": "blocked",
                    "reason": (
                        "exact historical Pro conversation URL unavailable"
                    ),
                    "reply_url": None,
                    "stance": "opposing",
                    "stance_detail": "opposing_substantive_pro_followup",
                    "confidence": "high",
                    "evidence": ["ledger search", "session search"],
                    "supersedes_existing_resolution": True,
                    "resolution_revision_reason": (
                        "Corrected skip to explicit Pro contract blocker"
                    ),
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        first = watcher.sync_browser_handoffs(
            self.config,
            self.connection,
            history_path=history_file,
            ledger_path=ledger_file,
        )
        self.assertEqual(first["revised_event_ids"], [event_id])
        stored = self.connection.execute(
            "SELECT disposition FROM event_resolutions WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        self.assertEqual(stored["disposition"], "blocked")
        self.assertEqual(first["audit"]["blocked_resolutions"], 1)

        second = watcher.sync_browser_handoffs(
            self.config,
            self.connection,
            history_path=history_file,
            ledger_path=ledger_file,
        )
        self.assertEqual(second["resolved_event_ids"], [])
        self.assertEqual(second["already_resolved_event_ids"], [event_id])

    def test_initial_audit_requeues_legacy_ack_without_resolution(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="x_api",
        )
        with self.connection:
            self.connection.execute(
                """
                UPDATE events
                SET delivery_state = 'acknowledged'
                WHERE event_id = ?
                """,
                ("2080696811623190996",),
            )
        started = watcher.start_initial_audit(self.config, self.connection)
        self.assertEqual(started["pending_events"], 3)
        self.assertEqual(started["queued_events"], 3)
        state = self.connection.execute(
            "SELECT delivery_state FROM events WHERE event_id = ?",
            ("2080696811623190996",),
        ).fetchone()["delivery_state"]
        self.assertEqual(state, "queued")
        with self.assertRaisesRegex(ValueError, "unresolved direct events"):
            watcher.complete_initial_audit(self.config, self.connection)

    def test_live_poll_rejects_repeated_pagination_token(self) -> None:
        def fetch(url: str, token: str, timeout: int) -> dict:
            return {"data": [], "meta": {"next_token": "cycle"}}

        with mock.patch.dict(os.environ, {"X_BEARER_TOKEN": "test-token"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "repeated pagination token"):
                watcher.poll_live(self.config, self.connection, fetch=fetch)
        self.assertIsNone(watcher.get_meta(self.connection, "since_id"))
        self.assertEqual(watcher.get_meta(self.connection, "consecutive_failures"), "1")

    def test_exclusive_process_lock_rejects_overlap(self) -> None:
        with watcher.exclusive_process_lock(self.config.lock_file):
            with self.assertRaises(watcher.AlreadyRunningError):
                with watcher.exclusive_process_lock(self.config.lock_file):
                    pass

    def test_keychain_token_fallback_does_not_require_secret_in_config(self) -> None:
        raw = json.loads(self.config.source_path.read_text(encoding="utf-8"))
        raw["keychain_service"] = "test-service"
        raw["keychain_account"] = "test-account"
        self.config.source_path.write_text(json.dumps(raw), encoding="utf-8")
        configured = watcher.load_config(self.config.source_path)
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch(
                "xmention_watcher.subprocess.run",
                return_value=SimpleNamespace(stdout="keychain-token\n"),
            ) as run,
        ):
            self.assertEqual(watcher.bearer_token(configured), "keychain-token")
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[0], "/usr/bin/security")
        self.assertNotIn("keychain-token", arguments)

    def test_native_keychain_helper_is_preferred_when_executable(self) -> None:
        helper = self.root / "keychain-helper"
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        helper.chmod(0o700)
        raw = json.loads(self.config.source_path.read_text(encoding="utf-8"))
        raw["keychain_helper"] = str(helper)
        raw["keychain_service"] = "test-service"
        raw["keychain_account"] = "test-account"
        self.config.source_path.write_text(json.dumps(raw), encoding="utf-8")
        configured = watcher.load_config(self.config.source_path)
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch(
                "xmention_watcher.subprocess.run",
                return_value=SimpleNamespace(stdout="helper-token\n"),
            ) as run,
        ):
            token, source = watcher.bearer_token_with_source(configured)
        self.assertEqual(token, "helper-token")
        self.assertEqual(source, "keychain_helper")
        self.assertEqual(
            run.call_args.args[0],
            [str(helper), "get", "test-service", "test-account"],
        )

    def test_preflight_reports_source_without_token_value(self) -> None:
        secret = "preflight-secret"
        with mock.patch.dict(os.environ, {"X_BEARER_TOKEN": secret}, clear=True):
            result = watcher.preflight(self.config)
        self.assertTrue(result["ready_for_live_poll"])
        self.assertEqual(result["token_source"], "environment:X_BEARER_TOKEN")
        self.assertNotIn(secret, json.dumps(result))

    def test_preflight_command_does_not_create_database(self) -> None:
        root = self.root / "read-only-preflight"
        root.mkdir()
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "user_id": "900",
                    "database": "var/watcher.sqlite3",
                    "notifications_enabled": False,
                }
            ),
            encoding="utf-8",
        )
        database = root / "var" / "watcher.sqlite3"
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "xmention_watcher.py",
                    "--config",
                    str(config_path),
                    "preflight",
                ],
            ),
            mock.patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(watcher.main(), 2)
        self.assertFalse(database.exists())

    def test_watchdog_persists_status_and_does_not_repeat_same_alert(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="test_fixture",
        )
        self.assertEqual(watcher.run_watchdog(self.config, loop=False), 0)
        first = watcher.read_json(self.config.alert_file)
        self.assertEqual(first["status"], "healthy")
        first_checked_at = first["checked_at"]
        self.assertEqual(watcher.run_watchdog(self.config, loop=False), 0)
        second = watcher.read_json(self.config.alert_file)
        self.assertEqual(second["checked_at"], first_checked_at)

    def test_launchd_templates_render_with_absolute_paths(self) -> None:
        output = self.root / "launch-agents"
        rendered = render_launchd.render(
            self.config.source_path,
            output,
        )
        self.assertEqual(len(rendered), 5)
        for path in rendered:
            payload = plistlib.loads(path.read_bytes())
            arguments = payload["ProgramArguments"]
            self.assertTrue(all("REPLACE_" not in value for value in arguments))
            self.assertEqual(arguments[2], "--config")
            self.assertEqual(Path(arguments[3]), self.config.source_path)
            self.assertEqual(payload["StandardOutPath"], "/dev/null")
            self.assertEqual(payload["StandardErrorPath"], "/dev/null")
            if path.name.endswith(".poll.plist"):
                expected_interval = self.config.poll_interval_seconds
            elif path.name.endswith(".watchdog.plist"):
                expected_interval = self.config.watchdog_interval_seconds
            elif path.name.endswith(".codex-update.plist"):
                expected_interval = 21600
            else:
                expected_interval = 60
            self.assertEqual(payload["StartInterval"], expected_interval)
        self.assertFalse(
            (output / "com.axrbarsic.xmention.autopilot.plist").exists()
        )


if __name__ == "__main__":
    unittest.main()
