from __future__ import annotations

import fcntl
import json
import tempfile
import unittest
from collections import deque
from datetime import timedelta
from pathlib import Path
from unittest import mock

from scripts import app_server_dispatch


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.sent: list[dict] = []
        self.closed = False
        self.__class__.instances.append(self)

    def send(self, payload: dict) -> None:
        self.sent.append(payload)

    def wait_response(self, request_id: int, *, deadline: float) -> dict:
        if request_id == 0:
            return {"userAgent": "test"}
        if request_id == 1:
            return {"thread": {"id": "relay-thread"}}
        if request_id == 2:
            return {
                "thread": {"id": "relay-thread"},
                "model": "gpt-5.6-luna",
                "cwd": self.kwargs["cwd"],
            }
        if request_id == 3:
            return {"turn": {"id": "turn-1", "status": "inProgress"}}
        if request_id == 4:
            return {}
        raise AssertionError(request_id)

    def wait_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        deadline: float,
    ) -> dict:
        return {
            "id": turn_id,
            "status": "completed",
            "items": [
                {
                    "id": "tool-1",
                    "type": "dynamicToolCall",
                    "tool": "codex_app.send_message_to_thread",
                    "status": "completed",
                    "success": True,
                    "arguments": {},
                }
            ],
        }

    def close(self) -> None:
        self.closed = True


