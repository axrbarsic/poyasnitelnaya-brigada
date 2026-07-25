from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import sys
import tempfile
import unittest
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

    def test_acknowledge_removes_event_from_pending_queue(self) -> None:
        watcher.ingest_response(
            self.config,
            self.connection,
            self.fixture,
            source="test_fixture",
        )
        result = watcher.acknowledge_events(
            self.config,
            self.connection,
            ["2080696811623190996"],
        )
        self.assertEqual(result["pending_count"], 2)
        pending = watcher.queued_events(self.connection)
        self.assertNotIn(
            "2080696811623190996",
            [event["event_id"] for event in pending],
        )
        repeated = watcher.acknowledge_events(
            self.config,
            self.connection,
            ["2080696811623190996", "999"],
        )
        self.assertEqual(repeated["acknowledged"], [])
        self.assertEqual(repeated["not_queued"], ["999", "2080696811623190996"])

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

    def test_first_live_poll_establishes_baseline_without_queueing(self) -> None:
        with mock.patch.dict(os.environ, {"X_BEARER_TOKEN": "test-token"}, clear=True):
            result = watcher.poll_live(
                self.config,
                self.connection,
                fetch=lambda url, token, timeout: self.fixture,
            )
        self.assertTrue(result["bootstrap"])
        self.assertEqual(result["observed_count"], 3)
        self.assertEqual(result["new_count"], 0)
        self.assertEqual(result["pending_count"], 0)
        states = {
            row["delivery_state"]
            for row in self.connection.execute(
                "SELECT delivery_state FROM events"
            ).fetchall()
        }
        self.assertEqual(states, {"baseline"})

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
        self.assertEqual(len(rendered), 2)
        for path in rendered:
            payload = plistlib.loads(path.read_bytes())
            arguments = payload["ProgramArguments"]
            self.assertTrue(all("REPLACE_" not in value for value in arguments))
            self.assertEqual(arguments[2], "--config")
            self.assertEqual(Path(arguments[3]), self.config.source_path)
            self.assertEqual(payload["StandardOutPath"], "/dev/null")
            self.assertEqual(payload["StandardErrorPath"], "/dev/null")


if __name__ == "__main__":
    unittest.main()
