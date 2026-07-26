from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import autopilot_dispatch
from scripts import session_janitor


class SessionJanitorTests(unittest.TestCase):
    def test_owner_age_is_measured_from_claim(self) -> None:
        age = session_janitor.owner_age_seconds(
            {"claimed_at": "2026-07-26T09:00:00Z"},
            now=datetime(2026, 7, 26, 9, 5, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(age, 300.0)

    def test_active_automation_match_requires_exact_preview(self) -> None:
        exact = {
            "name": session_janitor.AUTOMATION_TITLE,
            "preview": (
                "Automation: X: автопилот ответов\n"
                "Automation ID: x\n"
            ),
            "status": {"type": "active"},
        }
        manual = {**exact, "preview": "ручная задача"}

        self.assertTrue(session_janitor.matches_automation(exact))
        self.assertFalse(session_janitor.matches_automation(manual))
        self.assertEqual(session_janitor.status_type(exact), "active")

    def test_owner_maps_to_latest_task_created_before_claim(self) -> None:
        base = {
            "name": session_janitor.AUTOMATION_TITLE,
            "preview": (
                "Automation: X: автопилот ответов\n"
                "Automation ID: x\n"
            ),
            "status": {"type": "notLoaded"},
        }
        threads = [
            {
                **base,
                "id": "owner-task",
                "createdAt": 100,
                "updatedAt": 390,
            },
            {
                **base,
                "id": "later-empty-run",
                "createdAt": 400,
                "updatedAt": 410,
            },
            {
                **base,
                "id": "older-run",
                "createdAt": 50,
                "updatedAt": 120,
            },
        ]

        owning = session_janitor.owning_automation_thread(
            threads,
            {"claimed_at": "1970-01-01T00:03:20Z"},
        )

        self.assertIsNotNone(owning)
        self.assertEqual(owning["id"], "owner-task")
        self.assertTrue(
            session_janitor.owning_thread_is_live(
                owning,
                now_epoch=500,
                freshness_seconds=300,
            )
        )
        self.assertFalse(
            session_janitor.owning_thread_is_live(
                owning,
                now_epoch=700,
                freshness_seconds=300,
            )
        )

    def test_dry_run_never_releases_owner(self) -> None:
        with TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            autopilot_dispatch.atomic_write_json(
                state_path,
                {
                    "version": 1,
                    "owner": {
                        "claim_token": "claim",
                        "claimed_at": "2026-07-26T08:00:00Z",
                        "event_ids": ["123"],
                    },
                    "events": {
                        "123": {
                            "claim_token": "claim",
                            "last_dispatched_at": "2026-07-26T08:00:00Z",
                        }
                    },
                },
            )
            with mock.patch(
                "scripts.session_janitor.autopilot_dispatch.release"
            ) as release:
                candidate, recovered = session_janitor.recover_owner(
                    state_path,
                    autopilot_dispatch.load_state(state_path)["owner"],
                    owner_age=3600,
                    apply=False,
                )

            self.assertIsNotNone(
                autopilot_dispatch.load_state(state_path)["owner"]
            )
            self.assertEqual(candidate["event_ids"], ["123"])
            self.assertIsNone(recovered)
            release.assert_not_called()

    def test_helper_reaper_matches_only_inactive_task_start_bundle(self) -> None:
        prefix = {
            "name": session_janitor.AUTOMATION_TITLE,
            "preview": (
                "Automation: X: автопилот ответов\n"
                "Automation ID: x\n"
            ),
        }
        threads = [
            {
                **prefix,
                "id": "done",
                "createdAt": 100,
                "updatedAt": 120,
                "status": {"type": "notLoaded"},
            },
            {
                **prefix,
                "id": "owner",
                "createdAt": 200,
                "updatedAt": 300,
                "status": {"type": "notLoaded"},
            },
        ]
        processes = [
            {
                "pid": 10,
                "ppid": 1,
                "started_at": 100,
                "command": "/path/cua_node/bin/node_repl",
            },
            {
                "pid": 11,
                "ppid": 1,
                "started_at": 101,
                "command": "node ./mcp/server.mjs",
            },
            {
                "pid": 12,
                "ppid": 11,
                "started_at": 101,
                "command": "child",
            },
            {
                "pid": 20,
                "ppid": 1,
                "started_at": 200,
                "command": "/path/cua_node/bin/node_repl",
            },
            {
                "pid": 30,
                "ppid": 1,
                "started_at": 100,
                "command": "unrelated",
            },
        ]

        candidates = session_janitor.helper_process_candidates(
            processes,
            threads,
            now_epoch=500,
            grace_seconds=120,
            protected_ids={"owner"},
        )

        self.assertEqual(candidates, [10, 11, 12])

    def test_process_parser_reads_mac_lstart(self) -> None:
        parsed = session_janitor.parse_processes(
            "  10  1 Sun Jul 26 05:40:20 2026 "
            "node ./mcp/server.mjs\n"
        )

        self.assertEqual(parsed[0]["pid"], 10)
        self.assertEqual(parsed[0]["ppid"], 1)
        self.assertEqual(parsed[0]["command"], "node ./mcp/server.mjs")

    def test_only_old_completed_automation_threads_are_eligible(self) -> None:
        threads = [
            {
                "id": "old-idle",
                "name": session_janitor.AUTOMATION_TITLE,
                "threadSource": "automation",
                "preview": (
                    "Automation: X: автопилот ответов\n"
                    "Automation ID: x\n"
                ),
                "status": {"type": "idle"},
                "updatedAt": 100,
            },
            {
                "id": "active",
                "name": session_janitor.AUTOMATION_TITLE,
                "threadSource": "automation",
                "preview": (
                    "Automation: X: автопилот ответов\n"
                    "Automation ID: x\n"
                ),
                "status": {"type": "active", "activeFlags": []},
                "updatedAt": 100,
            },
            {
                "id": "pinned-manual",
                "name": session_janitor.AUTOMATION_TITLE,
                "threadSource": None,
                "preview": (
                    "Automation: X: автопилот ответов\n"
                    "Automation ID: x\n"
                ),
                "status": {"type": "notLoaded"},
                "updatedAt": 100,
            },
            {
                "id": "too-new",
                "name": session_janitor.AUTOMATION_TITLE,
                "threadSource": "automation",
                "preview": (
                    "Automation: X: автопилот ответов\n"
                    "Automation ID: x\n"
                ),
                "status": {"type": "idle"},
                "updatedAt": 950,
            },
        ]

        eligible = session_janitor.eligible_threads(
            threads,
            now=1000,
            minimum_age_seconds=300,
            protected_ids={"pinned-manual"},
        )

        self.assertEqual([thread["id"] for thread in eligible], ["old-idle"])


if __name__ == "__main__":
    unittest.main()
