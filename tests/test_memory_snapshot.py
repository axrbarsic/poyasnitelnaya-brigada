from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import memory_snapshot
import xmention_watcher as watcher


class MemorySnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.secret_sentinel = "DO_NOT_PERSIST_TEST_SECRET_8f97f0"
        config_path = self.root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "user_id": "900",
                    "database": "var/watcher.sqlite3",
                    "health_file": "var/health.json",
                    "wake_file": "var/wake-request.json",
                    "alert_file": "var/watchdog-alert.json",
                    "notifications_enabled": False,
                    "bearer_token": self.secret_sentinel,
                }
            ),
            encoding="utf-8",
        )
        self.config = watcher.load_config(config_path)
        self.connection = watcher.connect_database(self.config.database)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_snapshot_is_consistent_self_verifying_and_secret_free(self) -> None:
        watcher.complete_initial_audit(self.config, self.connection)
        result = memory_snapshot.create_memory_snapshot(
            self.config,
            self.connection,
            output_root=self.root / "var" / "snapshots",
        )
        snapshot = Path(result["snapshot"])
        manifest = json.loads(
            (snapshot / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["configured_user_id"], self.config.user_id)
        self.assertTrue(manifest["memory_audit"]["current_memory_ok"])
        self.assertEqual(
            manifest["memory_audit"]["status"],
            "archive_pending",
        )
        self.assertNotIn("token", json.dumps(manifest).lower())
        for path in snapshot.rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    self.secret_sentinel.encode("utf-8"),
                    path.read_bytes(),
                    msg=f"secret sentinel leaked into {path.name}",
                )
        for name, metadata in manifest["files"].items():
            path = snapshot / name
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_size, metadata["bytes"])
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(),
                metadata["sha256"],
            )
        copied = watcher.connect_database(snapshot / "watcher.sqlite3")
        try:
            self.assertEqual(
                watcher.memory_audit(self.config, copied),
                manifest["memory_audit"],
            )
        finally:
            copied.close()

    def test_snapshot_refuses_invalid_memory(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid memory"):
            memory_snapshot.create_memory_snapshot(
                self.config,
                self.connection,
                output_root=self.root / "var" / "snapshots",
            )
