from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import archive_intake
import xmention_watcher as watcher


class ArchiveIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.config_path = self.project / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "user_id": "900",
                    "database": "var/watcher.sqlite3",
                    "health_file": "var/health.json",
                    "wake_file": "var/wake-request.json",
                    "alert_file": "var/watchdog-alert.json",
                    "notifications_enabled": False,
                }
            ),
            encoding="utf-8",
        )
        config = watcher.load_config(self.config_path)
        connection = watcher.connect_database(config.database)
        watcher.complete_initial_audit(config, connection)
        connection.close()
        self.vault = self.base / "archive-vault" / "2026-07-25"
        self.vault.mkdir(parents=True)
        self.archive = self.vault / "source.zip"
        self.write_archive(text="Точный старый ответ")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def assignment(name: str, value: object) -> str:
        return (
            f"window.YTD.{name}.part0 = "
            + json.dumps(value, ensure_ascii=False)
            + "\n"
        )

    def write_archive(
        self,
        *,
        user_id: str = "900",
        text: str,
    ) -> None:
        with zipfile.ZipFile(self.archive, "w") as bundle:
            bundle.writestr(
                "data/account.js",
                self.assignment(
                    "account",
                    [
                        {
                            "account": {
                                "accountId": user_id,
                                "username": "axrbarsic",
                            }
                        }
                    ],
                ),
            )
            bundle.writestr(
                "data/tweets.js",
                self.assignment(
                    "tweets",
                    [
                        {
                            "tweet": {
                                "id_str": "7000000000000000001",
                                "created_at": (
                                    "Thu Jan 02 03:04:05 +0000 2020"
                                ),
                                "full_text": text,
                                "in_reply_to_status_id_str": (
                                    "6999999999999999999"
                                ),
                                "in_reply_to_user_id_str": "901",
                                "in_reply_to_screen_name": "target_user",
                            }
                        }
                    ],
                ),
            )
            bundle.writestr(
                "data/direct-messages.js",
                self.assignment(
                    "direct_messages",
                    [{"dmConversation": {"messages": ["private"]}}],
                ),
            )

    def test_dry_run_writes_external_plan_without_database_mutation(self) -> None:
        result, plan_path = archive_intake.build_intake_plan(
            self.config_path,
            self.archive,
        )
        config = watcher.load_config(self.config_path)
        connection = watcher.connect_database(config.database)
        try:
            import_count = connection.execute(
                "SELECT COUNT(*) FROM archive_imports"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(result["import_plan"]["account_user_id"], "900")
        self.assertEqual(
            result["import_plan"]["private_members_ignored"],
            ["data/direct-messages.js"],
        )
        self.assertEqual(result["import_plan"]["direct_messages_imported"], 0)
        self.assertTrue(plan_path.is_file())
        self.assertFalse(plan_path.is_relative_to(self.project))
        self.assertEqual(import_count, 0)

    def test_apply_requires_matching_dry_run_plan(self) -> None:
        reports = self.vault / "reports"
        with self.assertRaisesRegex(ValueError, "Dry-run intake plan is missing"):
            archive_intake.apply_intake(
                self.config_path,
                self.archive,
                report_directory=reports,
            )

    def test_apply_rejects_archive_changed_after_dry_run(self) -> None:
        archive_intake.build_intake_plan(self.config_path, self.archive)
        self.write_archive(text="Измененный после проверки ответ")

        with self.assertRaisesRegex(ValueError, "changed after dry-run"):
            archive_intake.apply_intake(
                self.config_path,
                self.archive,
            )

    def test_apply_imports_audits_and_snapshots_once(self) -> None:
        archive_intake.build_intake_plan(self.config_path, self.archive)

        first = archive_intake.apply_intake(
            self.config_path,
            self.archive,
        )
        snapshot_root = self.project / "var" / "snapshots"
        first_snapshots = sorted(snapshot_root.iterdir())
        second = archive_intake.apply_intake(
            self.config_path,
            self.archive,
        )
        second_snapshots = sorted(snapshot_root.iterdir())

        self.assertEqual(first["status"], "applied")
        self.assertTrue(first["memory_audit"]["final_complete"])
        self.assertIsNotNone(first["snapshots"]["before"])
        self.assertIsNotNone(first["snapshots"]["after"])
        self.assertEqual(len(first_snapshots), 2)
        self.assertEqual(second["status"], "already_applied")
        self.assertEqual(second["snapshots"], first["snapshots"])
        self.assertEqual(second_snapshots, first_snapshots)
        self.assertTrue(
            (self.vault / "reports" / "intake-receipt.json").is_file()
        )

    def test_existing_import_without_receipt_gets_recovery_snapshot(self) -> None:
        archive_intake.build_intake_plan(self.config_path, self.archive)
        first = archive_intake.apply_intake(
            self.config_path,
            self.archive,
        )
        receipt = Path(first["receipt"])
        receipt.unlink()
        snapshot_root = self.project / "var" / "snapshots"
        before_recovery = sorted(snapshot_root.iterdir())

        recovered = archive_intake.apply_intake(
            self.config_path,
            self.archive,
        )
        after_recovery = sorted(snapshot_root.iterdir())

        self.assertEqual(recovered["status"], "recovered_existing_import")
        self.assertIsNone(recovered["snapshots"]["before"])
        self.assertIsNotNone(recovered["snapshots"]["after"])
        self.assertEqual(len(after_recovery), len(before_recovery) + 1)
        self.assertTrue(receipt.is_file())

    def test_rejects_wrong_account_before_writing_plan(self) -> None:
        self.write_archive(user_id="999", text="Чужой архив")

        with self.assertRaisesRegex(ValueError, "does not match configured"):
            archive_intake.build_intake_plan(
                self.config_path,
                self.archive,
            )

    def test_rejects_archive_and_report_symlinks(self) -> None:
        archive_link = self.base / "archive-link.zip"
        archive_link.symlink_to(self.archive)
        with self.assertRaisesRegex(ValueError, "Archive must not be a symlink"):
            archive_intake.build_intake_plan(
                self.config_path,
                archive_link,
            )

        report_target = self.base / "real-reports"
        report_target.mkdir()
        report_link = self.vault / "report-link"
        report_link.symlink_to(report_target, target_is_directory=True)
        with self.assertRaisesRegex(
            ValueError,
            "Report directory must not be a symlink",
        ):
            archive_intake.build_intake_plan(
                self.config_path,
                self.archive,
                report_directory=report_link,
            )


if __name__ == "__main__":
    unittest.main()
