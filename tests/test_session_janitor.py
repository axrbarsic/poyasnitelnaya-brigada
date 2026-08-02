from __future__ import annotations

import fcntl
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import autopilot_dispatch
from scripts import session_janitor


class SessionJanitorTests(unittest.TestCase):
    def test_main_refuses_overlapping_run_before_app_server(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path = root / "janitor.lock"
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "session_janitor_lock_file": str(lock_path),
                        "session_janitor_orphan_owner_seconds": 300,
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with lock_path.open("a+", encoding="utf-8") as held_lock:
                fcntl.flock(
                    held_lock.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                with (
                    mock.patch.object(
                        sys,
                        "argv",
                        [
                            "session_janitor.py",
                            "--config",
                            str(config_path),
                        ],
                    ),
                    redirect_stdout(output),
                    mock.patch.object(
                        session_janitor,
                        "run_janitor",
                        side_effect=AssertionError(
                            "overlap must stop before app-server"
                        ),
                    ),
                ):
                    result = session_janitor.main()

            self.assertEqual(result, 0)
            self.assertEqual(
                json.loads(output.getvalue())["status"],
                "already_running",
            )

    def test_owner_age_is_measured_from_claim(self) -> None:
        age = session_janitor.owner_age_seconds(
            {"claimed_at": "2026-07-26T09:00:00Z"},
            now=datetime(2026, 7, 26, 9, 5, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(age, 300.0)

    def test_owner_lease_remaining_honors_renewed_expiry(self) -> None:
        remaining = session_janitor.owner_lease_remaining_seconds(
            {
                "claimed_at": "2026-07-26T09:00:00Z",
                "last_renewed_at": "2026-07-26T09:15:00Z",
                "lease_expires_at": "2026-07-26T09:45:00Z",
            },
            now=datetime(2026, 7, 26, 9, 20, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(remaining, 1500.0)

    def test_unexpired_lease_protects_missing_owner_thread(self) -> None:
        allowed = session_janitor.owner_recovery_allowed(
            {
                "claimed_at": "2026-07-26T09:00:00Z",
                "last_renewed_at": "2026-07-26T09:15:00Z",
                "lease_expires_at": "2026-07-26T09:45:00Z",
            },
            None,
            now=datetime(2026, 7, 26, 9, 20, 0, tzinfo=timezone.utc),
            orphan_owner_seconds=300,
        )

        self.assertFalse(allowed)

    def test_expired_lease_allows_stale_owner_recovery(self) -> None:
        allowed = session_janitor.owner_recovery_allowed(
            {
                "claimed_at": "2026-07-26T09:00:00Z",
                "last_renewed_at": "2026-07-26T09:15:00Z",
                "lease_expires_at": "2026-07-26T09:45:00Z",
            },
            None,
            now=datetime(2026, 7, 26, 9, 45, 1, tzinfo=timezone.utc),
            orphan_owner_seconds=300,
        )

        self.assertTrue(allowed)

    def test_live_owner_thread_blocks_recovery_after_lease_expiry(self) -> None:
        allowed = session_janitor.owner_recovery_allowed(
            {
                "claimed_at": "2026-07-26T09:00:00Z",
                "lease_expires_at": "2026-07-26T09:30:00Z",
            },
            {
                "id": "owner",
                "status": {"type": "active"},
                "updatedAt": 0,
            },
            now=datetime(2026, 7, 26, 9, 45, 1, tzinfo=timezone.utc),
            orphan_owner_seconds=300,
        )

        self.assertFalse(allowed)

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

    def test_x15_automation_match_requires_exact_id_and_title(self) -> None:
        title = dict(session_janitor.AUTOMATION_IDENTITIES)["x-15"]
        exact = {
            "name": title,
            "preview": (
                f"Automation: {title}\n"
                "Automation ID: x-15\n"
            ),
            "status": {"type": "idle"},
        }
        wrong_id = {
            **exact,
            "preview": (
                f"Automation: {title}\n"
                "Automation ID: unrelated\n"
            ),
        }
        wrong_title = {**exact, "name": "X: похожее имя"}

        self.assertTrue(session_janitor.matches_automation(exact))
        self.assertFalse(session_janitor.matches_automation(wrong_id))
        self.assertFalse(session_janitor.matches_automation(wrong_title))

    def test_active_outbound_owner_uses_x15_lease(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.json"
            state_path = root / "outbound-cycle.json"
            config_path.write_text(
                json.dumps(
                    {"outbound_cycle_state_file": state_path.name}
                ),
                encoding="utf-8",
            )
            owner = {
                "claim_token": "outbound-claim",
                "claimed_at": "2026-07-26T09:00:00Z",
                "expires_at": "2026-07-26T09:30:00Z",
                "status": "work_in_progress",
                "target_limit": 1,
            }
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "catchup_remaining": 0,
                        "adjustments": [],
                        "runs": [],
                        "owner": owner,
                    }
                ),
                encoding="utf-8",
            )

            active = session_janitor.active_outbound_owner(
                config_path,
                now=datetime(
                    2026, 7, 26, 9, 20, 0, tzinfo=timezone.utc
                ),
            )
            expired = session_janitor.active_outbound_owner(
                config_path,
                now=datetime(
                    2026, 7, 26, 9, 31, 0, tzinfo=timezone.utc
                ),
            )

        self.assertEqual(active, owner)
        self.assertIsNone(expired)

    def test_outbound_owner_maps_only_to_x15_task(self) -> None:
        title = dict(session_janitor.AUTOMATION_IDENTITIES)["x-15"]
        x15 = {
            "id": "x15-owner",
            "name": title,
            "preview": (
                f"Automation: {title}\n"
                "Automation ID: x-15\n"
            ),
            "createdAt": 100,
            "updatedAt": 200,
            "status": {"type": "notLoaded"},
        }
        inbound = {
            "id": "inbound-owner",
            "name": session_janitor.AUTOMATION_TITLE,
            "preview": (
                "Automation: X: автопилот ответов\n"
                "Automation ID: x\n"
            ),
            "createdAt": 120,
            "updatedAt": 200,
            "status": {"type": "notLoaded"},
        }

        owning = session_janitor.owning_automation_thread(
            [inbound, x15],
            {"claimed_at": "1970-01-01T00:02:30Z"},
            identities=(("x-15", title),),
        )

        self.assertIsNotNone(owning)
        self.assertEqual(owning["id"], "x15-owner")

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

    def test_live_owner_path_never_attempts_recovery(self) -> None:
        owner = {
            "claim_token": "claim",
            "claimed_at": "2020-01-01T00:00:00Z",
            "lease_expires_at": "2020-01-01T00:30:00Z",
            "event_ids": ["123"],
        }
        thread = {
            "id": "owner-task",
            "status": {"type": "active"},
            "updatedAt": 0,
        }
        helpers = session_janitor.HelperCleanup([10], [], [], None)
        archive = session_janitor.ArchiveResult(["old"], [])

        with mock.patch.object(session_janitor, "recover_owner") as recover:
            busy, candidate, recovered = (
                session_janitor._recover_or_report_busy_owner(
                    Path("unused.json"),
                    owner,
                    thread,
                    age_seconds=3600,
                    lease_remaining_seconds=-1,
                    orphan_owner_seconds=300,
                    apply=True,
                    helpers=helpers,
                    archive=archive,
                )
            )

        recover.assert_not_called()
        self.assertEqual(busy["status"], "owner_busy")
        self.assertEqual(busy["active_thread_ids"], ["owner-task"])
        self.assertIsNone(candidate)
        self.assertIsNone(recovered)

    def test_expired_owner_path_delegates_one_exact_recovery(self) -> None:
        owner = {
            "claim_token": "claim",
            "claimed_at": "2020-01-01T00:00:00Z",
            "lease_expires_at": "2020-01-01T00:30:00Z",
            "event_ids": ["123"],
        }
        state_path = Path("state.json")
        helpers = session_janitor.HelperCleanup([], [], [], None)
        archive = session_janitor.ArchiveResult([], [])
        expected_candidate = {"claim_token": "claim"}
        expected_recovered = {"claim_token": "claim", "event_ids": ["123"]}

        with mock.patch.object(
            session_janitor,
            "recover_owner",
            return_value=(expected_candidate, expected_recovered),
        ) as recover:
            busy, candidate, recovered = (
                session_janitor._recover_or_report_busy_owner(
                    state_path,
                    owner,
                    None,
                    age_seconds=3600,
                    lease_remaining_seconds=-1,
                    orphan_owner_seconds=300,
                    apply=True,
                    helpers=helpers,
                    archive=archive,
                )
            )

        recover.assert_called_once_with(
            state_path,
            owner,
            owner_age=3600,
            apply=True,
        )
        self.assertIsNone(busy)
        self.assertEqual(candidate, expected_candidate)
        self.assertEqual(recovered, expected_recovered)

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

    def test_x15_thread_and_helpers_are_archivable(self) -> None:
        title = dict(session_janitor.AUTOMATION_IDENTITIES)["x-15"]
        thread = {
            "id": "x15-done",
            "name": title,
            "threadSource": "automation",
            "preview": (
                f"Automation: {title}\n"
                "Automation ID: x-15\n"
            ),
            "createdAt": 100,
            "updatedAt": 120,
            "status": {"type": "notLoaded"},
        }
        active = {
            **thread,
            "id": "x15-active",
            "createdAt": 200,
            "updatedAt": 490,
            "status": {"type": "active"},
        }
        processes = [
            {
                "pid": 40,
                "ppid": 1,
                "started_at": 100,
                "command": "/path/cua_node/bin/node_repl",
            },
            {
                "pid": 41,
                "ppid": 40,
                "started_at": 100,
                "command": "child",
            },
            {
                "pid": 50,
                "ppid": 1,
                "started_at": 200,
                "command": "/path/cua_node/bin/node_repl",
            },
        ]

        eligible = session_janitor.eligible_threads(
            [thread, active],
            now=500,
            minimum_age_seconds=120,
        )
        helpers = session_janitor.helper_process_candidates(
            processes,
            [thread, active],
            now_epoch=500,
            grace_seconds=120,
            protected_ids=set(),
        )

        self.assertEqual([item["id"] for item in eligible], ["x15-done"])
        self.assertEqual(helpers, [40, 41])

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