class AppServerDispatchTests(unittest.TestCase):
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

    def setUp(self) -> None:
        FakeClient.instances.clear()

    def make_config(self, root: Path) -> Path:
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "browser_owner_thread_id": "owner-thread",
                    "app_server_relay_thread_id": "relay-thread",
                    "browser_owner_cwd": ".",
                    "codex_cli_path": "/usr/bin/false",
                    "app_server_dispatch_state_file": "var/dispatch.json",
                    "app_server_dispatch_lock_file": "var/dispatch.lock",
                }
            ),
            encoding="utf-8",
        )
        return config

    def test_idle_gate_never_starts_app_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            with (
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
                    "AppServerClient",
                    side_effect=AssertionError("must not start"),
                ),
            ):
                result = app_server_dispatch.dispatch(
                    config,
                    lease_seconds=1800,
                )

            self.assertFalse(result["dispatched"])
            state = json.loads(
                (Path(directory) / "var/dispatch.json").read_text()
            )
            self.assertEqual(state["status"], "idle")

    def test_desktop_supervisor_idle_never_checks_or_launches_desktop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            payload = json.loads(config.read_text(encoding="utf-8"))
            payload["desktop_relay_mode"] = "in_app_heartbeat"
            config.write_text(json.dumps(payload), encoding="utf-8")
            with (
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
                mock.patch.object(
                    app_server_dispatch,
                    "AppServerClient",
                    side_effect=AssertionError("must not start app-server"),
                ),
            ):
                result = app_server_dispatch.dispatch(
                    config,
                    lease_seconds=1800,
                )

            self.assertEqual(result["status"], "idle")
            self.assertFalse(result["desktop_launched"])

    def test_desktop_supervisor_reuses_running_desktop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            payload = json.loads(config.read_text(encoding="utf-8"))
            payload["desktop_relay_mode"] = "in_app_heartbeat"
            config.write_text(json.dumps(payload), encoding="utf-8")
            with (
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

            self.assertEqual(
                result["status"],
                "desktop_ready_waiting_relay",
            )
            self.assertEqual(result["desktop_pids"], [42])
            self.assertFalse(result["desktop_launched"])
            self.assertIn("waiting_since", result)

    def test_desktop_supervisor_preserves_relay_waiting_since(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            payload = json.loads(config.read_text(encoding="utf-8"))
            payload["desktop_relay_mode"] = "in_app_heartbeat"
            config.write_text(json.dumps(payload), encoding="utf-8")
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
                result["status"],
                "desktop_ready_waiting_relay",
            )
            self.assertEqual(
                result["waiting_since"],
                "2026-07-29T14:00:00Z",
            )

    def test_desktop_supervisor_prioritizes_repair_over_x_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            payload = json.loads(config.read_text(encoding="utf-8"))
            payload["desktop_relay_mode"] = "in_app_heartbeat"
            config.write_text(json.dumps(payload), encoding="utf-8")
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
            self.assertEqual(
                result["status"],
                "desktop_ready_waiting_relay",
            )

    def test_idle_closes_only_supervisor_managed_desktop_after_grace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            payload = json.loads(config.read_text(encoding="utf-8"))
            payload.update(
                {
                    "desktop_relay_mode": "in_app_heartbeat",
                    "desktop_auto_quit_after_work": True,
                    "desktop_auto_quit_grace_seconds": 60,
                }
            )
            config.write_text(json.dumps(payload), encoding="utf-8")
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
                        "idle_since":
                            app_server_dispatch.autopilot_dispatch.isoformat(
                                idle_since
                            ),
                    }
                ),
                encoding="utf-8",
            )
            with (
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

            self.assertEqual(
                result["status"],
                "desktop_closed_after_work",
            )
            self.assertTrue(result["desktop_closed"])
            stop.assert_called_once()

    def test_desktop_supervisor_launches_only_for_ready_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            payload = json.loads(config.read_text(encoding="utf-8"))
            payload["desktop_relay_mode"] = "in_app_heartbeat"
            config.write_text(json.dumps(payload), encoding="utf-8")
            with (
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
            self.assertTrue(result["desktop_launched"])
            launch.assert_called_once()

    def test_desktop_launch_failure_preserves_queue_and_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            payload = json.loads(config.read_text(encoding="utf-8"))
            payload["desktop_relay_mode"] = "in_app_heartbeat"
            config.write_text(json.dumps(payload), encoding="utf-8")
            gate = {
                "status": "ready",
                "dispatch": True,
                "event_ids": ["123"],
                "pending_count": 1,
            }
            with (
                mock.patch.object(
                    app_server_dispatch.autopilot_bridge,
                    "gate",
                    return_value=gate,
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
            self.assertEqual(result["pending_count"], 1)
            self.assertFalse(result["dispatched"])
            self.assertTrue(result["alert_sent"])
            notify.assert_called_once()

    def test_second_process_stops_before_gate_or_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
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
            state = json.loads(
                (root / "var/dispatch.json").read_text()
            )
            self.assertEqual(state["status"], "dispatcher_busy")

    def test_ready_gate_uses_luna_relay_for_sol_max_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            with (
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
                    "AppServerClient",
                    FakeClient,
                ),
                mock.patch.object(
                    app_server_dispatch,
                    "wait_owner_resolution",
                ) as wait_owner,
            ):
                result = app_server_dispatch.dispatch(
                    config,
                    lease_seconds=1800,
                )

            self.assertEqual(result["status"], "completed")
            client = FakeClient.instances[0]
            resume = next(
                payload
                for payload in client.sent
                if payload.get("method") == "thread/resume"
            )
            unarchive = next(
                payload
                for payload in client.sent
                if payload.get("method") == "thread/unarchive"
            )
            archive = next(
                payload
                for payload in client.sent
                if payload.get("method") == "thread/archive"
            )
            turn = next(
                payload
                for payload in client.sent
                if payload.get("method") == "turn/start"
            )
            self.assertEqual(
                resume["params"]["threadId"],
                "relay-thread",
            )
            self.assertEqual(
                unarchive["params"]["threadId"],
                "relay-thread",
            )
            self.assertEqual(
                archive["params"]["threadId"],
                "relay-thread",
            )
            self.assertEqual(turn["params"]["model"], "gpt-5.6-luna")
            self.assertEqual(turn["params"]["effort"], "low")
            self.assertEqual(
                turn["params"]["cwd"],
                str(root.resolve()),
            )
            self.assertIn(
                "codex_app.send_message_to_thread",
                turn["params"]["input"][0]["text"],
            )
            self.assertIn(
                "owner-thread",
                turn["params"]["input"][0]["text"],
            )
            self.assertIn(
                "python3 scripts/autopilot_bridge.py",
                turn["params"]["input"][0]["text"],
            )
            wait_owner.assert_called_once()
            self.assertTrue(client.closed)

    def test_wrong_resumed_thread_fails_closed(self) -> None:
        class WrongThreadClient(FakeClient):
            def wait_response(
                self,
                request_id: int,
                *,
                deadline: float,
            ) -> dict:
                if request_id == 2:
                    return {"thread": {"id": "wrong-thread"}}
                return super().wait_response(
                    request_id,
                    deadline=deadline,
                )

        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            with (
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
                    "AppServerClient",
                    WrongThreadClient,
                ),
            ):
                with self.assertRaisesRegex(
                    app_server_dispatch.AppServerError,
                    "wrong relay thread",
                ):
                    app_server_dispatch.dispatch(
                        config,
                        lease_seconds=1800,
                    )

            state = json.loads(
                (Path(directory) / "var/dispatch.json").read_text()
            )
            self.assertEqual(state["status"], "failed")

    def test_completed_relay_without_handoff_fails_promptly(self) -> None:
        class NoHandoffClient(FakeClient):
            def wait_turn(
                self,
                *,
                thread_id: str,
                turn_id: str,
                deadline: float,
            ) -> dict:
                return {
                    "id": turn_id,
                    "status": "completed",
                    "items": [],
                }

        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            with (
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
                    "AppServerClient",
                    NoHandoffClient,
                ),
                mock.patch.object(
                    app_server_dispatch,
                    "wait_owner_resolution",
                ) as wait_owner,
            ):
                with self.assertRaisesRegex(
                    app_server_dispatch.AppServerError,
                    "without a confirmed owner handoff",
                ):
                    app_server_dispatch.dispatch(
                        config,
                        lease_seconds=1800,
                    )

            wait_owner.assert_not_called()
            state = json.loads(
                (Path(directory) / "var/dispatch.json").read_text()
            )
            self.assertEqual(state["status"], "blocked")
            self.assertEqual(
                state["blocker_code"],
                "cross_thread_tool_unavailable",
            )

    def test_capability_latch_stops_before_app_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            signature = app_server_dispatch.runtime_signature(
                Path("/usr/bin/false")
            )
            state_path = root / "var/dispatch.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "status": "blocked",
                        "blocker_code": "cross_thread_tool_unavailable",
                        "relay_runtime_signature": signature,
                    }
                ),
                encoding="utf-8",
            )
            with (
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
                    "AppServerClient",
                    side_effect=AssertionError("must not start"),
                ),
            ):
                result = app_server_dispatch.dispatch(
                    config,
                    lease_seconds=1800,
                )

            self.assertEqual(result["status"], "relay_blocked")
            self.assertFalse(result["dispatched"])

    def test_turn_completion_received_before_response_is_buffered(self) -> None:
        client = app_server_dispatch.AppServerClient.__new__(
            app_server_dispatch.AppServerClient
        )
        client.pending_messages = deque()
        turn_completed = {
            "method": "turn/completed",
            "params": {
                "threadId": "owner-thread",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        }
        response = {"id": 2, "result": {"turn": {"id": "turn-1"}}}
        client.messages = mock.Mock(
            return_value=iter([turn_completed, response])
        )

        started = client.wait_response(2, deadline=1.0)
        completed = client.wait_turn(
            thread_id="owner-thread",
            turn_id="turn-1",
            deadline=1.0,
        )

        self.assertEqual(started["turn"]["id"], "turn-1")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(list(client.pending_messages), [])

    def test_wait_turn_reads_persisted_terminal_status(self) -> None:
        client = app_server_dispatch.AppServerClient.__new__(
            app_server_dispatch.AppServerClient
        )
        client.pending_messages = deque()
        client.next_request_id = 1000
        client.read_message = mock.Mock(return_value=None)
        client.send = mock.Mock()
        client.wait_response = mock.Mock(
            return_value={
                "thread": {
                    "turns": [
                        {
                            "id": "turn-1",
                            "status": "interrupted",
                            "items": [],
                        }
                    ]
                }
            }
        )

        turn = client.wait_turn(
            thread_id="relay-thread",
            turn_id="turn-1",
            deadline=10**12,
        )

        self.assertEqual(turn["status"], "interrupted")
        client.send.assert_called_once_with(
            {
                "method": "thread/read",
                "id": 1000,
                "params": {
                    "threadId": "relay-thread",
                    "includeTurns": True,
                },
            }
        )

    def test_relay_prompt_requires_direct_tool_discovery(self) -> None:
        self.assertIn("`tool_search`", app_server_dispatch.RELAY_PROMPT)
        self.assertIn(
            "`codex_app.send_message_to_thread`",
            app_server_dispatch.RELAY_PROMPT,
        )


if __name__ == "__main__":
    unittest.main()
