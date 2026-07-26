from __future__ import annotations

import contextlib
import io
import json
import unittest

from scripts import autopilot_resume


class RetiredAutopilotResumeTests(unittest.TestCase):
    def test_cli_launcher_fails_closed_without_claiming_or_starting_codex(
        self,
    ) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = autopilot_resume.main()

        self.assertEqual(result, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["status"], "retired")
        self.assertIn("cannot access the built-in Browser", payload["error"])


if __name__ == "__main__":
    unittest.main()
