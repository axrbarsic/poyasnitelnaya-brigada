from __future__ import annotations

import json
import plistlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts import system_doctor, watcher_constants


class SystemDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        self.home = Path(self.temporary.name) / "home"
        self.root.mkdir()
        self.home.mkdir()
        (self.root / ".git").mkdir()
        (self.root / ".codex").mkdir()
        (self.root / ".codex" / "config.toml").write_text(
            'model = "gpt-5.6-sol"\n',
            encoding="utf-8",
        )
        (self.root / "required.txt").write_text("ok\n", encoding="utf-8")
        (self.root / "personality").mkdir()
        (self.root / "personality" / "policy.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "policy_id": "test",
                    "default_profile": {"instructions": ["Пиши прямо."]},
                    "topic_profiles": [],
                    "conversation_profiles": {},
                    "author_profiles": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.root / "var").mkdir()
        (self.root / "var" / "wake-request.json").write_text(
            json.dumps({"pending_count": 0, "events": []}),
            encoding="utf-8",
        )
        (self.root / "var" / "health.json").write_text(
            json.dumps(
                {
                    "status": "healthy",
                    "consecutive_failures": 0,
                    "last_success_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            ),
            encoding="utf-8",
        )
        helper = self.root / "var" / "keychain-helper"
        helper.write_text("#!/bin/sh\n", encoding="utf-8")
        helper.chmod(0o755)
        database = self.root / "var" / "watcher.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
            connection.commit()
        (self.root / "skill-source").mkdir()
        (self.root / "skill-source" / "SKILL.md").write_text(
            "same\n", encoding="utf-8"
        )
        installed_skill = self.home / ".codex" / "skills" / "x"
        installed_skill.mkdir(parents=True)
        (installed_skill / "SKILL.md").write_text("same\n", encoding="utf-8")
        (self.root / "generation-skill-source").mkdir()
        (self.root / "generation-skill-source" / "SKILL.md").write_text(
            "generation\n", encoding="utf-8"
        )
        installed_generation_skill = (
            self.home / ".codex" / "skills" / "generation"
        )
        installed_generation_skill.mkdir(parents=True)
        (installed_generation_skill / "SKILL.md").write_text(
            "generation\n", encoding="utf-8"
        )
        state_dir = self.home / ".codex"
        state_dir.mkdir(exist_ok=True)
        state = state_dir / "state_1.sqlite"
        with closing(sqlite3.connect(state)) as connection:
            connection.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    archived INTEGER,
                    is_pinned INTEGER,
                    title TEXT,
                    model TEXT,
                    reasoning_effort TEXT,
                    cwd TEXT
                )
                """
            )
            connection.executemany(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("owner", 0, 1, "Owner", "sol", "high", str(self.root)),
                    ("relay", 0, 0, "Relay", "luna", "low", str(self.root)),
                ],
            )
            connection.commit()
        automations = self.home / ".codex" / "automations"
        for identifier, status in (("relay-a", "ACTIVE"), ("old-a", "PAUSED")):
            path = automations / identifier
            path.mkdir(parents=True)
            (path / "automation.toml").write_text(
                "\n".join(
                    [
                        f'id = "{identifier}"',
                        f'status = "{status}"',
                        'target_thread_id = "owner"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
        launch_dir = self.home / "Library" / "LaunchAgents"
        launch_dir.mkdir(parents=True)
        (launch_dir / "agent.plist").write_bytes(
            plistlib.dumps({"Label": "agent"})
        )
        self.config = self.root / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "browser_owner_thread_id": "owner",
                    "codex_project_id": "project",
                    "desktop_relay_mode": "in_app_heartbeat",
                    "event_dispatch_on_new_events": True,
                    "event_dispatch_launchagent_label": "agent",
                }
            ),
            encoding="utf-8",
        )
        self.contract = {
            "schema_version": 1,
            "system_id": "test",
            "canonical_root": str(self.root),
            "git_origin": "https://example.test/repo.git",
            "required_files": ["required.txt"],
            "threads": {
                "browser_owner": {
                    "id": "owner",
                    "title": "Owner",
                    "model": "sol",
                    "minimum_reasoning_effort": "high",
                    "cwd": str(self.root),
                    "must_be_unarchived": True,
                    "unique_unarchived_in_cwd": True,
                    "pin_recommended": True,
                },
                "heartbeat_relay": {
                    "id": "relay",
                    "model": "luna",
                    "reasoning_effort": "low",
                    "cwd": str(self.root),
                    "must_be_unarchived": True,
                },
            },
            "automations": {
                "active": {"id": "relay-a", "status": "ACTIVE"},
                "retired": {"id": "old-a", "status": "PAUSED"},
            },
            "launch_agents": ["agent"],
            "runtime": {
                "database": "var/watcher.sqlite3",
                "wake_file": "var/wake-request.json",
                "keychain_helper": "var/keychain-helper",
                "health_file": "var/health.json",
                "max_poll_age_seconds": 180,
                "healthy_statuses": ["healthy"],
                "event_dispatch_launchagent_label": "agent",
            },
            "skills": [
                {
                    "id": "x_skill",
                    "display_name": "X skill",
                    "source": "skill-source",
                    "installed": ".codex/skills/x",
                },
                {
                    "id": "generation_skill",
                    "display_name": "generation skill",
                    "source": "generation-skill-source",
                    "installed": ".codex/skills/generation",
                },
            ],
            "personality": {
                "tracked_policy": "personality/policy.json",
                "runtime_overrides": "var/personality-overrides.json",
            },
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_contract_schema_versions_one_and_two_are_supported(self) -> None:
        self.assertEqual(
            system_doctor.contract_schema_version({"schema_version": 1}),
            1,
        )
        self.assertEqual(
            system_doctor.contract_schema_version({"schema_version": 2}),
            2,
        )
        with self.assertRaisesRegex(ValueError, "unsupported"):
            system_doctor.contract_schema_version({"schema_version": 3})

    def test_production_contract_matches_database_identity(self) -> None:
        contract_path = (
            Path(__file__).resolve().parents[1]
            / "recovery"
            / "system-contract.json"
        )
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        runtime = contract["runtime"]

        self.assertEqual(
            runtime["database_schema_version"],
            watcher_constants.SCHEMA_VERSION,
        )
        self.assertEqual(
            runtime["database_application_id"],
            watcher_constants.DATABASE_APPLICATION_ID,
        )

    def test_database_integrity_retries_transient_lock(self) -> None:
        database = self.root / "var" / "watcher.sqlite3"
        real_connect = sqlite3.connect
        attempts = 0

        def connect_once_locked(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_connect(*args, **kwargs)

        with (
            mock.patch(
                "scripts.system_doctor.sqlite3.connect",
                side_effect=connect_once_locked,
            ),
            mock.patch("scripts.system_doctor.time.sleep") as sleep,
        ):
            healthy, details = system_doctor.database_integrity(database)

        self.assertTrue(healthy)
        self.assertIsNone(details)
        self.assertEqual(attempts, 2)
        sleep.assert_called_once_with(0.1)

    def test_database_integrity_reports_persistent_error(self) -> None:
        database = self.root / "var" / "watcher.sqlite3"
        with mock.patch(
            "scripts.system_doctor.sqlite3.connect",
            side_effect=sqlite3.DatabaseError("database disk image is malformed"),
        ):
            healthy, details = system_doctor.database_integrity(database)

        self.assertFalse(healthy)
        self.assertEqual(details["attempts"], 1)
        self.assertEqual(details["error_class"], "DatabaseError")
        self.assertFalse(details["transient"])

    def test_database_integrity_rejects_stale_schema_version(self) -> None:
        database = self.root / "var" / "watcher.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute(
            f"PRAGMA user_version={watcher_constants.SCHEMA_VERSION - 1}"
        )
        connection.close()

        healthy, details = system_doctor.database_integrity(
            database,
            expected_schema_version=watcher_constants.SCHEMA_VERSION,
        )

        self.assertFalse(healthy)
        self.assertFalse(details["schema_current"])
        self.assertEqual(
            details["expected_schema_version"],
            watcher_constants.SCHEMA_VERSION,
        )

    def test_contract_reports_schema_mismatch_as_migration_failure(self) -> None:
        self.contract["runtime"]["database_schema_version"] = (
            watcher_constants.SCHEMA_VERSION
        )
        with mock.patch(
            "scripts.system_doctor.git_origin",
            return_value="https://example.test/repo.git",
        ):
            checks = system_doctor.check_contract(
                self.root,
                self.home,
                self.contract,
                self.config,
            )

        database_check = next(
            check
            for check in checks
            if check.identifier == "runtime.database"
        )
        self.assertEqual(database_check.status, "fail")
        self.assertIn("Версия схемы", database_check.summary)
        self.assertIn("миграции", database_check.repair)

    def test_database_integrity_rejects_foreign_application_id(self) -> None:
        database = self.root / "var" / "watcher.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA application_id=123456")
        connection.close()

        healthy, details = system_doctor.database_integrity(
            database,
            expected_application_id=watcher_constants.DATABASE_APPLICATION_ID,
        )

        self.assertFalse(healthy)
        self.assertFalse(details["application_matches"])
        self.assertEqual(details["application_id"], 123456)

    def test_database_integrity_retries_one_open_failure(self) -> None:
        database = self.root / "var" / "watcher.sqlite3"
        real_connect = sqlite3.connect
        attempts = 0

        def connect_after_one_open_failure(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError(
                    "unable to open database file"
                )
            return real_connect(*args, **kwargs)

        with (
            mock.patch(
                "scripts.system_doctor.sqlite3.connect",
                side_effect=connect_after_one_open_failure,
            ),
            mock.patch("scripts.system_doctor.time.sleep") as sleep,
        ):
            healthy, details = system_doctor.database_integrity(database)

        self.assertTrue(healthy)
        self.assertIsNone(details)
        self.assertEqual(attempts, 2)
        sleep.assert_called_once_with(0.1)

    def test_database_integrity_persistent_open_failure_is_fail(self) -> None:
        database = self.root / "var" / "watcher.sqlite3"
        with (
            mock.patch(
                "scripts.system_doctor.sqlite3.connect",
                side_effect=sqlite3.OperationalError(
                    "unable to open database file"
                ),
            ),
            mock.patch("scripts.system_doctor.time.sleep") as sleep,
        ):
            healthy, details = system_doctor.database_integrity(database)

        self.assertFalse(healthy)
        self.assertEqual(details["attempts"], 3)
        self.assertTrue(details["retryable"])
        self.assertFalse(details["transient"])
        self.assertEqual(sleep.call_count, 2)

    def test_relay_progress_detects_unclaimed_stalled_queue(self) -> None:
        now = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
        check = system_doctor.relay_progress_check(
            pending_count=2,
            dispatch_state={
                "status": "desktop_ready_waiting_relay",
                "waiting_since": "2026-07-29T14:56:59Z",
            },
            owner=None,
            max_wait_seconds=180,
            now=now,
        )

        self.assertEqual(check.status, "fail")
        self.assertEqual(check.identifier, "runtime.relay_progress")
        self.assertEqual(check.details["age_seconds"], 181.0)

    def test_production_relay_grace_covers_heartbeat_startup(self) -> None:
        contract_path = (
            Path(__file__).resolve().parents[1]
            / "recovery"
            / "system-contract.json"
        )
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        max_wait_seconds = contract["runtime"]["max_relay_wait_seconds"]
        now = datetime(2026, 8, 2, 9, 34, tzinfo=timezone.utc)
        requested_at = now - timedelta(seconds=240)
        dispatch_state = {
            "status": "desktop_ready_waiting_relay",
            "waiting_since": requested_at.isoformat(),
            "work_kind": "x",
        }
        event_dispatch_state = {
            "status": "dispatch_kicked",
            "reason": "claim_completed_with_pending_queue",
            "requested_at": requested_at.isoformat(),
            "event_ids": ["oldest"],
        }

        relay = system_doctor.relay_progress_check(
            pending_count=1,
            dispatch_state=dispatch_state,
            event_dispatch_state=event_dispatch_state,
            required_event_id="oldest",
            owner=None,
            max_wait_seconds=max_wait_seconds,
            now=now,
        )
        latency = system_doctor.queue_latency_check(
            events=[
                {
                    "id": "oldest",
                    "first_seen_at": (
                        now - timedelta(seconds=600)
                    ).isoformat(),
                }
            ],
            owner=None,
            dispatch_state=dispatch_state,
            event_dispatch_state=event_dispatch_state,
            delivery_grace_seconds=max_wait_seconds,
            max_age_seconds=300,
            now=now,
        )

        self.assertEqual(max_wait_seconds, 300)
        self.assertEqual(relay.status, "pass")
        self.assertEqual(latency.status, "warn")
        self.assertTrue(latency.details["active_x_delivery"])

        expired_at = requested_at + timedelta(
            seconds=max_wait_seconds + 1
        )
        expired_relay = system_doctor.relay_progress_check(
            pending_count=1,
            dispatch_state=dispatch_state,
            event_dispatch_state=event_dispatch_state,
            required_event_id="oldest",
            owner=None,
            max_wait_seconds=max_wait_seconds,
            now=expired_at,
        )

        self.assertEqual(expired_relay.status, "fail")
        self.assertEqual(expired_relay.details["age_seconds"], 301.0)

    def test_relay_progress_accepts_active_owner(self) -> None:
        check = system_doctor.relay_progress_check(
            pending_count=2,
            dispatch_state={
                "status": "desktop_ready_waiting_relay",
                "waiting_since": "2026-07-29T14:00:00Z",
            },
            owner={"event_ids": ["1", "2"]},
            max_wait_seconds=180,
        )

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["owner_event_ids"], ["1", "2"])

    def test_relay_progress_accepts_active_repair_handoff(self) -> None:
        check = system_doctor.relay_progress_check(
            pending_count=2,
            dispatch_state={
                "status": "work_in_progress",
                "work_kind": "repair",
            },
            owner=None,
            max_wait_seconds=180,
            now=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["work_kind"], "repair")

    def test_relay_progress_accepts_repair_waiting_gate(self) -> None:
        check = system_doctor.relay_progress_check(
            pending_count=2,
            dispatch_state={
                "status": "repair_waiting",
                "work_kind": "x",
            },
            owner=None,
            max_wait_seconds=180,
        )

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["dispatch_status"], "repair_waiting")

    def test_relay_progress_ignores_resource_deferred_queue(self) -> None:
        check = system_doctor.relay_progress_check(
            pending_count=2,
            dispatch_state={
                "status": "deferred_resources",
                "checked_at": "2026-07-29T14:00:00Z",
            },
            owner=None,
            max_wait_seconds=180,
        )

        self.assertEqual(check.status, "pass")
        self.assertEqual(
            check.details["dispatch_status"],
            "deferred_resources",
        )

    def test_relay_progress_rejects_failed_dispatch_with_queue(self) -> None:
        check = system_doctor.relay_progress_check(
            pending_count=1,
            dispatch_state={
                "status": "desktop_launch_failed",
                "checked_at": "2026-07-29T14:59:59Z",
            },
            owner=None,
            max_wait_seconds=180,
            now=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(check.status, "fail")
        self.assertEqual(check.identifier, "runtime.relay_progress")

    def test_relay_progress_accepts_fresh_completion_dispatch(self) -> None:
        check = system_doctor.relay_progress_check(
            pending_count=3,
            dispatch_state={
                "status": "leased_waiting",
                "checked_at": "2026-07-29T15:00:00Z",
            },
            event_dispatch_state={
                "status": "dispatch_kicked",
                "reason": "claim_completed_with_pending_queue",
                "requested_at": "2026-07-29T14:59:59Z",
                "event_ids": ["123", "124", "125"],
            },
            required_event_id="123",
            owner=None,
            max_wait_seconds=180,
            now=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(check.status, "pass")
        self.assertEqual(
            check.details["event_dispatch_status"],
            "dispatch_kicked",
        )
        self.assertTrue(check.details["event_dispatch_covers_oldest"])

    def test_relay_progress_rejects_expired_completion_dispatch(self) -> None:
        check = system_doctor.relay_progress_check(
            pending_count=1,
            dispatch_state={"status": "leased_waiting"},
            event_dispatch_state={
                "status": "dispatch_kicked",
                "reason": "claim_completed_with_pending_queue",
                "requested_at": "2026-07-29T14:56:59Z",
                "event_ids": ["123"],
            },
            required_event_id="123",
            owner=None,
            max_wait_seconds=180,
            now=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(check.status, "fail")
        self.assertEqual(
            check.details["event_dispatch_age_seconds"],
            181.0,
        )

    def test_queue_latency_detects_unowned_stale_event(self) -> None:
        check = system_doctor.queue_latency_check(
            events=[
                {
                    "id": "123",
                    "first_seen_at": "2026-07-29T14:54:59Z",
                }
            ],
            owner=None,
            max_age_seconds=300,
            now=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(check.status, "fail")
        self.assertEqual(check.details["oldest_event_id"], "123")
        self.assertEqual(check.details["age_seconds"], 301.0)

    def test_queue_latency_warns_for_owned_long_running_event(self) -> None:
        check = system_doctor.queue_latency_check(
            events=[
                {
                    "id": "123",
                    "first_seen_at": "2026-07-29T14:54:59Z",
                },
                {
                    "id": "456",
                    "first_seen_at": "2026-07-29T14:59:59Z",
                },
            ],
            owner={"event_ids": ["123"]},
            max_age_seconds=300,
            now=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(check.status, "warn")
        self.assertTrue(check.details["owned"])

    def test_queue_latency_warns_while_another_bounded_batch_is_owned(
        self,
    ) -> None:
        check = system_doctor.queue_latency_check(
            events=[
                {
                    "id": "123",
                    "first_seen_at": "2026-07-29T14:54:59Z",
                },
                {
                    "id": "456",
                    "first_seen_at": "2026-07-29T14:59:59Z",
                },
            ],
            owner={
                "event_ids": ["456"],
                "lease_expires_at": "2026-07-29T15:30:00Z",
            },
            max_age_seconds=300,
            now=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(check.status, "warn")
        self.assertTrue(check.details["active_owner"])
        self.assertFalse(check.details["owned"])

    def test_queue_latency_fails_for_expired_bounded_batch(self) -> None:
        check = system_doctor.queue_latency_check(
            events=[
                {
                    "id": "123",
                    "first_seen_at": "2026-07-29T14:54:59Z",
                }
            ],
            owner={
                "event_ids": ["456"],
                "lease_expires_at": "2026-07-29T14:59:59Z",
            },
            max_age_seconds=300,
            now=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(check.status, "fail")
        self.assertFalse(check.details["active_owner"])

    def test_queue_latency_never_calls_expired_oldest_owner_active(self) -> None:
        check = system_doctor.queue_latency_check(
            events=[
                {
                    "id": "123",
                    "first_seen_at": "2026-07-29T14:54:59Z",
                }
            ],
            owner={
                "event_ids": ["123"],
                "lease_expires_at": "2026-07-29T14:59:59Z",
            },
            max_age_seconds=300,
            now=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(check.status, "fail")
        self.assertTrue(check.details["owned"])
        self.assertFalse(check.details["active_owner"])
        self.assertIn("без активного owner", check.summary)

    def test_queue_latency_warns_during_active_repair_handoff(self) -> None:
        check = system_doctor.queue_latency_check(
            events=[
                {
                    "id": "123",
                    "first_seen_at": "2026-07-29T14:54:59Z",
                }
            ],
            owner=None,
            supervisor_state={
                "incident": {
                    "status": "work_in_progress",
                    "owner": {
                        "claim_token": "repair-token",
                        "lease_expires_at": "2026-07-29T15:30:00Z",
                    },
                }
            },
            max_age_seconds=300,
            now=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(check.status, "warn")
        self.assertTrue(check.details["active_repair"])
        self.assertFalse(check.details["active_owner"])

    def test_queue_latency_fails_for_expired_repair_handoff(self) -> None:
        check = system_doctor.queue_latency_check(
            events=[
                {
                    "id": "123",
                    "first_seen_at": "2026-07-29T14:54:59Z",
                }
            ],
            owner=None,
            supervisor_state={
                "incident": {
                    "status": "work_in_progress",
                    "owner": {
                        "claim_token": "repair-token",
                        "lease_expires_at": "2026-07-29T14:59:59Z",
                    },
                }
            },
            max_age_seconds=300,
            now=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(check.status, "fail")
        self.assertFalse(check.details["active_repair"])

    def test_queue_latency_warns_during_fresh_x_delivery_grace(self) -> None:
        check = system_doctor.queue_latency_check(
            events=[
                {
                    "id": "123",
                    "first_seen_at": "2026-07-29T14:50:00Z",
                }
            ],
            owner=None,
            dispatch_state={
                "status": "desktop_ready_waiting_relay",
                "work_kind": "x",
                "waiting_since": "2026-07-29T14:59:00Z",
                "event_ids": ["123"],
            },
            delivery_grace_seconds=180,
            max_age_seconds=300,
            now=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(check.status, "warn")
        self.assertTrue(check.details["active_x_delivery"])
        self.assertTrue(check.details["delivery_covers_oldest"])
        self.assertEqual(check.details["delivery_age_seconds"], 60.0)

    def test_queue_latency_fails_after_x_delivery_grace_expires(self) -> None:
        check = system_doctor.queue_latency_check(
            events=[
                {
                    "id": "123",
                    "first_seen_at": "2026-07-29T14:50:00Z",
                }
            ],
            owner=None,
            dispatch_state={
                "status": "desktop_ready_waiting_relay",
                "work_kind": "x",
                "waiting_since": "2026-07-29T14:56:59Z",
                "checked_at": "2026-07-29T15:00:00Z",
                "event_ids": ["123"],
            },
            delivery_grace_seconds=180,
            max_age_seconds=300,
            now=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(check.status, "fail")
        self.assertFalse(check.details["active_x_delivery"])
        self.assertEqual(check.details["delivery_age_seconds"], 181.0)

    def test_queue_latency_warns_for_fresh_completion_dispatch(self) -> None:
        check = system_doctor.queue_latency_check(
            events=[
                {
                    "id": "123",
                    "first_seen_at": "2026-07-29T14:50:00Z",
                }
            ],
            owner=None,
            dispatch_state={
                "status": "leased_waiting",
                "work_kind": "x",
                "event_ids": ["122"],
            },
            event_dispatch_state={
                "status": "dispatch_requested",
                "reason": "claim_completed_with_pending_queue",
                "requested_at": "2026-07-29T14:59:59Z",
                "event_ids": ["123"],
            },
            delivery_grace_seconds=180,
            max_age_seconds=300,
            now=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(check.status, "warn")
        self.assertTrue(check.details["active_x_delivery"])
        self.assertEqual(check.details["delivery_source"], "event_dispatch")
        self.assertTrue(check.details["event_dispatch_covers_oldest"])

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_contract_passes_fresh_x_delivery_to_queue_latency(
        self,
        _git_origin: mock.Mock,
    ) -> None:
        current = datetime.now(timezone.utc)
        (self.root / "var" / "wake-request.json").write_text(
            json.dumps(
                {
                    "pending_count": 1,
                    "events": [
                        {
                            "id": "123",
                            "first_seen_at": (
                                current - timedelta(seconds=301)
                            ).isoformat().replace("+00:00", "Z"),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.root / "var" / "dispatch.json").write_text(
            json.dumps(
                {
                    "status": "desktop_ready_waiting_relay",
                    "work_kind": "x",
                    "waiting_since": current.isoformat(),
                    "checked_at": current.isoformat(),
                    "event_ids": ["123"],
                }
            ),
            encoding="utf-8",
        )
        (self.root / "var" / "autopilot.json").write_text(
            json.dumps({"owner": None}),
            encoding="utf-8",
        )
        self.contract["runtime"].update(
            {
                "dispatch_state_file": "var/dispatch.json",
                "autopilot_state_file": "var/autopilot.json",
                "max_dispatch_age_seconds": 180,
                "max_relay_wait_seconds": 180,
                "max_queue_age_seconds": 300,
            }
        )

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        latency = next(
            check
            for check in checks
            if check.identifier == "runtime.queue_latency"
        )
        self.assertEqual(latency.status, "warn")
        self.assertTrue(latency.details["active_x_delivery"])

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_contract_reads_fresh_completion_dispatch_state(
        self,
        _git_origin: mock.Mock,
    ) -> None:
        current = datetime.now(timezone.utc)
        (self.root / "var" / "wake-request.json").write_text(
            json.dumps(
                {
                    "pending_count": 1,
                    "events": [
                        {
                            "id": "123",
                            "first_seen_at": (
                                current - timedelta(seconds=301)
                            ).isoformat().replace("+00:00", "Z"),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.root / "var" / "dispatch.json").write_text(
            json.dumps(
                {
                    "status": "leased_waiting",
                    "work_kind": "x",
                    "checked_at": current.isoformat(),
                    "event_ids": ["122"],
                }
            ),
            encoding="utf-8",
        )
        (self.root / "var" / "event-dispatch.json").write_text(
            json.dumps(
                {
                    "status": "dispatch_kicked",
                    "reason": "claim_completed_with_pending_queue",
                    "requested_at": current.isoformat(),
                    "event_ids": ["123"],
                }
            ),
            encoding="utf-8",
        )
        (self.root / "var" / "autopilot.json").write_text(
            json.dumps({"owner": None}),
            encoding="utf-8",
        )
        self.contract["runtime"].update(
            {
                "dispatch_state_file": "var/dispatch.json",
                "event_dispatch_state_file": "var/event-dispatch.json",
                "autopilot_state_file": "var/autopilot.json",
                "max_dispatch_age_seconds": 180,
                "max_relay_wait_seconds": 180,
                "max_queue_age_seconds": 300,
            }
        )

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        relay = next(
            check
            for check in checks
            if check.identifier == "runtime.relay_progress"
        )
        latency = next(
            check
            for check in checks
            if check.identifier == "runtime.queue_latency"
        )
        self.assertEqual(relay.status, "pass")
        self.assertEqual(latency.status, "warn")
        self.assertEqual(latency.details["delivery_source"], "event_dispatch")

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_contract_reads_default_supervisor_state_for_active_repair(
        self,
        _git_origin: mock.Mock,
    ) -> None:
        current = datetime.now(timezone.utc)
        (self.root / "var" / "wake-request.json").write_text(
            json.dumps(
                {
                    "pending_count": 1,
                    "events": [
                        {
                            "id": "123",
                            "first_seen_at": (
                                current - timedelta(seconds=301)
                            ).isoformat().replace("+00:00", "Z"),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.root / "var" / "autopilot-supervisor.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "incident": {
                        "status": "work_in_progress",
                        "owner": {
                            "claim_token": "repair-token",
                            "lease_expires_at": (
                                current + timedelta(minutes=30)
                            ).isoformat().replace("+00:00", "Z"),
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        self.contract["runtime"]["max_queue_age_seconds"] = 300

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        check = next(
            item
            for item in checks
            if item.identifier == "runtime.queue_latency"
        )
        self.assertEqual(check.status, "warn")
        self.assertTrue(check.details["active_repair"])

    def test_queue_latency_rejects_invalid_event_shape(self) -> None:
        check = system_doctor.queue_latency_check(
            events=[None],
            owner=None,
            max_age_seconds=300,
        )

        self.assertEqual(check.status, "fail")
        self.assertEqual(check.details["invalid_event_ids"], ["<invalid>"])

    def test_queue_latency_rejects_missing_first_seen_at(self) -> None:
        check = system_doctor.queue_latency_check(
            events=[{"id": "123", "first_seen_at": None}],
            owner=None,
            max_age_seconds=300,
        )

        self.assertEqual(check.status, "fail")
        self.assertEqual(check.details["invalid_event_ids"], ["123"])

    def test_contract_treats_persistent_busy_as_warning(self) -> None:
        with (
            mock.patch(
                "scripts.system_doctor.git_origin",
                return_value="https://example.test/repo.git",
            ),
            mock.patch(
                "scripts.system_doctor.database_integrity",
                return_value=(
                    False,
                    {
                        "attempts": 3,
                        "error_class": "OperationalError",
                        "error_message": "database is locked",
                        "transient": True,
                    },
                ),
            ),
        ):
            checks = system_doctor.check_contract(
                self.root,
                self.home,
                self.contract,
                self.config,
            )

        database_check = next(
            check
            for check in checks
            if check.identifier == "runtime.database"
        )
        self.assertEqual(database_check.status, "warn")
        self.assertIn("повреждение не подтверждено", database_check.summary)

    def test_contract_treats_persistent_open_failure_as_warning(self) -> None:
        with (
            mock.patch(
                "scripts.system_doctor.git_origin",
                return_value="https://example.test/repo.git",
            ),
            mock.patch(
                "scripts.system_doctor.database_integrity",
                return_value=(
                    False,
                    {
                        "attempts": 3,
                        "error_class": "OperationalError",
                        "error_message": "unable to open database file",
                        "retryable": True,
                        "transient": False,
                    },
                ),
            ),
        ):
            checks = system_doctor.check_contract(
                self.root,
                self.home,
                self.contract,
                self.config,
            )

        database_check = next(
            check
            for check in checks
            if check.identifier == "runtime.database"
        )
        self.assertEqual(database_check.status, "warn")
        self.assertIn("повреждение не подтверждено", database_check.summary)
        self.assertIn("восстановления доступа", database_check.repair)

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_green_contract_passes(self, _git_origin: mock.Mock) -> None:
        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        failures = [check for check in checks if check.status == "fail"]
        self.assertEqual(failures, [])
        skill_statuses = {
            check.identifier: check.status
            for check in checks
            if check.identifier.startswith("deployment.")
        }
        self.assertEqual(skill_statuses["deployment.x_skill"], "pass")
        self.assertEqual(skill_statuses["deployment.generation_skill"], "pass")

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_disabled_event_dispatch_fails(
        self,
        _git_origin: mock.Mock,
    ) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["event_dispatch_on_new_events"] = False
        self.config.write_text(json.dumps(config), encoding="utf-8")

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        check = next(
            item
            for item in checks
            if item.identifier == "config.event_dispatch"
        )
        self.assertEqual(check.status, "fail")

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_locked_keychain_accessibility_fails(
        self,
        _git_origin: mock.Mock,
    ) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["keychain_service"] = "test-service"
        config["keychain_account"] = "test-account"
        self.config.write_text(json.dumps(config), encoding="utf-8")
        helper = self.root / "var" / "keychain-helper"
        marker = self.root / "helper-was-executed"
        helper.write_text(
            "#!/bin/sh\n"
            f"touch '{marker}'\n"
            "exit 2\n",
            encoding="utf-8",
        )
        helper.chmod(0o755)

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        accessibility = next(
            check
            for check in checks
            if check.identifier == "runtime.keychain_accessibility"
        )
        self.assertEqual(accessibility.status, "fail")
        self.assertIn("ensure-after-first-unlock", accessibility.repair)
        bundle = next(
            check
            for check in checks
            if check.identifier == "runtime.keychain_bundle"
        )
        self.assertEqual(bundle.status, "fail")
        self.assertFalse(marker.exists())

    @mock.patch(
        "scripts.system_doctor.keychain_bundle.verify_bundle",
    )
    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_signed_keychain_bundle_and_accessibility_pass(
        self,
        _git_origin: mock.Mock,
        _verify_bundle: mock.Mock,
    ) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["keychain_service"] = "test-service"
        config["keychain_account"] = "test-account"
        self.config.write_text(json.dumps(config), encoding="utf-8")
        helper = self.root / "var" / "keychain-helper"
        _verify_bundle.return_value = (
            system_doctor.keychain_bundle.BundleVerification(
                ok=True,
                app_path="/tmp/XMentionKeychainHelper.app",
                executable_path=str(helper.resolve()),
                bundle_id="com.axrbarsic.xmention.keychain-helper",
                application_id=(
                    "J6MW4855LU.com.axrbarsic.xmention.keychain-helper"
                ),
                profile_name="Mac Team Provisioning Profile",
                profile_expires_at="2027-01-01T00:00:00+00:00",
                errors=(),
            )
        )

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        statuses = {
            check.identifier: check.status
            for check in checks
            if check.identifier.startswith("runtime.keychain_")
        }
        self.assertEqual(statuses["runtime.keychain_helper"], "pass")
        self.assertEqual(statuses["runtime.keychain_bundle"], "pass")
        self.assertEqual(statuses["runtime.keychain_accessibility"], "pass")

    def prepare_degraded_poll(
        self,
        *,
        source: str = "x_api_conversation_tail",
        tail_success_at: str | None = None,
        consecutive_failures: int = 1,
        error_class: str = "timeout",
        error_message: str = "The read operation timed out",
    ) -> None:
        now = (
            datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        (self.root / "var" / "health.json").write_text(
            json.dumps(
                {
                    "status": "degraded",
                    "consecutive_failures": consecutive_failures,
                    "last_success_at": now,
                    "last_error_class": error_class,
                    "last_error_message": error_message,
                }
            ),
            encoding="utf-8",
        )
        database = self.root / "var" / "watcher.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                """
                CREATE TABLE poll_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    new_count INTEGER NOT NULL,
                    error_class TEXT,
                    error_message TEXT
                )
                """
            )
            connection.execute(
                "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                """
                INSERT INTO poll_runs(
                    started_at, completed_at, source, status, new_count,
                    error_class, error_message
                ) VALUES (?, ?, ?, 'failure', 0, ?, ?)
                """,
                (now, now, source, error_class, error_message),
            )
            connection.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                (
                    "conversation_tail_last_success_at",
                    tail_success_at or now,
                ),
            )
            connection.commit()

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_recent_tail_timeout_warns_without_failing(
        self, _git_origin: mock.Mock
    ) -> None:
        self.prepare_degraded_poll()

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        poll = next(
            check
            for check in checks
            if check.identifier == "runtime.poll_health"
        )
        self.assertEqual(poll.status, "warn")
        self.assertEqual(
            poll.details["latest_source"],
            "x_api_conversation_tail",
        )

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_stale_tail_timeout_still_fails(
        self, _git_origin: mock.Mock
    ) -> None:
        self.prepare_degraded_poll(
            tail_success_at="2020-01-01T00:00:00Z"
        )

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        poll = next(
            check
            for check in checks
            if check.identifier == "runtime.poll_health"
        )
        self.assertEqual(poll.status, "fail")

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_recent_primary_poll_timeout_warns_without_failing(
        self, _git_origin: mock.Mock
    ) -> None:
        self.prepare_degraded_poll(source="x_api")

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        poll = next(
            check
            for check in checks
            if check.identifier == "runtime.poll_health"
        )
        self.assertEqual(poll.status, "warn")
        self.assertEqual(poll.details["latest_source"], "x_api")

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_recent_primary_tls_handshake_timeout_canary_warns(
        self, _git_origin: mock.Mock
    ) -> None:
        self.prepare_degraded_poll(
            source="x_api",
            error_class="URLError",
            error_message=(
                "<urlopen error _ssl.c:1112: "
                "The handshake operation timed out>"
            ),
        )

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        poll = next(
            check
            for check in checks
            if check.identifier == "runtime.poll_health"
        )
        self.assertEqual(poll.status, "warn")
        self.assertEqual(poll.details["consecutive_failures"], 1)
        self.assertEqual(poll.details["last_error_class"], "URLError")

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_repeated_primary_poll_timeout_still_fails(
        self, _git_origin: mock.Mock
    ) -> None:
        self.prepare_degraded_poll(
            source="x_api",
            consecutive_failures=2,
        )

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        poll = next(
            check
            for check in checks
            if check.identifier == "runtime.poll_health"
        )
        self.assertEqual(poll.status, "fail")

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_archived_owner_fails_closed(self, _git_origin: mock.Mock) -> None:
        state = self.home / ".codex" / "state_1.sqlite"
        with closing(sqlite3.connect(state)) as connection:
            connection.execute(
                "UPDATE threads SET archived = 1 WHERE id = 'owner'"
            )
            connection.commit()

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        owner = next(
            check
            for check in checks
            if check.identifier == "thread.browser_owner"
        )
        self.assertEqual(owner.status, "fail")
        self.assertIn("archived", owner.details)

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_duplicate_owner_role_fails_cardinality(
        self, _git_origin: mock.Mock
    ) -> None:
        state = self.home / ".codex" / "state_1.sqlite"
        with closing(sqlite3.connect(state)) as connection:
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "owner-copy",
                    0,
                    0,
                    "Owner",
                    "sol",
                    "high",
                    str(self.root),
                ),
            )
            connection.commit()

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        cardinality = next(
            check
            for check in checks
            if check.identifier == "thread.browser_owner.cardinality"
        )
        self.assertEqual(cardinality.status, "fail")
        self.assertEqual(
            cardinality.details["actual"],
            ["owner", "owner-copy"],
        )

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_owner_effort_above_minimum_passes(
        self, _git_origin: mock.Mock
    ) -> None:
        state = self.home / ".codex" / "state_1.sqlite"
        with closing(sqlite3.connect(state)) as connection:
            connection.execute(
                "UPDATE threads SET reasoning_effort = 'max' "
                "WHERE id = 'owner'"
            )
            connection.commit()

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        owner = next(
            check
            for check in checks
            if check.identifier == "thread.browser_owner"
        )
        self.assertEqual(owner.status, "pass")

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_owner_effort_below_minimum_fails(
        self, _git_origin: mock.Mock
    ) -> None:
        state = self.home / ".codex" / "state_1.sqlite"
        with closing(sqlite3.connect(state)) as connection:
            connection.execute(
                "UPDATE threads SET reasoning_effort = 'medium' "
                "WHERE id = 'owner'"
            )
            connection.commit()

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        owner = next(
            check
            for check in checks
            if check.identifier == "thread.browser_owner"
        )
        self.assertEqual(owner.status, "fail")
        self.assertEqual(
            owner.details["reasoning_effort"],
            {"actual": "medium", "minimum": "high"},
        )

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_dynamic_owner_role_and_automation_target_follow_config(
        self, _git_origin: mock.Mock
    ) -> None:
        owner = self.contract["threads"]["browser_owner"]
        owner.pop("id")
        owner["id_source"] = "config.browser_owner_thread_id"
        self.contract["automations"]["active"][
            "target_thread_role"
        ] = "browser_owner"

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        thread_check = next(
            check for check in checks if check.identifier == "thread.browser_owner"
        )
        automation_check = next(
            check for check in checks if check.identifier == "automation.active"
        )
        self.assertEqual(thread_check.status, "pass")
        self.assertEqual(automation_check.status, "pass")

        path = self.home / ".codex" / "automations" / "relay-a" / "automation.toml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'target_thread_id = "owner"',
                'target_thread_id = "other"',
            ),
            encoding="utf-8",
        )
        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )
        automation_check = next(
            check for check in checks if check.identifier == "automation.active"
        )
        self.assertEqual(automation_check.status, "fail")
        self.assertEqual(
            automation_check.details["target_thread_id"]["expected"],
            "owner",
        )

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_inbound_throughput_config_matches_versioned_contract(
        self, _git_origin: mock.Mock
    ) -> None:
        self.contract["runtime"].update(
            {
                "inbound_claim_max_events": 3,
                "inbound_max_parallel_read_tabs": 3,
            }
        )
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["autopilot_max_claim_events"] = 3
        config["autopilot_max_parallel_read_tabs"] = 3
        self.config.write_text(json.dumps(config), encoding="utf-8")

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )
        throughput = next(
            check
            for check in checks
            if check.identifier == "config.inbound_throughput"
        )
        self.assertEqual(throughput.status, "pass")

        config["autopilot_max_claim_events"] = 1
        self.config.write_text(json.dumps(config), encoding="utf-8")
        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )
        throughput = next(
            check
            for check in checks
            if check.identifier == "config.inbound_throughput"
        )
        self.assertEqual(throughput.status, "fail")
        self.assertEqual(throughput.details["actual_claim_events"], 1)

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_malformed_automation_is_a_check_failure_not_doctor_crash(
        self, _git_origin: mock.Mock
    ) -> None:
        path = (
            self.home
            / ".codex"
            / "automations"
            / "relay-a"
            / "automation.toml"
        )
        path.write_text("[unsupported]\nid = \"relay-a\"\n", encoding="utf-8")

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        automation = next(
            check
            for check in checks
            if check.identifier == "automation.active"
        )
        self.assertEqual(automation.status, "fail")
        self.assertIn("ValueError", automation.details["error"])

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_open_owner_rotation_warns_then_fails_when_stale(
        self, _git_origin: mock.Mock
    ) -> None:
        state_path = self.root / "var" / "rotation.json"
        self.contract["runtime"]["owner_rotation_state_file"] = (
            "var/rotation.json"
        )
        self.contract["runtime"]["max_owner_rotation_age_seconds"] = 900

        def write_state(updated_at: datetime) -> None:
            state_path.write_text(
                json.dumps(
                    {
                        "generation": 0,
                        "transaction": {
                            "phase": "thread_created",
                            "old_thread_id": "owner",
                            "new_thread_id": "replacement",
                            "updated_at": updated_at.isoformat(),
                        },
                    }
                ),
                encoding="utf-8",
            )

        write_state(datetime.now(timezone.utc))
        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )
        rotation = next(
            check
            for check in checks
            if check.identifier == "runtime.owner_rotation"
        )
        self.assertEqual(rotation.status, "warn")

        write_state(datetime.now(timezone.utc) - timedelta(minutes=20))
        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )
        rotation = next(
            check
            for check in checks
            if check.identifier == "runtime.owner_rotation"
        )
        self.assertEqual(rotation.status, "fail")

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_active_owner_demotes_stale_dispatcher_to_warning(
        self, _git_origin: mock.Mock
    ) -> None:
        self.contract["runtime"].update(
            {
                "dispatch_state_file": "var/dispatch.json",
                "autopilot_state_file": "var/autopilot.json",
                "max_dispatch_age_seconds": 180,
            }
        )
        stale = datetime.now(timezone.utc) - timedelta(minutes=10)
        (self.root / "var" / "dispatch.json").write_text(
            json.dumps({"checked_at": stale.isoformat()}),
            encoding="utf-8",
        )
        autopilot = self.root / "var" / "autopilot.json"
        autopilot.write_text(
            json.dumps(
                {
                    "owner": {
                        "event_ids": ["123"],
                        "lease_expires_at": (
                            datetime.now(timezone.utc) + timedelta(minutes=10)
                        ).isoformat(),
                    }
                }
            ),
            encoding="utf-8",
        )

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )
        dispatch = next(
            check
            for check in checks
            if check.identifier == "runtime.dispatch_health"
        )
        self.assertEqual(dispatch.status, "warn")

        autopilot.write_text(json.dumps({"owner": None}), encoding="utf-8")
        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )
        dispatch = next(
            check
            for check in checks
            if check.identifier == "runtime.dispatch_health"
        )
        self.assertEqual(dispatch.status, "fail")

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_launchagent_runtime_uses_installed_interpreter(
        self, _git_origin: mock.Mock
    ) -> None:
        entrypoint = self.root / "entrypoint.py"
        entrypoint.write_text(
            "import argparse\nargparse.ArgumentParser().parse_args()\n",
            encoding="utf-8",
        )
        installed = self.home / "Library" / "LaunchAgents" / "agent.plist"
        installed.write_bytes(
            plistlib.dumps(
                {
                    "Label": "agent",
                    "ProgramArguments": [
                        sys.executable,
                        str(entrypoint),
                    ],
                }
            )
        )
        self.contract["launch_agent_programs"] = {
            "agent": "entrypoint.py"
        }

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        runtime = next(
            check
            for check in checks
            if check.identifier == "launchagent.runtime_compatibility"
        )
        self.assertEqual(runtime.status, "pass")
        self.assertEqual(runtime.details["probed_labels"], ["agent"])

        entrypoint.write_text(
            "import dependency_that_does_not_exist\n",
            encoding="utf-8",
        )
        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )
        runtime = next(
            check
            for check in checks
            if check.identifier == "launchagent.runtime_compatibility"
        )
        self.assertEqual(runtime.status, "fail")
        self.assertEqual(runtime.details["failures"][0]["label"], "agent")
        self.assertNotEqual(
            runtime.details["failures"][0]["returncode"],
            0,
        )

    @mock.patch(
        "scripts.system_doctor.git_origin",
        return_value="https://example.test/repo.git",
    )
    def test_queue_count_mismatch_is_detected(
        self, _git_origin: mock.Mock
    ) -> None:
        (self.root / "var" / "wake-request.json").write_text(
            json.dumps({"pending_count": 2, "events": []}),
            encoding="utf-8",
        )

        checks = system_doctor.check_contract(
            self.root,
            self.home,
            self.contract,
            self.config,
        )

        queue = next(
            check for check in checks if check.identifier == "runtime.queue"
        )
        self.assertEqual(queue.status, "fail")


if __name__ == "__main__":
    unittest.main()
