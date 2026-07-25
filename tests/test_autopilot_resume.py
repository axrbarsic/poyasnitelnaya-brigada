from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import autopilot_resume


class AutopilotResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.wake_file = self.root / "var" / "wake-request.json"
        self.state_file = self.root / "var" / "autopilot-dispatch.json"
        self.health_file = self.root / "var" / "autopilot-health.json"
        self.codex = self.root / "bin" / "codex"
        self.codex.parent.mkdir(parents=True)
        self.codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.codex.chmod(0o755)
        self.owner_cwd = self.root / "owner"
        self.owner_cwd.mkdir()
        self.config_file = self.root / "config.json"
        self.config_file.write_text(
            json.dumps(
                {
                    "wake_file": "var/wake-request.json",
                    "autopilot_state_file": "var/autopilot-dispatch.json",
                    "autopilot_health_file": "var/autopilot-health.json",
                    "autopilot_last_message_file": "var/last-message.txt",
                    "autopilot_process_lock": "var/autopilot-resume.lock",
                    "autopilot_owner_rotation_after_runs": 2,
                    "browser_owner_thread_id":
                        "019f95c3-4e21-72e0-8d47-4ed1277bd0c4",
                    "browser_owner_cwd": str(self.owner_cwd),
                    "codex_cli_path": str(self.codex),
                    "notifications_enabled": False,
                }
            ),
            encoding="utf-8",
        )
        self.write_events([])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_events(self, events: list[dict]) -> None:
        self.wake_file.parent.mkdir(parents=True, exist_ok=True)
        self.wake_file.write_text(
            json.dumps(
                {
                    "pending_count": len(events),
                    "events": events,
                }
            ),
            encoding="utf-8",
        )

    def event(self) -> dict:
        return {
            "event_id": "2081031642001404189",
            "event_url":
                "https://x.com/example/status/2081031642001404189",
            "username": "example",
            "conversation_id": "2080651494051737864",
            "created_at": "2026-07-25T14:59:41Z",
            "first_seen_at": "2026-07-25T15:03:27Z",
            "is_reply": True,
        }

    def test_empty_queue_does_not_start_codex(self) -> None:
        calls: list[tuple] = []

        def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args[0], 0, "", "")

        result = autopilot_resume.run_once(
            self.config_file,
            lease_seconds=1800,
            runner=runner,
        )

        self.assertEqual(result["status"], "idle")
        self.assertEqual(calls, [])
        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "idle")

    def test_pending_event_resumes_exact_owner_with_sol_high(self) -> None:
        self.write_events([self.event()])
        captured: dict = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            self.write_events([])
            return subprocess.CompletedProcess(command, 0, "", "")

        result = autopilot_resume.run_once(
            self.config_file,
            lease_seconds=1800,
            runner=runner,
        )

        self.assertEqual(result["status"], "completed")
        command = captured["command"]
        self.assertEqual(command[:4], [
            str(self.codex),
            "exec",
            "resume",
            "--ephemeral",
        ])
        self.assertIn(
            "019f95c3-4e21-72e0-8d47-4ed1277bd0c4",
            command,
        )
        self.assertIn("gpt-5.6-sol", command)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertIn(self.event()["event_id"], captured["input"])
        self.assertIn(
            f"WATCHER_CONFIG={self.config_file.resolve()}",
            captured["input"],
        )
        self.assertEqual(captured["cwd"], self.owner_cwd)
        self.assertNotIn("\u2013", captured["input"])
        self.assertNotIn("\u2014", captured["input"])
        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["completed_runs"], 1)
        self.assertFalse(health["rotation_recommended"])
        self.assertEqual(result["unresolved_event_ids"], [])

    def test_completed_run_count_survives_idle_checks(self) -> None:
        self.write_events([self.event()])

        def runner(command, **kwargs):
            self.write_events([])
            return subprocess.CompletedProcess(command, 0, "", "")

        autopilot_resume.run_once(
            self.config_file,
            lease_seconds=1800,
            runner=runner,
        )
        self.write_events([])
        autopilot_resume.run_once(
            self.config_file,
            lease_seconds=1800,
            runner=runner,
        )

        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "idle")
        self.assertEqual(health["completed_runs"], 1)
        self.assertFalse(health["rotation_recommended"])

    def test_successful_owner_exit_with_pending_event_is_visible(self) -> None:
        self.write_events([self.event()])

        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "", "")

        result = autopilot_resume.run_once(
            self.config_file,
            lease_seconds=1800,
            runner=runner,
        )

        self.assertEqual(result["status"], "completed_unresolved")
        self.assertEqual(
            result["unresolved_event_ids"],
            [self.event()["event_id"]],
        )
        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "completed_unresolved")

    def test_failed_resume_releases_claim(self) -> None:
        self.write_events([self.event()])

        def failing_runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 7, "", "failed")

        first = autopilot_resume.run_once(
            self.config_file,
            lease_seconds=1800,
            runner=failing_runner,
        )
        second = autopilot_resume.run_once(
            self.config_file,
            lease_seconds=1800,
            runner=failing_runner,
        )

        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "failed")
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        record = state["events"][self.event()["event_id"]]
        self.assertNotIn("last_dispatched_at", record)
        self.assertNotIn("claim_token", record)

    def test_invalid_owner_config_fails_before_claim(self) -> None:
        config = json.loads(self.config_file.read_text(encoding="utf-8"))
        config["browser_owner_thread_id"] = "not-a-uuid"
        self.config_file.write_text(json.dumps(config), encoding="utf-8")
        self.write_events([self.event()])

        with self.assertRaisesRegex(ValueError, "must be a UUID"):
            autopilot_resume.run_once(
                self.config_file,
                lease_seconds=1800,
            )

        self.assertFalse(self.state_file.exists())


if __name__ == "__main__":
    unittest.main()
