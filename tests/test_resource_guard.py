from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from scripts import resource_guard


class ResourceGuardTests(unittest.TestCase):
    def test_process_parser_counts_codex_envelope(self) -> None:
        output = "\n".join(
            [
                "204800 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
                "102400 /Applications/ChatGPT.app/Contents/Frameworks/"
                "ChatGPT Helper (Renderer).app/Contents/MacOS/"
                "ChatGPT Helper (Renderer) --type=renderer",
                "51200 node /tmp/node_repl/server.js",
                "25600 uv run mcp_server.py",
            ]
        )

        sample = resource_guard.parse_processes(output)

        self.assertEqual(sample.codex_rss_mb, 300.0)
        self.assertEqual(sample.renderer_count, 1)
        self.assertEqual(sample.node_repl_count, 1)
        self.assertEqual(sample.mcp_process_count, 1)

    def test_assess_reports_only_exceeded_limits(self) -> None:
        sample = resource_guard.ResourceSample(
            codex_rss_mb=2400,
            renderer_count=9,
            node_repl_count=4,
            mcp_process_count=6,
            free_percent=10,
        )
        config = {
            "memory_guard_max_codex_rss_mb": 2300,
            "memory_guard_max_renderer_count": 8,
            "memory_guard_max_node_repl_count": 6,
            "memory_guard_max_mcp_process_count": 10,
            "memory_guard_min_free_percent": 12,
        }

        reasons = resource_guard.assess(sample, config)

        self.assertEqual(len(reasons), 3)
        self.assertTrue(any("codex_rss_mb" in reason for reason in reasons))
        self.assertTrue(any("renderer_count" in reason for reason in reasons))
        self.assertTrue(any("free_percent" in reason for reason in reasons))

    def test_system_metric_parsers(self) -> None:
        self.assertEqual(
            resource_guard.parse_idle_seconds('"HIDIdleTime" = 9000000000'),
            9,
        )
        self.assertTrue(
            resource_guard.parse_on_ac_power("Now drawing from 'AC Power'")
        )
        self.assertEqual(
            resource_guard.parse_swap_used_mb(
                "total = 1024.00M  used = 266.75M  free = 757.25M"
            ),
            266.75,
        )
        self.assertEqual(
            resource_guard.parse_runtime_id(
                "101 /Applications/Codex.app/Contents/MacOS/Codex\n"
                "102 /Applications/Codex.app/Contents/Frameworks/"
                "Codex Helper.app/Contents/MacOS/Codex Helper"
            ),
            "/Applications/Codex.app/Contents/MacOS/Codex:101",
        )

    def test_auto_mode_prefers_performance_only_when_idle_on_ac(self) -> None:
        config = {
            "resource_mode": "auto",
            "memory_guard_profiles": {"balanced": {}},
            "resource_mode_pressure_free_percent": 20,
            "resource_mode_pressure_codex_rss_mb": 2250,
            "resource_mode_pressure_swap_used_mb": 768,
            "resource_mode_idle_seconds": 900,
        }
        idle = resource_guard.ResourceSample(
            codex_rss_mb=1800,
            renderer_count=4,
            node_repl_count=3,
            mcp_process_count=6,
            free_percent=45,
            user_idle_seconds=1200,
            on_ac_power=True,
            swap_used_mb=200,
        )
        active = resource_guard.ResourceSample(
            **{**idle.__dict__, "user_idle_seconds": 30}
        )
        pressure = resource_guard.ResourceSample(
            **{**idle.__dict__, "codex_rss_mb": 2300}
        )

        self.assertEqual(
            resource_guard.select_mode(idle, config), "performance"
        )
        self.assertEqual(
            resource_guard.select_mode(active, config), "balanced"
        )
        self.assertEqual(
            resource_guard.select_mode(pressure, config), "efficiency"
        )

    def test_profile_limit_and_hard_cap_both_apply(self) -> None:
        sample = resource_guard.ResourceSample(
            codex_rss_mb=2750,
            renderer_count=4,
            node_repl_count=3,
            mcp_process_count=6,
            free_percent=40,
            swap_used_mb=200,
        )
        limits = {
            "memory_guard_profiles": {
                "performance": {
                    "memory_guard_max_codex_rss_mb": 3000,
                    "memory_guard_max_renderer_count": 7,
                    "memory_guard_max_node_repl_count": 9,
                    "memory_guard_max_mcp_process_count": 18,
                    "memory_guard_min_free_percent": 12,
                    "memory_guard_max_swap_used_mb": 1152,
                }
            },
            "memory_guard_hard_caps": {
                "memory_guard_max_codex_rss_mb": 2700,
                "memory_guard_max_renderer_count": 8,
                "memory_guard_max_node_repl_count": 10,
                "memory_guard_max_mcp_process_count": 20,
                "memory_guard_min_free_percent": 10,
                "memory_guard_max_swap_used_mb": 1280,
            },
        }

        reasons = resource_guard.assess(
            sample, limits, mode="performance"
        )

        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith("hard_cap:"))

    def test_collect_falls_back_when_ps_is_sandboxed(self) -> None:
        native = resource_guard.ResourceSample(
            codex_rss_mb=1900,
            renderer_count=5,
            node_repl_count=3,
            mcp_process_count=4,
            free_percent=None,
        )
        with (
            mock.patch(
                "scripts.resource_guard.subprocess.run",
                side_effect=PermissionError("ps denied"),
            ),
            mock.patch(
                "scripts.resource_guard.collect_native_processes",
                return_value=native,
            ) as fallback,
        ):
            sample = resource_guard.collect()

        fallback.assert_called_once_with()
        self.assertEqual(sample.codex_rss_mb, 1900)
        self.assertIsNone(sample.free_percent)

    def test_collect_keeps_process_sample_if_memory_pressure_fails(self) -> None:
        ps_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "102400 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT\n"
            ),
            stderr="",
        )
        with mock.patch(
            "scripts.resource_guard.subprocess.run",
            side_effect=[
                ps_result,
                PermissionError("memory pressure denied"),
                PermissionError("ioreg denied"),
                PermissionError("pmset denied"),
                PermissionError("sysctl denied"),
            ],
        ):
            sample = resource_guard.collect()

        self.assertEqual(sample.codex_rss_mb, 100.0)
        self.assertIsNone(sample.free_percent)


if __name__ == "__main__":
    unittest.main()
