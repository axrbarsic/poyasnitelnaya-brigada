from __future__ import annotations

import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from scripts import render_launchd


class RenderLaunchdTests(unittest.TestCase):
    def test_janitor_minimum_age_is_rendered_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            output = root / "rendered"
            config.write_text(
                json.dumps(
                    {
                        "poll_interval_seconds": 60,
                        "watchdog_interval_seconds": 60,
                        "session_janitor_interval_seconds": 300,
                        "session_janitor_minimum_age_seconds": 60,
                        "wake_file": "var/wake-request.json",
                    }
                ),
                encoding="utf-8",
            )

            rendered = render_launchd.render(config, output)

            janitor_path = next(
                path
                for path in rendered
                if path.name == "com.axrbarsic.xmention.janitor.plist"
            )
            payload = plistlib.loads(janitor_path.read_bytes())
            arguments = payload["ProgramArguments"]
            age_index = arguments.index("--minimum-age-seconds") + 1
            self.assertEqual(arguments[age_index], "60")

    def test_janitor_minimum_age_rejects_unsafe_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "session_janitor_minimum_age_seconds": 59,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "at least 60 seconds"):
                render_launchd.render(config, root / "rendered")


if __name__ == "__main__":
    unittest.main()
