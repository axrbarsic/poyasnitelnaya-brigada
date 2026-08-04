from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import event_dispatch


class EventDispatchTests(unittest.TestCase):
    def make_config(
        self,
        root: Path,
        *,
        enabled: bool = True,
    ) -> Path:
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "event_dispatch_on_new_events": enabled,
                    "event_dispatch_state_file": "var/event-dispatch.json",
                }
            ),
            encoding="utf-8",
        )
        return config

    def test_live_new_event_kicks_dispatch_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.make_config(root)
            runner = mock.Mock(
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="",
                    stderr="",
                )
            )

            result = event_dispatch.trigger_from_poll_result(
                config,
                {
                    "new_event_ids": ["2082313815820005869"],
                    "new_count": 1,
                },
                live_poll=True,
                runner=runner,
            )

            self.assertEqual(result["status"], "dispatch_kicked")
            self.assertTrue(result["triggered"])
            runner.assert_called_once()
            command = runner.call_args.args[0]
            self.assertEqual(command[:2], ["/bin/launchctl", "kickstart"])
            self.assertTrue(
                command[2].endswith(
                    "/com.axrbarsic.xmention.dispatch"
                )
            )
            state = json.loads(
                (root / "var" / "event-dispatch.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                state["event_ids"],
                ["2082313815820005869"],
            )
            self.assertEqual(
                state["reason"],
                event_dispatch.NEW_EVENTS_REASON,
            )

    def test_completion_request_is_durable_before_kick(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.make_config(root)
            observed_request: dict = {}

            def runner(*_args: object, **_kwargs: object) -> object:
                observed_request.update(
                    json.loads(
                        (
                            root / "var" / "event-dispatch.json"
                        ).read_text(encoding="utf-8")
                    )
                )
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="",
                    stderr="",
                )

            result = event_dispatch.trigger_pending_events(
                config,
                ["2082313815820005870", "2082313815820005869"],
                runner=runner,
            )

            self.assertEqual(
                observed_request["status"],
                "dispatch_requested",
            )
            self.assertEqual(
                observed_request["reason"],
                event_dispatch.CLAIM_COMPLETED_REASON,
            )
            self.assertEqual(
                observed_request["event_ids"],
                ["2082313815820005869", "2082313815820005870"],
            )
            self.assertEqual(result["status"], "dispatch_kicked")
            self.assertTrue(result["triggered"])

    def test_empty_or_fixture_poll_does_not_kick(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.make_config(root)
            runner = mock.Mock()

            empty = event_dispatch.trigger_from_poll_result(
                config,
                {"new_event_ids": [], "new_count": 0},
                live_poll=True,
                runner=runner,
            )
            fixture = event_dispatch.trigger_from_poll_result(
                config,
                {"new_event_ids": ["2082313815820005869"]},
                live_poll=False,
                runner=runner,
            )

            self.assertEqual(empty["status"], "not_needed")
            self.assertEqual(fixture["status"], "not_needed")
            runner.assert_not_called()

    def test_launchctl_failure_keeps_minute_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.make_config(root)
            runner = mock.Mock(
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=3,
                    stdout="",
                    stderr="service unavailable",
                )
            )

            result = event_dispatch.trigger_from_poll_result(
                config,
                {"new_event_ids": ["2082313815820005869"]},
                live_poll=True,
                runner=runner,
            )

            self.assertEqual(result["status"], "fallback_scheduled")
            self.assertFalse(result["triggered"])
            self.assertEqual(result["returncode"], 3)
            self.assertEqual(result["error"], "service unavailable")

    def test_disabled_fast_path_does_not_kick(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.make_config(root, enabled=False)
            runner = mock.Mock()

            result = event_dispatch.trigger_from_poll_result(
                config,
                {"new_event_ids": ["2082313815820005869"]},
                live_poll=True,
                runner=runner,
            )

            self.assertEqual(result["status"], "disabled")
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
