from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from scripts import codex_cli_updater


class CodexCliUpdaterTests(unittest.TestCase):
    def make_config(self, root: Path) -> Path:
        config = root / "config.json"
        (root / "var").mkdir()
        (root / "var/wake-request.json").write_text(
            json.dumps({"events": [], "pending_count": 0}),
            encoding="utf-8",
        )
        (root / "var/autopilot-dispatch.json").write_text(
            json.dumps({"version": 1, "events": {}, "owner": None}),
            encoding="utf-8",
        )
        alias = root / "bin/codex"
        config.write_text(
            json.dumps(
                {
                    "wake_file": "var/wake-request.json",
                    "autopilot_state_file": "var/autopilot-dispatch.json",
                    "browser_owner_cwd": ".",
                    "codex_terminal_path": str(alias),
                    "codex_cli_update_state_file":
                        "var/codex-cli-update.json",
                }
            ),
            encoding="utf-8",
        )
        return config

    def test_version_order_handles_preview_and_stable(self) -> None:
        alpha = codex_cli_updater.Version.parse("0.146.0-alpha.3.1")
        later_alpha = codex_cli_updater.Version.parse(
            "rust-v0.146.0-alpha.4"
        )
        stable = codex_cli_updater.Version.parse("0.146.0")
        next_minor = codex_cli_updater.Version.parse("0.147.0-alpha.1")

        self.assertLess(alpha, later_alpha)
        self.assertLess(later_alpha, stable)
        self.assertLess(stable, next_minor)

    def test_preview_selects_highest_semantic_release(self) -> None:
        releases = [
            {"tag_name": "rust-v0.145.0", "draft": False},
            {"tag_name": "rust-v0.146.0-alpha.2", "draft": False},
            {"tag_name": "rust-v0.146.0-alpha.9", "draft": False},
            {"tag_name": "invalid", "draft": False},
            {"tag_name": "rust-v9.0.0", "draft": True},
        ]
        with mock.patch.object(
            codex_cli_updater,
            "github_json",
            return_value=releases,
        ):
            selected = codex_cli_updater.select_release("preview")

        self.assertEqual(
            selected["tag_name"],
            "rust-v0.146.0-alpha.9",
        )

    def test_safe_idle_rejects_pending_x_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            (root / "var/wake-request.json").write_text(
                json.dumps(
                    {
                        "events": [
                            {
                                "id": "123",
                                "url": "https://x.com/a/status/123",
                            }
                        ],
                        "pending_count": 1,
                    }
                ),
                encoding="utf-8",
            )
            idle, reason = codex_cli_updater.safe_idle(config)

        self.assertFalse(idle)
        self.assertEqual(reason, "x_queue_busy")

    def test_current_newer_version_never_downgrades(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            with (
                mock.patch.object(
                    codex_cli_updater,
                    "safe_idle",
                    return_value=(True, "idle"),
                ),
                mock.patch.object(
                    codex_cli_updater,
                    "read_version",
                    return_value=codex_cli_updater.Version.parse(
                        "0.146.0-alpha.3.1"
                    ),
                ),
                mock.patch.object(
                    codex_cli_updater,
                    "select_release",
                    return_value={
                        "tag_name": "rust-v0.145.0",
                        "html_url": "https://example.test/release",
                    },
                ),
                mock.patch.object(
                    codex_cli_updater,
                    "select_asset",
                    side_effect=AssertionError("must not download"),
                ),
            ):
                result = codex_cli_updater.run(config)

        self.assertEqual(result["status"], "current")
        self.assertFalse(result["updated"])
        self.assertEqual(
            result["current_version"],
            "0.146.0-alpha.3.1",
        )

    def test_digest_verification_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.tar.gz"
            path.write_bytes(b"payload")
            digest = hashlib.sha256(b"other").hexdigest()
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                codex_cli_updater.verify_digest(path, "sha256:" + digest)

    def test_doctor_uses_real_terminal_type_for_launchagent(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout="healthy",
            stderr="",
        )
        with mock.patch.object(
            codex_cli_updater.subprocess,
            "run",
            return_value=completed,
        ) as run:
            result = codex_cli_updater.doctor(Path("/tmp/codex"))

        self.assertEqual(result, "healthy")
        self.assertEqual(
            run.call_args.kwargs["env"]["TERM"],
            "xterm-256color",
        )

    def test_github_history_is_published_only_when_configured(self) -> None:
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        config = {
            "codex_cli_update_github_issue": 12,
            "codex_cli_update_github_repo": "owner/repository",
            "github_cli_path": "/tmp/gh",
        }
        with mock.patch.object(
            codex_cli_updater.subprocess,
            "run",
            return_value=completed,
        ) as run:
            published = codex_cli_updater.publish_github_history(
                config,
                previous=codex_cli_updater.Version.parse("0.1.0"),
                current=codex_cli_updater.Version.parse("0.2.0"),
                release_url="https://example.test/release",
                summary="Исправления.",
            )

        self.assertTrue(published)
        self.assertEqual(
            run.call_args.args[0][:6],
            [
                "/tmp/gh",
                "issue",
                "comment",
                "12",
                "--repo",
                "owner/repository",
            ],
        )

    def test_github_history_failure_is_nonfatal(self) -> None:
        config = {
            "codex_cli_update_github_issue": 12,
            "github_cli_path": "/missing/gh",
        }
        with mock.patch.object(
            codex_cli_updater.subprocess,
            "run",
            side_effect=OSError("missing"),
        ):
            published = codex_cli_updater.publish_github_history(
                config,
                previous=codex_cli_updater.Version.parse("0.1.0"),
                current=codex_cli_updater.Version.parse("0.2.0"),
                release_url="https://example.test/release",
                summary="Исправления.",
            )

        self.assertFalse(published)

    def test_summary_falls_back_when_compare_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(
                    codex_cli_updater,
                    "github_json",
                    side_effect=urllib.error.URLError("offline"),
                ),
                mock.patch.object(
                    codex_cli_updater.subprocess,
                    "run",
                    side_effect=AssertionError("model must not run"),
                ),
            ):
                summary = codex_cli_updater.russian_summary(
                    Path("/tmp/codex"),
                    {
                        "body": "",
                        "tag_name": "rust-v0.2.0",
                    },
                    Path(directory),
                    previous=codex_cli_updater.Version.parse("0.1.0"),
                )

        self.assertIn("официальной истории релиза", summary)

    def test_extract_rejects_archive_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "codex.tar.gz"
            payload = b"binary"
            with tarfile.open(archive, "w:gz") as bundle:
                member = tarfile.TarInfo("../codex")
                member.size = len(payload)
                bundle.addfile(member, io.BytesIO(payload))
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                codex_cli_updater.extract_codex(
                    archive,
                    root / "codex",
                )

    def test_atomic_alias_can_be_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old"
            new = root / "new"
            old.write_text("old", encoding="utf-8")
            new.write_text("new", encoding="utf-8")
            alias = root / "codex"
            alias.symlink_to(old)

            previous = codex_cli_updater.atomic_alias(alias, new)
            self.assertEqual(alias.resolve(), new.resolve())
            codex_cli_updater.restore_alias(alias, previous)

            self.assertEqual(alias.resolve(), old.resolve())


if __name__ == "__main__":
    unittest.main()
