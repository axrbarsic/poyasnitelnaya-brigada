from __future__ import annotations

import json
import plistlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import system_doctor


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
                    model TEXT,
                    reasoning_effort TEXT,
                    cwd TEXT
                )
                """
            )
            connection.executemany(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("owner", 0, 1, "sol", "high", str(self.root)),
                    ("relay", 0, 0, "luna", "low", str(self.root)),
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
                    "desktop_relay_mode": "in_app_heartbeat",
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
                    "model": "sol",
                    "minimum_reasoning_effort": "high",
                    "cwd": str(self.root),
                    "must_be_unarchived": True,
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
            },
            "skill": {
                "source": "skill-source",
                "installed": ".codex/skills/x",
            },
            "personality": {
                "tracked_policy": "personality/policy.json",
                "runtime_overrides": "var/personality-overrides.json",
            },
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

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
