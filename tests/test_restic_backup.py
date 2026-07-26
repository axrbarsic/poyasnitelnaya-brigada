from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

import evidence_import
import memory_snapshot
import restic_backup
import xmention_watcher as watcher


class ResticBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
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
                    "bearer_token": "DO_NOT_BACK_UP_TOKEN_123456789",
                }
            ),
            encoding="utf-8",
        )
        self.watcher_config = watcher.load_config(self.config_path)
        connection = watcher.connect_database(self.watcher_config.database)
        try:
            watcher.complete_initial_audit(self.watcher_config, connection)
            memory_snapshot.create_memory_snapshot(
                self.watcher_config,
                connection,
                output_root=self.project / "var/snapshots",
            )
        finally:
            connection.close()

        evidence_source = self.root / "evidence-source"
        evidence_source.mkdir()
        (evidence_source / "history.jsonl").write_text(
            '{"event_id":"123"}\n',
            encoding="utf-8",
        )
        evidence_import.import_evidence(
            source=evidence_source,
            output_root=self.project / "var/evidence/browser-owner",
            label="session-1",
        )
        self.archive_vault = self.root / "archives/x-mention-watcher"
        self.archive_vault.mkdir(parents=True)
        (self.archive_vault / "source.zip").write_bytes(b"archive fixture")
        self.helper = self.project / "var/keychain-helper"
        self.helper.parent.mkdir(parents=True, exist_ok=True)
        self.helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.helper.chmod(self.helper.stat().st_mode | stat.S_IXUSR)
        self.state_root = self.project / "var/backup-state"
        self.settings = restic_backup.BackupSettings(
            project_root=self.project,
            watcher_config=self.config_path,
            snapshot_root=self.project / "var/snapshots",
            evidence_root=self.project / "var/evidence/browser-owner",
            state_root=self.state_root,
            archive_vault=self.archive_vault,
            restic_binary="restic",
            keychain_helper=self.helper,
            keychain_service="test-service",
            tag="test-x-memory",
            backend="local-test",
            aws_region="",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_plan_contains_only_completed_durable_sources(self) -> None:
        plan = restic_backup.build_backup_plan(self.settings)
        sources = {Path(value) for value in plan["sources"]}

        self.assertTrue(plan["complete"])
        self.assertEqual(len(plan["snapshots"]), 1)
        self.assertEqual(len(plan["evidence"]), 1)
        self.assertTrue(plan["archive_vault"]["present"])
        self.assertIn(self.archive_vault.resolve(), sources)
        self.assertNotIn(
            (self.project / "var/watcher.sqlite3").resolve(),
            sources,
        )
        self.assertTrue(plan["excluded_live_database"])

    def test_plan_settings_do_not_require_installed_restic_or_remote_region(
        self,
    ) -> None:
        settings_path = self.project / "backup.json"
        settings_path.write_text(
            json.dumps(
                {
                    "archive_vault": str(self.archive_vault),
                    "aws_region": "REPLACE_WITH_B2_REGION",
                    "backend": "backblaze-b2-s3",
                    "keychain_service": "test-service",
                    "restic_binary": "definitely-not-installed-restic",
                    "tag": "test-x-memory",
                }
            ),
            encoding="utf-8",
        )

        settings = restic_backup.load_settings(
            settings_path,
            project_root=self.project,
            require_restic=False,
            require_remote=False,
        )

        self.assertEqual(settings.aws_region, "REPLACE_WITH_B2_REGION")
        self.assertTrue(restic_backup.build_backup_plan(settings)["complete"])

    def test_plan_fails_closed_for_corrupted_snapshot(self) -> None:
        snapshot = next((self.project / "var/snapshots").iterdir())
        (snapshot / "conversation-history.jsonl").write_text(
            "corrupted\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Invalid memory snapshot"):
            restic_backup.build_backup_plan(self.settings)

    def test_configured_missing_archive_vault_fails_closed(self) -> None:
        missing = dataclasses_replace(
            self.settings,
            archive_vault=self.root / "missing-archive-vault",
        )
        with self.assertRaisesRegex(ValueError, "archive_vault is missing"):
            restic_backup.build_backup_plan(missing)

    def test_repository_validation_is_backblaze_region_bound(self) -> None:
        production = dataclasses_replace(
            self.settings,
            backend="backblaze-b2-s3",
            aws_region="us-east-005",
        )
        restic_backup.validate_repository(
            production,
            "s3:https://s3.us-east-005.backblazeb2.com/bucket/x-memory",
        )
        with self.assertRaisesRegex(ValueError, "region"):
            restic_backup.validate_repository(
                production,
                "s3:https://s3.us-west-004.backblazeb2.com/bucket/x-memory",
            )
        with self.assertRaisesRegex(ValueError, "Invalid"):
            restic_backup.validate_repository(
                production,
                "s3:https://user:secret@s3.us-east-005.backblazeb2.com/bucket",
            )
        with self.assertRaisesRegex(ValueError, "dedicated prefix"):
            restic_backup.validate_repository(
                production,
                "s3:https://s3.us-east-005.backblazeb2.com/bucket",
            )

    def test_restic_command_keeps_secrets_out_of_arguments_and_output(self) -> None:
        fake = self.root / "fake-restic"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "print(json.dumps({\n"
            "  'argv': sys.argv[1:],\n"
            "  'has_repository': bool(os.environ.get('RESTIC_REPOSITORY')),\n"
            "  'has_password': bool(os.environ.get('RESTIC_PASSWORD')),\n"
            "}))\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        settings = dataclasses_replace(
            self.settings,
            restic_binary=str(fake),
        )
        credentials = restic_backup.ResticCredentials(
            repository=str(self.root / "repo"),
            password="TOP_SECRET_RESTIC_PASSWORD",
        )

        result = restic_backup.run_restic(
            settings,
            credentials,
            ["snapshots"],
        )
        payload = json.loads(result.stdout)

        self.assertEqual(payload["argv"], ["snapshots"])
        self.assertTrue(payload["has_repository"])
        self.assertTrue(payload["has_password"])
        self.assertNotIn("TOP_SECRET_RESTIC_PASSWORD", result.stdout)
        self.assertNotIn(str(self.root / "repo"), payload["argv"])

    def test_failure_state_is_secret_free_and_atomic(self) -> None:
        credentials = restic_backup.ResticCredentials(
            repository=str(self.root / "repo"),
            password="MUST_NOT_APPEAR_IN_STATE",
        )
        error = restic_backup.ResticCommandError(
            restic_backup._redact(
                "failed with MUST_NOT_APPEAR_IN_STATE",
                credentials.secret_values(),
            )
        )

        restic_backup.record_failure(
            self.settings,
            action="backup",
            error=error,
        )

        state = (self.state_root / "latest.json").read_text(encoding="utf-8")
        self.assertIn("[REDACTED]", state)
        self.assertNotIn("MUST_NOT_APPEAR_IN_STATE", state)

    def test_restore_space_margin_has_fixed_and_proportional_floor(self) -> None:
        gib = 1024 * 1024 * 1024
        self.assertEqual(
            restic_backup._required_restore_free_bytes(1),
            512 * 1024 * 1024 + 1,
        )
        self.assertEqual(
            restic_backup._required_restore_free_bytes(10 * gib),
            12 * gib,
        )
        with self.assertRaises(ValueError):
            restic_backup._required_restore_free_bytes(0)

    @unittest.skipUnless(shutil.which("restic"), "restic is not installed")
    def test_real_local_backup_and_restore_smoke(self) -> None:
        repository = self.root / "restic-repository"
        settings = dataclasses_replace(
            self.settings,
            restic_binary=str(Path(shutil.which("restic")).resolve()),
        )
        credentials = restic_backup.ResticCredentials(
            repository=str(repository),
            password="local-integration-only-password",
        )

        restic_backup.initialize_repository(settings, credentials)
        backup = restic_backup.backup_once(
            settings,
            credentials,
            create_snapshot=False,
        )
        restore = restic_backup.restore_smoke(settings, credentials)

        self.assertEqual(backup["status"], "complete")
        self.assertRegex(backup["snapshot_id"], r"^[0-9a-f]{64}$")
        self.assertEqual(restore["status"], "complete")
        self.assertEqual(restore["snapshot_id"], backup["snapshot_id"])
        self.assertEqual(restore["restored_source_count"], 3)
        self.assertGreaterEqual(restore["restored_snapshot_count"], 1)
        self.assertEqual(restore["restored_evidence_count"], 1)
        self.assertTrue(restore["archive_vault_restored"])
        self.assertFalse(restore["restore_kept"])
        durable_restore = restic_backup.load_restore_receipt(settings)
        self.assertEqual(
            durable_restore["snapshot_id"],
            backup["snapshot_id"],
        )


def dataclasses_replace(
    settings: restic_backup.BackupSettings,
    **changes: object,
) -> restic_backup.BackupSettings:
    values = {
        field.name: getattr(settings, field.name)
        for field in settings.__dataclass_fields__.values()
    }
    values.update(changes)
    return restic_backup.BackupSettings(**values)


if __name__ == "__main__":
    unittest.main()
