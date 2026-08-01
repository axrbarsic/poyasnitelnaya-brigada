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
        self.contract.write_text(
            json.dumps(
                {
                    "threads": {
                        "browser_owner": {
                            "id": "owner",
                            "model": "gpt-5.6-sol",
                            "minimum_reasoning_effort": "max",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
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

    def test_billing_block_suspends_poll_without_model_wake(self) -> None:
        blocked = self.check(
            "runtime.poll_health",
            "fail",
            details={
                "health_status": "degraded",
                "age_seconds": 181,
                "max_age_seconds": 180,
                "last_error_message": "X API HTTP 402 (Payment Required)",
            },
        )
        suspend = mock.Mock(
            return_value={
                "action": "suspend_billing_blocked_poll",
                "success": True,
                "returncode": 0,
                "already_unloaded": False,
            }
        )
        generic_repair = mock.Mock(
            side_effect=AssertionError("billing block must not be kicked")
        )

        first = autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now,
            checks=[blocked],
            repair_runner=generic_repair,
            billing_block_runner=suspend,
        )
        second = autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now + timedelta(seconds=60),
            checks=[blocked],
            repair_runner=generic_repair,
            billing_block_runner=suspend,
        )
        gate = autopilot_supervisor.gate(
            self.config,
            now=self.now + timedelta(seconds=61),
        )

        self.assertEqual(first["status"], "external_action_required")
        self.assertEqual(second["status"], "external_action_required")
        self.assertFalse(first["model_wake_required"])
        self.assertTrue(first["external_action_required"])
        self.assertFalse(gate["dispatch"])
        self.assertFalse(gate["repair_pending"])
        self.assertTrue(gate["external_action_required"])
        suspend.assert_called_once_with()
        generic_repair.assert_not_called()

    def test_billing_block_does_not_starve_existing_x_queue(self) -> None:
        blocked = self.check(
            "runtime.poll_health",
            "fail",
            details={
                "health_status": "billing_blocked",
                "last_error_message": "X API HTTP 402 (Payment Required)",
            },
        )
        autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now,
            checks=[blocked],
            billing_block_runner=mock.Mock(
                return_value={
                    "action": "suspend_billing_blocked_poll",
                    "success": True,
                    "returncode": 0,
                }
            ),
        )
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
                self.contract,
                lease_seconds=1800,
                now=self.now + timedelta(seconds=1),
            )

        reserve_x.assert_called_once_with(
            self.config,
            lease_seconds=1800,
        )
        self.assertEqual(result["route"], "x")
        self.assertTrue(result["dispatch"])
        self.assertEqual(result["owner_thread_id"], "owner")
        self.assertEqual(result["owner_model"], "gpt-5.6-sol")
        self.assertEqual(result["owner_thinking"], "max")

    def test_billing_incident_clears_after_poll_recovers(self) -> None:
        blocked = self.check(
            "runtime.poll_health",
            "fail",
            details={
                "last_error_message": "X API HTTP 402 (Payment Required)",
            },
        )
        autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now,
            checks=[blocked],
            billing_block_runner=mock.Mock(
                return_value={
                    "action": "suspend_billing_blocked_poll",
                    "success": True,
                    "returncode": 0,
                }
            ),
        )

        recovered = autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now + timedelta(seconds=60),
            checks=[self.check("runtime.poll_health", "pass")],
        )

        self.assertTrue(recovered["healthy"])
        self.assertEqual(
            autopilot_supervisor.gate(self.config)["status"],
            "idle",
        )

    def test_archived_heartbeat_target_is_repaired_without_model(self) -> None:
        failure = self.check(
            "automation.active_heartbeat_targets",
            "fail",
            details={
                "targets": [
                    {
                        "automation_id": "mail",
                        "thread_id": "archived-thread",
                    }
                ]
            },
        )
        repair = {
            "action": "unarchive_active_heartbeat_targets",
            "success": True,
            "results": [
                {"thread_id": "archived-thread", "returncode": 0}
            ],
        }
        with mock.patch.object(
            autopilot_supervisor,
            "unarchive_automation_targets",
            return_value=repair,
        ) as unarchive:
            result = autopilot_supervisor.run_once(
                self.config,
                self.contract,
                now=self.now,
                checks=[failure],
            )

        unarchive.assert_called_once()
        self.assertEqual(result["status"], "repair_observing")
        self.assertFalse(result["model_wake_required"])

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
                self.contract,
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
        self.assertEqual(result["owner_thread_id"], "owner")

    def test_queue_latency_incident_yields_relay_to_x(self) -> None:
        queue_latency = self.check("runtime.queue_latency", "fail")
        autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now,
            checks=[queue_latency],
        )
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
                self.contract,
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

    def test_three_ready_gates_then_doctor_wakes_do_not_livelock(
        self,
    ) -> None:
        events = [
            {
                "id": "oldest",
                "first_seen_at": "2026-07-27T17:50:00Z",
            }
        ]
        dispatch_state = {"status": "leased_waiting", "work_kind": "x"}

        for offset in (0, 60, 120):
            observed_at = self.now + timedelta(seconds=offset)
            event_dispatch_state = {
                "status": "dispatch_kicked",
                "reason": "claim_completed_with_pending_queue",
                "requested_at": observed_at.isoformat(),
                "event_ids": ["oldest"],
            }
            relay = system_doctor.relay_progress_check(
                pending_count=1,
                dispatch_state=dispatch_state,
                event_dispatch_state=event_dispatch_state,
                required_event_id="oldest",
                owner=None,
                max_wait_seconds=180,
                now=observed_at,
            )
            latency = system_doctor.queue_latency_check(
                events=events,
                owner=None,
                dispatch_state=dispatch_state,
                event_dispatch_state=event_dispatch_state,
                delivery_grace_seconds=180,
                max_age_seconds=300,
                now=observed_at,
            )
            result = autopilot_supervisor.run_once(
                self.config,
                self.contract,
                now=observed_at,
                checks=[relay, latency],
            )

            self.assertEqual(relay.status, "pass")
            self.assertEqual(latency.status, "warn")
            self.assertEqual(
                latency.details["delivery_source"],
                "event_dispatch",
            )
            self.assertTrue(result["healthy"])
            self.assertFalse(
                autopilot_supervisor.gate(
                    self.config,
                    now=observed_at,
                )["dispatch"]
            )

        last_requested_at = self.now + timedelta(seconds=120)
        expired_at = last_requested_at + timedelta(seconds=181)
        expired_event_dispatch = {
            "status": "dispatch_kicked",
            "reason": "claim_completed_with_pending_queue",
            "requested_at": last_requested_at.isoformat(),
            "event_ids": ["oldest"],
        }
        expired_relay = system_doctor.relay_progress_check(
            pending_count=1,
            dispatch_state=dispatch_state,
            event_dispatch_state=expired_event_dispatch,
            required_event_id="oldest",
            owner=None,
            max_wait_seconds=180,
            now=expired_at,
        )
        expired = system_doctor.queue_latency_check(
            events=events,
            owner=None,
            dispatch_state=dispatch_state,
            event_dispatch_state=expired_event_dispatch,
            delivery_grace_seconds=180,
            max_age_seconds=300,
            now=expired_at,
        )
        result = autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=expired_at,
            checks=[expired_relay, expired],
        )

        self.assertEqual(expired_relay.status, "fail")
        self.assertEqual(expired.status, "fail")
        self.assertFalse(expired.details["active_x_delivery"])
        self.assertFalse(result["healthy"])
        self.assertEqual(
            result["failures"],
            ["runtime.relay_progress", "runtime.queue_latency"],
        )

    def test_mixed_queue_and_database_failure_keeps_repair_priority(
        self,
    ) -> None:
        failures = [
            self.check("runtime.queue_latency", "fail"),
            self.check("runtime.database", "fail"),
        ]
        autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now,
            checks=failures,
        )
        with mock.patch.object(
            autopilot_supervisor.autopilot_bridge,
            "reserve_handoff",
            side_effect=AssertionError(
                "X must wait while mixed repair is pending"
            ),
        ):
            result = autopilot_supervisor.relay_reserve_handoff(
                self.config,
                self.contract,
                lease_seconds=1800,
                now=self.now,
            )

        self.assertEqual(result["route"], "repair")
        self.assertTrue(result["dispatch"])
        self.assertTrue(result["repair_pending"])

    def test_relay_does_not_check_x_while_repair_is_pending(self) -> None:
        database_failure = self.check("runtime.database", "fail")
        autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now,
            checks=[database_failure],
        )
        with mock.patch.object(
            autopilot_supervisor.autopilot_bridge,
            "reserve_handoff",
            side_effect=AssertionError(
                "X must wait while repair is pending"
            ),
        ):
            result = autopilot_supervisor.relay_reserve_handoff(
                self.config,
                self.contract,
                lease_seconds=1800,
                now=self.now,
            )

        self.assertEqual(result["route"], "repair")
        self.assertTrue(result["dispatch"])
        self.assertTrue(result["repair_pending"])
        self.assertEqual(result["owner_thread_id"], "owner")

    def test_relay_does_not_preempt_active_x_owner_for_repair(self) -> None:
        database_failure = self.check("runtime.database", "fail")
        autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now,
            checks=[database_failure],
        )
        with (
            mock.patch.object(
                autopilot_supervisor,
                "active_x_owner_snapshot",
                return_value={
                    "owner_busy": True,
                    "active_event_ids": ["123"],
                    "lease_expires_at": "2026-07-27T18:30:00Z",
                },
            ),
            mock.patch.object(
                autopilot_supervisor,
                "reserve_handoff",
                side_effect=AssertionError(
                    "repair must wait for the active X owner"
                ),
            ),
            mock.patch.object(
                autopilot_supervisor.autopilot_bridge,
                "reserve_handoff",
                side_effect=AssertionError(
                    "X must not receive another handoff"
                ),
            ),
        ):
            result = autopilot_supervisor.relay_reserve_handoff(
                self.config,
                self.contract,
                lease_seconds=1800,
                now=self.now,
            )

        self.assertEqual(result["status"], "repair_waiting_for_x_owner")
        self.assertFalse(result["dispatch"])
        self.assertTrue(result["repair_pending"])
        self.assertTrue(result["owner_busy"])
        self.assertEqual(result["active_event_ids"], ["123"])

    def test_active_x_owner_snapshot_honors_exact_lease(self) -> None:
        state_path = self.root / "var" / "autopilot-dispatch.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "events": {},
                    "owner": {
                        "claim_token": "x-token",
                        "event_ids": ["123"],
                        "claimed_at": "2026-07-27T18:00:00Z",
                        "lease_expires_at": "2026-07-27T18:30:00Z",
                    },
                }
            ),
            encoding="utf-8",
        )

        active = autopilot_supervisor.active_x_owner_snapshot(
            self.config,
            lease_seconds=1800,
            now=self.now + timedelta(minutes=29),
        )
        expired = autopilot_supervisor.active_x_owner_snapshot(
            self.config,
            lease_seconds=1800,
            now=self.now + timedelta(minutes=31),
        )

        self.assertTrue(active["owner_busy"])
        self.assertEqual(active["active_event_ids"], ["123"])
        self.assertFalse(expired["owner_busy"])

    def test_relay_releases_repair_reservation_if_x_owner_wins_race(
        self,
    ) -> None:
        database_failure = self.check("runtime.database", "fail")
        autopilot_supervisor.run_once(
            self.config,
            self.contract,
            now=self.now,
            checks=[database_failure],
        )
        repair = {
            "status": "repair_handoff_pending",
            "dispatch": True,
            "repair_pending": True,
            "incident_id": "incident",
            "reservation_token": "repair-token",
        }
        snapshots = [
            {"owner_busy": False, "active_event_ids": []},
            {
                "owner_busy": True,
                "active_event_ids": ["123"],
                "lease_expires_at": "2026-07-27T18:30:00Z",
            },
        ]
        with (
            mock.patch.object(
                autopilot_supervisor,
                "active_x_owner_snapshot",
                side_effect=snapshots,
            ),
            mock.patch.object(
                autopilot_supervisor,
                "reserve_handoff",
                return_value=repair,
            ),
            mock.patch.object(
                autopilot_supervisor,
                "release_handoff",
            ) as release,
        ):
            result = autopilot_supervisor.relay_reserve_handoff(
                self.config,
                self.contract,
                lease_seconds=1800,
                now=self.now,
            )

        release.assert_called_once_with(
            self.config,
            reservation_token="repair-token",
            reason="x_owner_became_active",
        )
        self.assertEqual(result["status"], "repair_waiting_for_x_owner")
        self.assertFalse(result["dispatch"])

    def test_relay_rejects_owner_drift_before_reserving_work(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "browser_owner_thread_id": "wrong-owner",
                    "autopilot_supervisor_state_file": "var/supervisor.json",
                }
            ),
            encoding="utf-8",
        )

        with mock.patch.object(
            autopilot_supervisor.autopilot_bridge,
            "reserve_handoff",
        ) as reserve_x:
            with self.assertRaisesRegex(
                ValueError,
                "browser owner differs",
            ):
                autopilot_supervisor.relay_reserve_handoff(
                    self.config,
                    self.contract,
                    lease_seconds=1800,
                    now=self.now,
                )

        reserve_x.assert_not_called()

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
