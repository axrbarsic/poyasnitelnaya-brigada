from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from scripts import app_server_dispatch


class AppServerDispatchTests(unittest.TestCase):
    def make_config(self, root: Path, **overrides: object) -> Path:
        payload: dict[str, object] = {
            "browser_owner_thread_id": "owner-thread",
            "browser_owner_cwd": ".",
            "codex_cli_path": "/usr/bin/false",
            "desktop_relay_mode": "in_app_heartbeat",
            "app_server_dispatch_state_file": "var/dispatch.json",
            "app_server_dispatch_lock_file": "var/dispatch.lock",
        }
        payload.update(overrides)
        config = root / "config.json"
        config.write_text(json.dumps(payload), encoding="utf-8")
        return config

    def no_repair(self) -> Any:
        return mock.patch.object(
            app_server_dispatch.autopilot_supervisor,
            "gate",
            return_value={
                "status": "healthy",
                "dispatch": False,
                "repair_pending": False,
            },
        )

    def test_direct_script_entrypoint_loads_without_pythonpath(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(project_root / "scripts/app_server_dispatch.py"),
                    "--help",
                ],
                cwd=temporary,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Wake Codex Desktop", completed.stdout)

    def test_desktop_launch_fails_closed_for_ambiguous_new_pids(
        self,
    ) -> None:
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(
                app_server_dispatch,
                "desktop_processes",
                side_effect=[[], [901, 902]],
            ),
            mock.patch.object(
                app_server_dispatch.subprocess,
                "run",
                return_value=completed,
            ),
        ):
            with self.assertRaisesRegex(
                app_server_dispatch.AppServerError,
                "ownership is ambiguous",
            ):
                app_server_dispatch.launch_desktop(
                    executable=Path("/tmp/codex"),
                    cwd=Path("/tmp"),
                    process_executable="/tmp/Codex",
                    timeout_seconds=1,
                )

    def test_only_in_app_heartbeat_transport_is_accepted(self) -> None:
        for mode in (None, "external_app_server"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                config = self.make_config(root)
                payload = json.loads(config.read_text(encoding="utf-8"))
                if mode is None:
                    payload.pop("desktop_relay_mode")
                else:
                    payload["desktop_relay_mode"] = mode
                config.write_text(json.dumps(payload), encoding="utf-8")

                with mock.patch.object(
                    app_server_dispatch,
                    "desktop_supervisor_dispatch",
                    side_effect=AssertionError("must fail before dispatch"),
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "must be exactly in_app_heartbeat",
                    ):
                        app_server_dispatch.dispatch(
                            config,
                            lease_seconds=1800,
                        )

    def test_idle_gate_never_inspects_or_launches_desktop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self.make_config(root)
            with (
                self.no_repair(),
                mock.patch.object(
                    app_server_dispatch.autopilot_bridge,
                    "gate",
                    return_value={
                        "status": "idle",
                        "dispatch": False,
                        "event_ids": [],
                        "pending_count": 0,
                    },
                ),
                mock.patch.object(
                    app_server_dispatch,
                    "desktop_processes",
                    side_effect=AssertionError("must not inspect Desktop"),
                ),
            ):
                result = app_server_dispatch.dispatch(
                    config,
                    lease_seconds=1800,
                )

            self.assertEqual(result["status"], "idle")
            self.assertFalse(result["desktop_launched"])
            state = json.loads((root / "var/dispatch.json").read_text())
            self.assertEqual(state["mode"], "in_app_heartbeat")

    def test_ready_queue_reuses_running_desktop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self.make_config(root)
            with (
                self.no_repair(),
                mock.patch.object(
                    app_server_dispatch.autopilot_bridge,
                    "gate",
                    return_value={
                        "status": "ready",
                        "dispatch": True,
                        "event_ids": ["123"],
                        "pending_count": 1,
                    },
                ),
                mock.patch.object(
                    app_server_dispatch,
                    "desktop_processes",
                    return_value=[42],
                ),
                mock.patch.object(
                    app_server_dispatch,
                    "launch_desktop",
                    side_effect=AssertionError("must not launch"),
                ),
            ):
                result = app_server_dispatch.dispatch(
                    config,
                    lease_seconds=1800,
                )

            self.assertEqual(result["status"], "desktop_ready_waiting_relay")
            self.assertEqual(result["desktop_pids"], [42])
            self.assertFalse(result["desktop_launched"])
            self.assertIn("waiting_since", result)

    def test_ready_queue_preserves_original_waiting_since(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self.make_config(root)
            state_path = root / "var/dispatch.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "status": "desktop_launched_waiting_relay",
                        "waiting_since": "2026-07-29T14:00:00Z",
                        "work_kind": "x",
                    }
                ),
                encoding="utf-8",
            )
            with (
                self.no_repair(),
                mock.patch.object(
                    app_server_dispatch.autopilot_bridge,
                    "gate",
                    return_value={
                        "status": "ready",
                        "dispatch": True,
                        "event_ids": ["123", "124"],
                        "pending_count": 2,
                    },
                ),
                mock.patch.object(
                    app_server_dispatch,
                    "desktop_processes",
                    return_value=[42],
                ),
            ):
                result = app_server_dispatch.dispatch(
                    config,
                    lease_seconds=1800,
                )

            self.assertEqual(
                result["waiting_since"],
                "2026-07-29T14:00:00Z",
            )

    def test_repair_is_selected_before_x_queue(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self.make_config(root)
            with (
                mock.patch.object(
                    app_server_dispatch.autopilot_supervisor,
                    "gate",
                    return_value={
                        "status": "escalation_pending",
                        "dispatch": True,
                        "repair_pending": True,
                        "incident_id": "incident-1",
                    },
                ),
                mock.patch.object(
                    app_server_dispatch.autopilot_bridge,
                    "gate",
                    side_effect=AssertionError("X gate must wait"),
                ),
                mock.patch.object(
                    app_server_dispatch,
                    "desktop_processes",
                    return_value=[42],
                ),
            ):
                result = app_server_dispatch.dispatch(
                    config,
                    lease_seconds=1800,
                )

            self.assertEqual(result["work_kind"], "repair")
            self.assertEqual(result["repair_incident_id"], "incident-1")

    def test_idle_closes_only_managed_desktop_after_grace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self.make_config(
                root,
                desktop_auto_quit_after_work=True,
                desktop_auto_quit_grace_seconds=60,
            )
            state_path = root / "var/dispatch.json"
            state_path.parent.mkdir(parents=True)
            idle_since = (
                app_server_dispatch.autopilot_dispatch.utc_now()
                - timedelta(seconds=120)
            )
            state_path.write_text(
                json.dumps(
                    {
                        "managed_by_supervisor": True,
                        "managed_desktop_pid": 84,
                        "idle_since": (
                            app_server_dispatch.autopilot_dispatch.isoformat(
                                idle_since
                            )
                        ),
                    }
                ),
                encoding="utf-8",
            )
            with (
                self.no_repair(),
                mock.patch.object(
                    app_server_dispatch.autopilot_bridge,
                    "gate",
                    return_value={
                        "status": "idle",
                        "dispatch": False,
                        "event_ids": [],
                        "pending_count": 0,
                    },
                ),
                mock.patch.object(
                    app_server_dispatch.autopilot_dispatch,
                    "status",
                    return_value={
                        "owner_busy": False,
                        "pending_count": 0,
                        "leased_count": 0,
                    },
                ),
                mock.patch.object(
                    app_server_dispatch.resource_guard,
                    "check",
                    return_value={"defer": False},
                ),
                mock.patch.object(
                    app_server_dispatch,
                    "stop_managed_desktop",
                    return_value=True,
                ) as stop,
            ):
                result = app_server_dispatch.dispatch(
                    config,
                    lease_seconds=1800,
                )

            self.assertEqual(result["status"], "desktop_closed_after_work")
            self.assertTrue(result["desktop_closed"])
            stop.assert_called_once()

    def test_ready_queue_launches_desktop_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self.make_config(root)
            with (
                self.no_repair(),
                mock.patch.object(
                    app_server_dispatch.autopilot_bridge,
                    "gate",
                    return_value={
                        "status": "ready",
                        "dispatch": True,
                        "event_ids": ["123"],
                        "pending_count": 1,
                    },
                ),
                mock.patch.object(
                    app_server_dispatch,
                    "desktop_processes",
                    return_value=[],
                ),
                mock.patch.object(
                    app_server_dispatch,
                    "launch_desktop",
                    return_value=[84],
                ) as launch,
            ):
                result = app_server_dispatch.dispatch(
                    config,
                    lease_seconds=1800,
                )

            self.assertEqual(
                result["status"],
                "desktop_launched_waiting_relay",
            )
            self.assertEqual(result["desktop_pids"], [84])
            launch.assert_called_once()

    def test_launch_failure_preserves_queue_and_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self.make_config(root)
            with (
                self.no_repair(),
                mock.patch.object(
                    app_server_dispatch.autopilot_bridge,
                    "gate",
                    return_value={
                        "status": "ready",
                        "dispatch": True,
                        "event_ids": ["123"],
                        "pending_count": 1,
                    },
                ),
                mock.patch.object(
                    app_server_dispatch,
                    "desktop_processes",
                    return_value=[],
                ),
                mock.patch.object(
                    app_server_dispatch,
                    "launch_desktop",
                    side_effect=app_server_dispatch.AppServerError("boom"),
                ),
                mock.patch.object(
                    app_server_dispatch,
                    "notify_desktop_failure",
                    return_value=True,
                ) as notify,
            ):
                result = app_server_dispatch.dispatch(
                    config,
                    lease_seconds=1800,
                )

            self.assertEqual(result["status"], "desktop_launch_failed")
            self.assertEqual(result["event_ids"], ["123"])
            self.assertFalse(result["dispatched"])
            notify.assert_called_once()

    def test_second_process_stops_before_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self.make_config(root)
            lock_path = root / "var/dispatch.lock"
            lock_path.parent.mkdir(parents=True)
            with lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(
                    handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                with mock.patch.object(
                    app_server_dispatch,
                    "dispatch",
                    side_effect=AssertionError("must not reach gate"),
                ):
                    result = app_server_dispatch.run_once(
                        config,
                        lease_seconds=1800,
                    )

            self.assertEqual(result["status"], "dispatcher_busy")
            self.assertFalse(result["dispatched"])
            state = json.loads((root / "var/dispatch.json").read_text())
            self.assertEqual(state["status"], "dispatcher_busy")


if __name__ == "__main__":
    unittest.main()
