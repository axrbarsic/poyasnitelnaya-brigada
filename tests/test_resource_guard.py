from __future__ import annotations

import subprocess
import unittest
from datetime import datetime, timezone
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
        self.assertEqual(sample.node_repl_rss_mb, 50.0)
        self.assertEqual(sample.mcp_process_rss_mb, 25.0)

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

    def test_active_voice_forces_efficiency_mode(self) -> None:
        config = {
            "resource_mode": "auto",
            "memory_guard_profiles": {"balanced": {}},
            "resource_mode_pressure_free_percent": 20,
            "resource_mode_pressure_codex_rss_mb": 2250,
            "resource_mode_pressure_swap_used_mb": 768,
            "resource_mode_idle_seconds": 900,
        }
        sample = resource_guard.ResourceSample(
            codex_rss_mb=1200,
            renderer_count=2,
            node_repl_count=1,
            mcp_process_count=2,
            free_percent=60,
            user_idle_seconds=1800,
            on_ac_power=True,
            swap_used_mb=0,
            voice_active=True,
            voice_input_pids=(123,),
        )

        self.assertEqual(
            resource_guard.select_mode(sample, config), "efficiency"
        )

    def test_codex_audio_command_requires_codex_audio_service(self) -> None:
        self.assertTrue(
            resource_guard.is_codex_audio_command(
                "/Applications/ChatGPT.app/Contents/Frameworks/"
                "Codex Helper --utility-sub-type=audio.mojom.AudioService"
            )
        )
        self.assertFalse(
            resource_guard.is_codex_audio_command(
                "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"
            )
        )

    def test_voice_hold_covers_quiet_gap_between_replies(self) -> None:
        now = datetime(2026, 7, 26, 13, 50, tzinfo=timezone.utc)
        sample = resource_guard.ResourceSample(
            codex_rss_mb=1200,
            renderer_count=2,
            node_repl_count=1,
            mcp_process_count=2,
            free_percent=60,
            voice_active=False,
        )

        held, until, observed = resource_guard.apply_voice_hold(
            sample,
            {"voice_priority_until": "2026-07-26T13:52:00Z"},
            now=now,
            hold_seconds=300,
        )

        self.assertTrue(held.voice_active)
        self.assertEqual(until, "2026-07-26T13:52:00Z")
        self.assertFalse(observed)

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

    def test_historical_swap_does_not_block_after_memory_recovers(self) -> None:
        sample = resource_guard.ResourceSample(
            codex_rss_mb=1500,
            renderer_count=3,
            node_repl_count=3,
            mcp_process_count=6,
            free_percent=40,
            swap_used_mb=4096,
        )
        limits = {
            "memory_guard_max_codex_rss_mb": 2200,
            "memory_guard_max_renderer_count": 5,
            "memory_guard_max_node_repl_count": 6,
            "memory_guard_max_mcp_process_count": 12,
            "memory_guard_min_free_percent": 20,
            "memory_guard_max_swap_used_mb": 896,
            "memory_guard_swap_recovery_free_percent": 30,
            "memory_guard_swap_recovery_codex_rss_mb": 2000,
        }

        reasons = resource_guard.assess(sample, limits)

        self.assertEqual(reasons, [])

    def test_historical_swap_does_not_force_efficiency_mode(self) -> None:
        sample = resource_guard.ResourceSample(
            codex_rss_mb=1500,
            renderer_count=3,
            node_repl_count=7,
            mcp_process_count=15,
            free_percent=40,
            user_idle_seconds=30,
            on_ac_power=True,
            swap_used_mb=4096,
        )
        config = {
            "resource_mode": "auto",
            "memory_guard_profiles": {"balanced": {}},
            "resource_mode_pressure_free_percent": 20,
            "resource_mode_pressure_codex_rss_mb": 2250,
            "resource_mode_pressure_swap_used_mb": 768,
            "resource_mode_idle_seconds": 900,
            "memory_guard_swap_recovery_free_percent": 30,
            "memory_guard_swap_recovery_codex_rss_mb": 2000,
        }

        self.assertEqual(
            resource_guard.select_mode(sample, config),
            "balanced",
        )

    def test_swap_still_blocks_under_current_memory_pressure(self) -> None:
        sample = resource_guard.ResourceSample(
            codex_rss_mb=2100,
            renderer_count=3,
            node_repl_count=3,
            mcp_process_count=6,
            free_percent=15,
            swap_used_mb=4096,
        )
        limits = {
            "memory_guard_max_codex_rss_mb": 2200,
            "memory_guard_max_renderer_count": 5,
            "memory_guard_max_node_repl_count": 6,
            "memory_guard_max_mcp_process_count": 12,
            "memory_guard_min_free_percent": 20,
            "memory_guard_max_swap_used_mb": 896,
            "memory_guard_swap_recovery_free_percent": 30,
            "memory_guard_swap_recovery_codex_rss_mb": 2000,
        }

        reasons = resource_guard.assess(sample, limits)

        self.assertTrue(any("swap_used_mb" in reason for reason in reasons))

    def test_helper_count_does_not_block_healthy_memory_envelope(self) -> None:
        sample = resource_guard.ResourceSample(
            codex_rss_mb=1500,
            renderer_count=3,
            node_repl_count=10,
            mcp_process_count=24,
            free_percent=40,
            swap_used_mb=0,
            node_repl_rss_mb=40,
            mcp_process_rss_mb=120,
        )
        limits = {
            "memory_guard_max_codex_rss_mb": 2200,
            "memory_guard_max_renderer_count": 5,
            "memory_guard_max_node_repl_count": 6,
            "memory_guard_max_mcp_process_count": 12,
            "memory_guard_min_free_percent": 20,
            "memory_guard_max_swap_used_mb": 896,
            "memory_guard_helper_recovery_free_percent": 30,
            "memory_guard_helper_recovery_codex_rss_mb": 1800,
        }

        reasons = resource_guard.assess(sample, limits)

        self.assertEqual(reasons, [])

    def test_helper_count_blocks_when_free_memory_is_low(self) -> None:
        sample = resource_guard.ResourceSample(
            codex_rss_mb=1500,
            renderer_count=3,
            node_repl_count=10,
            mcp_process_count=24,
            free_percent=25,
            swap_used_mb=0,
            node_repl_rss_mb=40,
            mcp_process_rss_mb=120,
        )
        limits = {
            "memory_guard_max_codex_rss_mb": 2200,
            "memory_guard_max_renderer_count": 5,
            "memory_guard_max_node_repl_count": 6,
            "memory_guard_max_mcp_process_count": 12,
            "memory_guard_min_free_percent": 20,
            "memory_guard_max_swap_used_mb": 896,
            "memory_guard_helper_recovery_free_percent": 30,
            "memory_guard_helper_recovery_codex_rss_mb": 1800,
        }

        reasons = resource_guard.assess(sample, limits)

        self.assertTrue(any("node_repl_count" in reason for reason in reasons))
        self.assertTrue(
            any("mcp_process_count" in reason for reason in reasons)
        )

    def test_helper_count_blocks_when_helpers_are_memory_heavy(self) -> None:
        sample = resource_guard.ResourceSample(
            codex_rss_mb=1200,
            renderer_count=3,
            node_repl_count=10,
            mcp_process_count=24,
            free_percent=35,
            swap_used_mb=0,
            node_repl_rss_mb=200,
            mcp_process_rss_mb=400,
        )
        limits = {
            "memory_guard_max_codex_rss_mb": 2200,
            "memory_guard_max_renderer_count": 5,
            "memory_guard_max_node_repl_count": 6,
            "memory_guard_max_mcp_process_count": 12,
            "memory_guard_min_free_percent": 20,
            "memory_guard_max_swap_used_mb": 896,
            "memory_guard_helper_recovery_free_percent": 25,
            "memory_guard_helper_recovery_codex_rss_mb": 1800,
            "memory_guard_helper_recovery_total_rss_mb": 512,
        }

        reasons = resource_guard.assess(sample, limits)

        self.assertTrue(any("node_repl_count" in reason for reason in reasons))
        self.assertTrue(
            any("mcp_process_count" in reason for reason in reasons)
        )

    def test_high_free_envelope_allows_one_transient_dispatch(self) -> None:
        sample = resource_guard.ResourceSample(
            codex_rss_mb=2550,
            renderer_count=5,
            node_repl_count=13,
            mcp_process_count=39,
            free_percent=62,
            swap_used_mb=3900,
            node_repl_rss_mb=75,
            mcp_process_rss_mb=320,
        )
        limits = {
            "memory_guard_max_codex_rss_mb": 2200,
            "memory_guard_max_renderer_count": 5,
            "memory_guard_max_node_repl_count": 6,
            "memory_guard_max_mcp_process_count": 12,
            "memory_guard_min_free_percent": 20,
            "memory_guard_max_swap_used_mb": 896,
            "memory_guard_high_free_recovery_percent": 50,
            "memory_guard_high_free_recovery_codex_rss_mb": 2700,
            "memory_guard_high_free_recovery_renderer_count": 5,
            "memory_guard_high_free_recovery_helper_rss_mb": 512,
        }

        self.assertEqual(resource_guard.assess(sample, limits), [])

    def test_high_free_envelope_still_blocks_large_helper_rss(self) -> None:
        sample = resource_guard.ResourceSample(
            codex_rss_mb=2550,
            renderer_count=5,
            node_repl_count=13,
            mcp_process_count=39,
            free_percent=62,
            swap_used_mb=3900,
            node_repl_rss_mb=250,
            mcp_process_rss_mb=400,
        )
        limits = {
            "memory_guard_max_codex_rss_mb": 2200,
            "memory_guard_max_renderer_count": 5,
            "memory_guard_max_node_repl_count": 6,
            "memory_guard_max_mcp_process_count": 12,
            "memory_guard_min_free_percent": 20,
            "memory_guard_max_swap_used_mb": 896,
            "memory_guard_high_free_recovery_percent": 50,
            "memory_guard_high_free_recovery_codex_rss_mb": 2700,
            "memory_guard_high_free_recovery_renderer_count": 5,
            "memory_guard_high_free_recovery_helper_rss_mb": 512,
        }

        reasons = resource_guard.assess(sample, limits)

        self.assertTrue(any("codex_rss_mb" in reason for reason in reasons))
        self.assertTrue(any("node_repl_count" in reason for reason in reasons))
        self.assertTrue(any("swap_used_mb" in reason for reason in reasons))

    def test_high_free_policy_also_applies_to_hard_caps(self) -> None:
        sample = resource_guard.ResourceSample(
            codex_rss_mb=2256,
            renderer_count=4,
            node_repl_count=13,
            mcp_process_count=11,
            free_percent=46,
            swap_used_mb=971,
            node_repl_rss_mb=84,
            mcp_process_rss_mb=68,
        )
        config = {
            "memory_guard_profiles": {
                "efficiency": {
                    "memory_guard_max_codex_rss_mb": 2200,
                    "memory_guard_max_renderer_count": 5,
                    "memory_guard_max_node_repl_count": 6,
                    "memory_guard_max_mcp_process_count": 12,
                    "memory_guard_min_free_percent": 20,
                    "memory_guard_max_swap_used_mb": 896,
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
            "memory_guard_high_free_recovery_percent": 40,
            "memory_guard_high_free_recovery_codex_rss_mb": 2700,
            "memory_guard_high_free_recovery_renderer_count": 5,
            "memory_guard_high_free_recovery_helper_rss_mb": 512,
        }

        reasons = resource_guard.assess(
            sample,
            config,
            mode="efficiency",
        )

        self.assertEqual(reasons, [])

    def test_high_free_policy_does_not_recover_below_threshold(self) -> None:
        sample = resource_guard.ResourceSample(
            codex_rss_mb=2256,
            renderer_count=4,
            node_repl_count=13,
            mcp_process_count=11,
            free_percent=39,
            swap_used_mb=971,
            node_repl_rss_mb=84,
            mcp_process_rss_mb=68,
        )
        config = {
            "memory_guard_profiles": {
                "efficiency": {
                    "memory_guard_max_codex_rss_mb": 2200,
                    "memory_guard_max_renderer_count": 5,
                    "memory_guard_max_node_repl_count": 6,
                    "memory_guard_max_mcp_process_count": 12,
                    "memory_guard_min_free_percent": 20,
                    "memory_guard_max_swap_used_mb": 896,
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
            "memory_guard_high_free_recovery_percent": 40,
            "memory_guard_high_free_recovery_codex_rss_mb": 2700,
            "memory_guard_high_free_recovery_renderer_count": 5,
            "memory_guard_high_free_recovery_helper_rss_mb": 512,
        }

        reasons = resource_guard.assess(
            sample,
            config,
            mode="efficiency",
        )

        self.assertTrue(any("codex_rss_mb" in reason for reason in reasons))
        self.assertTrue(any("node_repl_count" in reason for reason in reasons))
        self.assertTrue(any("swap_used_mb" in reason for reason in reasons))

    def test_idle_mode_needs_safe_free_memory_for_performance(self) -> None:
        sample = resource_guard.ResourceSample(
            codex_rss_mb=1000,
            renderer_count=3,
            node_repl_count=3,
            mcp_process_count=6,
            free_percent=29,
            user_idle_seconds=1800,
            on_ac_power=True,
            swap_used_mb=4000,
        )
        config = {
            "resource_mode": "auto",
            "memory_guard_profiles": {"balanced": {}},
            "resource_mode_pressure_free_percent": 20,
            "resource_mode_pressure_codex_rss_mb": 2250,
            "resource_mode_pressure_swap_used_mb": 768,
            "resource_mode_idle_seconds": 900,
            "resource_mode_performance_min_free_percent": 35,
            "memory_guard_swap_recovery_free_percent": 25,
            "memory_guard_swap_recovery_codex_rss_mb": 2000,
        }

        self.assertEqual(
            resource_guard.select_mode(sample, config),
            "balanced",
        )

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
