from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts import autopilot_supervisor, system_doctor


class AutopilotSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "browser_owner_thread_id": "owner",
                    "autopilot_supervisor_state_file": "var/supervisor.json",
                    "autopilot_supervisor_repair_cooldown_seconds": 60,
                    "autopilot_supervisor_escalation_retry_seconds": 120,
                }
            ),
            encoding="utf-8",
        )
        self.contract = self.root / "contract.json"
        self.contract.write_text("{}", encoding="utf-8")
        self.now = datetime(2026, 7, 27, 18, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def check(
        identifier: str,
        status: str,
        *,
        details: dict | None = None,
    ) -> system_doctor.Check:
        return system_doctor.Check(
            identifier,
            status,
            f"{identifier} {status}",
            f"repair {identifier}",
            details,
        )

    def test_healthy_run_is_silent_and_does_not_call_repair(self) -> None:
        result = autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now,
            checks=[self.check("runtime.poll_health", "pass")],
            repair_runner=mock.Mock(
                side_effect=AssertionError("repair must not run")
            ),
        )

        self.assertTrue(result["healthy"])
        self.assertFalse(result["model_wake_required"])
        self.assertFalse(autopilot_supervisor.gate(self.config)["dispatch"])

    def test_stale_poll_is_kicked_once_then_escalated(self) -> None:
        stale = self.check(
            "runtime.poll_health",
            "fail",
            details={
                "health_status": "healthy",
                "age_seconds": 181,
                "max_age_seconds": 180,
            },
        )
        repair = mock.Mock(
            return_value={
                "action": "kickstart_poll_launchagent",
                "success": True,
                "returncode": 0,
            }
        )
        first = autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now,
            checks=[stale],
            repair_runner=repair,
        )
        incident_id = first["incident_id"]

        second = autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now + timedelta(seconds=30),
            checks=[stale],
            repair_runner=repair,
        )
        third = autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now + timedelta(seconds=61),
            checks=[stale],
            repair_runner=repair,
        )

        self.assertEqual(first["status"], "repair_observing")
        self.assertEqual(second["incident_id"], incident_id)
        self.assertEqual(third["incident_id"], incident_id)
        self.assertEqual(third["status"], "escalation_pending")
        self.assertEqual(repair.call_count, 1)
        self.assertTrue(autopilot_supervisor.gate(self.config)["dispatch"])

    def test_nonrepairable_failure_claims_once_and_closes_with_report(
        self,
    ) -> None:
        database_failure = self.check("runtime.database", "fail")
        detected = autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now,
            checks=[database_failure],
        )
        with mock.patch.object(
            autopilot_supervisor.autopilot_bridge,
            "browser_owner_activity",
            return_value={"defer": False, "status": "owner_thread_idle"},
        ):
            reserved = autopilot_supervisor.reserve_handoff(
                self.config,
                lease_seconds=1800,
                now=self.now,
            )
            duplicate = autopilot_supervisor.reserve_handoff(
                self.config,
                lease_seconds=1800,
                now=self.now + timedelta(seconds=1),
            )

        claimed = autopilot_supervisor.claim(
            self.config,
            lease_seconds=1800,
            now=self.now + timedelta(seconds=2),
        )
        started = autopilot_supervisor.started(
            self.config,
            claim_token=claimed["claim_token"],
            now=self.now + timedelta(seconds=3),
        )
        completed = autopilot_supervisor.completed(
            self.config,
            self.contract,
            claim_token=claimed["claim_token"],
            report="SQLite восстановлена и проверена.",
            now=self.now + timedelta(seconds=4),
            checks=[self.check("runtime.database", "pass")],
        )

        self.assertEqual(detected["status"], "escalation_pending")
        self.assertEqual(reserved["incident_id"], detected["incident_id"])
        self.assertEqual(duplicate["status"], "repair_handoff_reserved")
        self.assertEqual(started["status"], "work_in_progress")
        self.assertEqual(completed["status"], "completed")
        self.assertFalse(autopilot_supervisor.gate(self.config)["dispatch"])

    def test_relay_falls_through_to_x_when_repair_is_idle(self) -> None:
        x_reservation = {
            "status": "handoff_reserved_ready",
            "dispatch": True,
            "event_ids": ["synthetic-event"],
            "reservation_token": "synthetic-reservation",
        }
        with mock.patch.object(
            autopilot_supervisor.autopilot_bridge,
            "reserve_handoff",
            return_value=x_reservation,
        ) as reserve_x:
            result = autopilot_supervisor.relay_reserve_handoff(
                self.config,
                lease_seconds=1800,
                now=self.now,
            )

        reserve_x.assert_called_once_with(
            self.config,
            lease_seconds=1800,
        )
        self.assertEqual(result["route"], "x")
        self.assertTrue(result["dispatch"])
        self.assertFalse(result["repair_pending"])
        self.assertEqual(result["event_ids"], ["synthetic-event"])

    def test_relay_does_not_check_x_while_repair_is_pending(self) -> None:
        database_failure = self.check("runtime.database", "fail")
        autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now,
            checks=[database_failure],
        )
        with (
            mock.patch.object(
                autopilot_supervisor.autopilot_bridge,
                "browser_owner_activity",
                return_value={"defer": False, "status": "owner_thread_idle"},
            ),
            mock.patch.object(
                autopilot_supervisor.autopilot_bridge,
                "reserve_handoff",
                side_effect=AssertionError(
                    "X must wait while repair is pending"
                ),
            ),
        ):
            result = autopilot_supervisor.relay_reserve_handoff(
                self.config,
                lease_seconds=1800,
                now=self.now,
            )

        self.assertEqual(result["route"], "repair")
        self.assertTrue(result["dispatch"])
        self.assertTrue(result["repair_pending"])

    def test_changed_failures_do_not_replace_active_owner(self) -> None:
        first_failure = self.check("runtime.database", "fail")
        second_failure = self.check(
            "runtime.poll_health",
            "fail",
            details={
                "health_status": "stale",
                "age_seconds": 181,
                "max_age_seconds": 180,
            },
        )
        autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now,
            checks=[first_failure],
        )
        claimed = autopilot_supervisor.claim(
            self.config,
            lease_seconds=1800,
            now=self.now + timedelta(seconds=1),
        )
        autopilot_supervisor.started(
            self.config,
            claim_token=claimed["claim_token"],
            now=self.now + timedelta(seconds=2),
        )

        observed = autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now + timedelta(seconds=3),
            checks=[second_failure],
            repair_runner=mock.Mock(
                side_effect=AssertionError(
                    "active owner must not be replaced by automatic repair"
                )
            ),
        )
        path = autopilot_supervisor.state_path(self.config)
        state = autopilot_supervisor.load_state(path)

        self.assertEqual(observed["status"], "work_in_progress")
        self.assertEqual(
            state["incident"]["owner"]["claim_token"],
            claimed["claim_token"],
        )
        self.assertEqual(
            state["incident"]["checks"][0]["identifier"],
            "runtime.poll_health",
        )

    def test_expired_owner_is_requeued_for_one_new_claim(self) -> None:
        failure = self.check("runtime.database", "fail")
        autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now,
            checks=[failure],
        )
        first_claim = autopilot_supervisor.claim(
            self.config,
            lease_seconds=30,
            now=self.now + timedelta(seconds=1),
        )

        recovered = autopilot_supervisor.gate(
            self.config,
            now=self.now + timedelta(seconds=32),
        )
        second_claim = autopilot_supervisor.claim(
            self.config,
            lease_seconds=30,
            now=self.now + timedelta(seconds=33),
        )

        self.assertTrue(recovered["dispatch"])
        self.assertEqual(recovered["status"], "escalation_pending")
        self.assertNotEqual(
            first_claim["claim_token"],
            second_claim["claim_token"],
        )

    def test_expired_handoff_is_requeued_by_gate(self) -> None:
        failure = self.check("runtime.database", "fail")
        autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now,
            checks=[failure],
        )
        with mock.patch.object(
            autopilot_supervisor.autopilot_bridge,
            "browser_owner_activity",
            return_value={"defer": False, "status": "owner_thread_idle"},
        ):
            autopilot_supervisor.reserve_handoff(
                self.config,
                lease_seconds=30,
                now=self.now + timedelta(seconds=1),
            )

        recovered = autopilot_supervisor.gate(
            self.config,
            now=self.now + timedelta(seconds=32),
        )

        self.assertTrue(recovered["dispatch"])
        self.assertEqual(recovered["status"], "escalation_pending")

    def test_completion_requires_started_state(self) -> None:
        failure = self.check("runtime.database", "fail")
        autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now,
            checks=[failure],
        )
        claimed = autopilot_supervisor.claim(
            self.config,
            lease_seconds=1800,
            now=self.now + timedelta(seconds=1),
        )

        with self.assertRaisesRegex(ValueError, "has not been started"):
            autopilot_supervisor.completed(
                self.config,
                self.contract,
                claim_token=claimed["claim_token"],
                report="Нельзя завершить до started.",
                now=self.now + timedelta(seconds=2),
                checks=[self.check("runtime.database", "pass")],
            )

    def test_canary_requires_real_claim_and_accepts_closure_report(self) -> None:
        created = autopilot_supervisor.create_canary(
            self.config,
            now=self.now,
        )
        claimed = autopilot_supervisor.claim(
            self.config,
            lease_seconds=1800,
            now=self.now + timedelta(seconds=1),
        )
        autopilot_supervisor.started(
            self.config,
            claim_token=claimed["claim_token"],
            now=self.now + timedelta(seconds=2),
        )
        completed = autopilot_supervisor.completed(
            self.config,
            self.contract,
            claim_token=claimed["claim_token"],
            report="Canary handoff подтвержден.",
            now=self.now + timedelta(seconds=3),
            checks=[self.check("canary.supervisor_handoff", "fail")],
        )

        self.assertTrue(created["canary"])
        self.assertEqual(created["incident_id"], completed["incident_id"])
        self.assertFalse(autopilot_supervisor.gate(self.config)["dispatch"])


if __name__ == "__main__":
    unittest.main()
