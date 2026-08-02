from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from scripts import launchagent_runtime


class LaunchagentRuntimeTests(unittest.TestCase):
    def test_parse_version(self) -> None:
        self.assertEqual(
            launchagent_runtime.parse_version("3.53.3"),
            (3, 53, 3),
        )

    def test_probe_reads_runtime_versions(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '{"python_version":"3.14.2",'
                '"sqlite_version":"3.53.3"}\n'
            ),
            stderr="",
        )
        with mock.patch(
            "scripts.launchagent_runtime.subprocess.run",
            return_value=completed,
        ):
            runtime = launchagent_runtime.probe(Path("/python"))

        self.assertEqual(runtime.python_version, "3.14.2")
        self.assertEqual(runtime.sqlite_version, "3.53.3")

    def test_require_safe_rejects_old_sqlite(self) -> None:
        runtime = launchagent_runtime.RuntimeInfo(
            executable=Path("/python"),
            python_version="3.11.8",
            sqlite_version="3.51.0",
        )
        with mock.patch(
            "scripts.launchagent_runtime.probe",
            return_value=runtime,
        ):
            with self.assertRaisesRegex(ValueError, "SQLite is too old"):
                launchagent_runtime.require_safe(Path("/python"))

    def test_require_safe_rejects_python_without_union_syntax(self) -> None:
        runtime = launchagent_runtime.RuntimeInfo(
            executable=Path("/python"),
            python_version="3.9.19",
            sqlite_version="3.53.3",
        )
        with mock.patch(
            "scripts.launchagent_runtime.probe",
            return_value=runtime,
        ):
            with self.assertRaisesRegex(ValueError, "Python is too old"):
                launchagent_runtime.require_safe(Path("/python"))

    def test_configured_executable_must_be_absolute(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be absolute"):
            launchagent_runtime.configured_executable(
                {"launchagent_python_executable": "python3"}
            )


if __name__ == "__main__":
    unittest.main()
