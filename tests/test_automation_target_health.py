from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import automation_target_health


class AutomationTargetHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.automations = self.home / ".codex" / "automations"
        self.archived = self.home / ".codex" / "archived_sessions"
        self.automations.mkdir(parents=True)
        self.archived.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_automation(
        self,
        identifier: str,
        *,
        status: str,
        thread_id: str,
    ) -> None:
        directory = self.automations / identifier
        directory.mkdir()
        (directory / "automation.toml").write_text(
            "\n".join(
                [
                    f'id = "{identifier}"',
                    'kind = "heartbeat"',
                    f'name = "{identifier} name"',
                    f'status = "{status}"',
                    f'target_thread_id = "{thread_id}"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def test_reports_only_active_heartbeat_with_archived_target(self) -> None:
        self.write_automation(
            "broken",
            status="ACTIVE",
            thread_id="archived-thread",
        )
        self.write_automation(
            "paused",
            status="PAUSED",
            thread_id="paused-thread",
        )
        self.write_automation(
            "healthy",
            status="ACTIVE",
            thread_id="live-thread",
        )
        (
            self.archived
            / "rollout-2026-07-29T00-00-00-archived-thread.jsonl"
        ).write_text("{}\n", encoding="utf-8")
        (
            self.archived
            / "rollout-2026-07-29T00-00-00-paused-thread.jsonl"
        ).write_text("{}\n", encoding="utf-8")

        result = (
            automation_target_health.archived_active_heartbeat_targets(
                self.home
            )
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["automation_id"], "broken")
        self.assertEqual(result[0]["thread_id"], "archived-thread")

    def test_prompt_contents_cannot_override_metadata(self) -> None:
        directory = self.automations / "safe"
        directory.mkdir()
        prompt = 'kind = "cron"\nstatus = "PAUSED"\ntarget_thread_id = "fake"'
        (directory / "automation.toml").write_text(
            "\n".join(
                [
                    'id = "safe"',
                    'kind = "heartbeat"',
                    'name = "Проверка"',
                    f"prompt = {json.dumps(prompt, ensure_ascii=False)}",
                    'status = "ACTIVE"',
                    'target_thread_id = "archived-thread"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (
            self.archived
            / "rollout-2026-07-29T00-00-00-archived-thread.jsonl"
        ).write_text("{}\n", encoding="utf-8")

        payload = automation_target_health.parse_automation(
            directory / "automation.toml"
        )
        result = automation_target_health.archived_active_heartbeat_targets(
            self.home
        )

        self.assertEqual(payload["kind"], "heartbeat")
        self.assertEqual(payload["status"], "ACTIVE")
        self.assertEqual(payload["target_thread_id"], "archived-thread")
        self.assertEqual(result[0]["automation_id"], "safe")


if __name__ == "__main__":
    unittest.main()
