from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import browser_owner_rotation


class BrowserOwnerRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.codex_home = self.root / "codex-home"
        self.automation_dir = self.codex_home / "automations" / "x-relay"
        self.automation_dir.mkdir(parents=True)
        (self.codex_home / "archived_sessions").mkdir(parents=True)
        self.codex_state = self.codex_home / "state_1.sqlite"
        with closing(sqlite3.connect(self.codex_state)) as connection:
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
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "old-owner",
                    0,
                    0,
                    "X: Browser owner",
                    "gpt-5.6-sol",
                    "max",
                    str(self.root),
                ),
            )
            connection.commit()
        self.prompt = self.root / "relay.prompt.txt"
        self.prompt.write_text("relay prompt\n", encoding="utf-8")
        self.init_prompt = self.root / "owner-init.prompt.txt"
        self.init_prompt.write_text("owner init\n", encoding="utf-8")
        self.config = self.root / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "browser_owner_thread_id": "old-owner",
                    "codex_project_id": "project",
                    "codex_home": str(self.codex_home),
                    "autopilot_health_file": "health.json",
                    "autopilot_owner_rotation_state_file": "rotation.json",
                    "autopilot_owner_rotation_after_runs": 2,
                }
            ),
            encoding="utf-8",
        )
        self.contract = {
            "canonical_root": str(self.root),
            "threads": {
                "browser_owner": {
                    "id_source": "config.browser_owner_thread_id",
                    "title": "X: Browser owner",
                    "model": "gpt-5.6-sol",
                    "minimum_reasoning_effort": "max",
                    "cwd": str(self.root),
                    "initial_prompt_source": self.init_prompt.name,
                }
            },
            "automations": {
                "active_relay": {
                    "id": "x-relay",
                    "kind": "heartbeat",
                    "name": "X: единый owner heartbeat",
                    "status": "ACTIVE",
                    "rrule": "FREQ=MINUTELY;INTERVAL=1",
                    "notification_policy": "failed_runs_only",
                    "target_thread_role": "browser_owner",
                    "prompt_source": self.prompt.name,
                }
            },
        }
        (self.root / "contract.json").write_text(
            json.dumps(self.contract),
            encoding="utf-8",
        )
        self.now = datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc)
        self.write_health(2)
        self.write_automation("old-owner")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_health(self, count: int) -> None:
        (self.root / "health.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "status": "completed",
                    "handoff_count": count,
                    "completed_runs": count,
                    "rotation_after_runs": 2,
                    "rotation_recommended": count >= 2,
                    "event_ids": [],
                }
            ),
            encoding="utf-8",
        )

    def write_automation(self, target: str) -> None:
        (self.automation_dir / "automation.toml").write_text(
            "\n".join(
                [
                    'id = "x-relay"',
                    'kind = "heartbeat"',
                    'name = "X: единый owner heartbeat"',
                    'status = "ACTIVE"',
                    'rrule = "FREQ=MINUTELY;INTERVAL=1"',
                    'notification_policy = "failed_runs_only"',
                    f'target_thread_id = "{target}"',
                    'prompt = "relay prompt"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def write_thread(
        self,
        thread_id: str,
        *,
        archived: int = 0,
        title: str = "X: Browser owner",
        model: str = "gpt-5.6-sol",
        reasoning_effort: str = "max",
        cwd: str | None = None,
    ) -> None:
        with closing(sqlite3.connect(self.codex_state)) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    archived,
                    0,
                    title,
                    model,
                    reasoning_effort,
                    cwd or str(self.root),
                ),
            )
            connection.commit()

    def reserve(self) -> dict:
        return browser_owner_rotation.reserve(
            self.config,
            self.contract,
            allow_new=True,
            owner_busy=False,
            outbound_busy=False,
            now=self.now,
        )

    def test_not_due_does_not_create_transaction(self) -> None:
        self.write_health(1)

        result = self.reserve()

        self.assertFalse(result["dispatch"])
        self.assertEqual(result["status"], "owner_rotation_not_due")
        self.assertIsNone(
            browser_owner_rotation.load_state(self.root / "rotation.json")[
                "transaction"
            ]
        )

    def test_busy_owner_defers_without_creating_transaction(self) -> None:
        result = browser_owner_rotation.reserve(
            self.config,
            self.contract,
            allow_new=True,
            owner_busy=True,
            outbound_busy=False,
            now=self.now,
        )

        self.assertFalse(result["dispatch"])
        self.assertEqual(result["status"], "owner_rotation_waiting")
        self.assertIsNone(
            browser_owner_rotation.load_state(self.root / "rotation.json")[
                "transaction"
            ]
        )

    @mock.patch("scripts.browser_owner_rotation.uuid.uuid4", return_value="token")
    def test_reserve_is_idempotent_until_new_thread_is_recorded(
        self, _uuid: mock.Mock
    ) -> None:
        first = self.reserve()
        second = self.reserve()

        self.assertTrue(first["dispatch"])
        self.assertEqual(first["action"], "create_thread")
        self.assertEqual(first["rotation_token"], "token")
        self.assertEqual(second["rotation_token"], "token")
        self.assertEqual(second["action"], "create_thread")
        self.assertEqual(first["owner_rotation_marker"], "X_OWNER_ROTATION_TOKEN=token")
        self.assertTrue(first["initial_prompt"].startswith(
            "X_OWNER_ROTATION_TOKEN=token\n\n"
        ))

    @mock.patch("scripts.browser_owner_rotation.uuid.uuid4", return_value="token")
    def test_created_thread_is_reused_after_interruption(
        self, _uuid: mock.Mock
    ) -> None:
        self.reserve()
        browser_owner_rotation.record_created(
            self.config,
            rotation_token="token",
            thread_id="new-owner",
            now=self.now,
        )

        pending = browser_owner_rotation.pending(self.config, self.contract)

        self.assertIsNotNone(pending)
        self.assertEqual(pending["action"], "update_automation")
        self.assertEqual(pending["new_thread_id"], "new-owner")

    @mock.patch("scripts.browser_owner_rotation.uuid.uuid4", return_value="token")
    def test_commit_recovers_after_automation_switch_and_resets_counter(
        self, _uuid: mock.Mock
    ) -> None:
        self.reserve()
        browser_owner_rotation.record_created(
            self.config,
            rotation_token="token",
            thread_id="new-owner",
            now=self.now,
        )
        self.write_thread("new-owner")
        self.write_automation("new-owner")

        pending = browser_owner_rotation.pending(self.config, self.contract)
        committed = browser_owner_rotation.commit(
            self.config,
            self.root / "contract.json",
            rotation_token="token",
            now=self.now,
        )

        self.assertEqual(pending["action"], "commit")
        self.assertEqual(committed["status"], "owner_rotation_committed")
        config = json.loads(self.config.read_text(encoding="utf-8"))
        health = json.loads((self.root / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(config["browser_owner_thread_id"], "new-owner")
        self.assertEqual(health["handoff_count"], 0)
        self.assertFalse(health["rotation_recommended"])

    @mock.patch("scripts.browser_owner_rotation.uuid.uuid4", return_value="token")
    def test_finish_requires_archive_then_closes_transaction(
        self, _uuid: mock.Mock
    ) -> None:
        self.reserve()
        browser_owner_rotation.record_created(
            self.config,
            rotation_token="token",
            thread_id="new-owner",
            now=self.now,
        )
        self.write_thread("new-owner")
        self.write_automation("new-owner")
        browser_owner_rotation.commit(
            self.config,
            self.root / "contract.json",
            rotation_token="token",
            now=self.now,
        )

        preflight = browser_owner_rotation.archive_preflight(
            self.config,
            self.root / "contract.json",
            rotation_token="token",
        )
        self.assertEqual(preflight["status"], "owner_rotation_archive_ready")
        self.assertEqual(preflight["old_thread_id"], "old-owner")
        self.assertEqual(preflight["new_thread_id"], "new-owner")

        with self.assertRaisesRegex(ValueError, "not archived"):
            browser_owner_rotation.finish(
                self.config,
                self.root / "contract.json",
                rotation_token="token",
                now=self.now,
            )
        self.write_thread("old-owner", archived=1)
        finished = browser_owner_rotation.finish(
            self.config,
            self.root / "contract.json",
            rotation_token="token",
            now=self.now,
        )

        self.assertEqual(finished["status"], "owner_rotation_completed")
        state = browser_owner_rotation.load_state(self.root / "rotation.json")
        self.assertIsNone(state["transaction"])
        self.assertEqual(state["generation"], 1)

    @mock.patch("scripts.browser_owner_rotation.uuid.uuid4", return_value="token")
    def test_archive_preflight_rejects_uncommitted_rotation(
        self, _uuid: mock.Mock
    ) -> None:
        self.reserve()

        with self.assertRaisesRegex(ValueError, "committed"):
            browser_owner_rotation.archive_preflight(
                self.config,
                self.root / "contract.json",
                rotation_token="token",
            )

    @mock.patch("scripts.browser_owner_rotation.uuid.uuid4", return_value="token")
    def test_failure_before_recording_preserves_recovery_marker(
        self, _uuid: mock.Mock
    ) -> None:
        self.reserve()

        failed = browser_owner_rotation.fail(
            self.config,
            rotation_token="token",
            error="create failed",
            now=self.now,
        )

        self.assertEqual(failed["phase"], "prepared")
        pending = browser_owner_rotation.pending(self.config, self.contract)
        self.assertIsNotNone(pending)
        self.assertEqual(
            pending["owner_rotation_marker"],
            "X_OWNER_ROTATION_TOKEN=token",
        )

    @mock.patch("scripts.browser_owner_rotation.uuid.uuid4", return_value="token")
    def test_failure_after_creation_preserves_exact_replacement(
        self, _uuid: mock.Mock
    ) -> None:
        self.reserve()
        browser_owner_rotation.record_created(
            self.config,
            rotation_token="token",
            thread_id="new-owner",
            now=self.now,
        )

        browser_owner_rotation.fail(
            self.config,
            rotation_token="token",
            error="automation failed",
            now=self.now,
        )
        pending = browser_owner_rotation.pending(self.config, self.contract)

        self.assertIsNotNone(pending)
        self.assertEqual(pending["new_thread_id"], "new-owner")
        self.assertEqual(pending["action"], "update_automation")

    def test_commit_rejects_wrong_automation_target(self) -> None:
        with mock.patch(
            "scripts.browser_owner_rotation.uuid.uuid4",
            return_value="token",
        ):
            self.reserve()
        browser_owner_rotation.record_created(
            self.config,
            rotation_token="token",
            thread_id="new-owner",
            now=self.now,
        )
        self.write_thread("new-owner")
        self.write_automation("unrelated")

        with self.assertRaisesRegex(ValueError, "new owner"):
            browser_owner_rotation.commit(
                self.config,
                self.root / "contract.json",
                rotation_token="token",
                now=self.now,
            )

    def test_commit_rejects_new_thread_with_wrong_runtime_settings(self) -> None:
        with mock.patch(
            "scripts.browser_owner_rotation.uuid.uuid4",
            return_value="token",
        ):
            self.reserve()
        browser_owner_rotation.record_created(
            self.config,
            rotation_token="token",
            thread_id="new-owner",
            now=self.now,
        )
        self.write_thread("new-owner", reasoning_effort="high")
        self.write_automation("new-owner")

        with self.assertRaisesRegex(ValueError, "reasoning_effort"):
            browser_owner_rotation.commit(
                self.config,
                self.root / "contract.json",
                rotation_token="token",
                now=self.now,
            )


if __name__ == "__main__":
    unittest.main()
