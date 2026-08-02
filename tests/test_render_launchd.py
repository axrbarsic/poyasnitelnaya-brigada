from __future__ import annotations

import json
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import render_launchd


class RenderLaunchdTests(unittest.TestCase):
    def test_runtime_launchagents_are_rendered_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            output = root / "rendered"
            config.write_text(
                json.dumps(
                    {
                        "poll_interval_seconds": 60,
                        "watchdog_interval_seconds": 60,
                        "app_server_dispatch_interval_seconds": 45,
                        "wake_file": "var/wake-request.json",
                        "launchagent_python_executable": sys.executable,
                    }
                ),
                encoding="utf-8",
            )

            rendered = render_launchd.render(config, output)

            self.assertEqual(len(rendered), 4)
            self.assertNotIn(
                "com.axrbarsic.xmention.janitor.plist",
                {path.name for path in rendered},
            )
            dispatch_path = next(
                path
                for path in rendered
                if path.name == "com.axrbarsic.xmention.dispatch.plist"
            )
            dispatch_payload = plistlib.loads(
                dispatch_path.read_bytes()
            )
            self.assertEqual(
                dispatch_payload["StartInterval"],
                45,
            )
            watchdog_path = next(
                path
                for path in rendered
                if path.name == "com.axrbarsic.xmention.watchdog.plist"
            )
            watchdog_payload = plistlib.loads(
                watchdog_path.read_bytes()
            )
            watchdog_arguments = watchdog_payload["ProgramArguments"]
            self.assertIn(
                "scripts/autopilot_supervisor.py",
                watchdog_arguments[1],
            )
            self.assertEqual(watchdog_arguments[-1], "run")
            for path in rendered:
                launchagent = plistlib.loads(path.read_bytes())
                self.assertEqual(
                    launchagent["ProgramArguments"][0],
                    sys.executable,
                )

    def test_nonpositive_interval_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "watchdog_interval_seconds": 0,
                        "launchagent_python_executable": sys.executable,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "must be positive"):
                render_launchd.render(config, root / "rendered")


if __name__ == "__main__":
    unittest.main()
