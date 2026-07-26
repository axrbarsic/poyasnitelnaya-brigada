from __future__ import annotations

import unittest

import readiness_audit


def complete_components() -> dict[str, object]:
    fingerprint = "a" * 64
    snapshot_id = "b" * 64
    layout = {"complete": True}
    memory = {
        "current_memory_ok": True,
        "archive_ready": True,
        "final_complete": True,
    }
    commenter_memory = {
        "complete": True,
        "identity_key": "x_user_id",
        "events_checked": 10,
        "lookup_failure_count": 0,
    }
    watcher_health = {"healthy": True, "status": "healthy"}
    backup_plan = {
        "complete": True,
        "plan_fingerprint": fingerprint,
        "source_count": 4,
        "snapshots": [{"snapshot": "/snapshot"}],
        "evidence": [{"path": "/evidence"}],
        "archive_vault": {"present": True},
    }
    snapshot = {"complete": True, "current": True, "errors": []}
    backup_receipt = {
        "snapshot_id": snapshot_id,
        "plan": {"plan_fingerprint": fingerprint},
    }
    restore_receipt = {
        "snapshot_id": snapshot_id,
        "plan_fingerprint": fingerprint,
        "restored_source_count": 4,
    }
    remote_receipts = readiness_audit.audit_remote_receipts(
        plan=backup_plan,
        backup_receipt=backup_receipt,
        restore_receipt=restore_receipt,
    )
    return {
        "layout": layout,
        "memory": memory,
        "commenter_memory": commenter_memory,
        "watcher_health": watcher_health,
        "backup_plan": backup_plan,
        "snapshot": snapshot,
        "remote_configured": True,
        "remote_configuration_error": None,
        "remote_receipts": remote_receipts,
    }


class ReadinessAuditTests(unittest.TestCase):
    def test_complete_requires_matching_backup_and_restore(self) -> None:
        result = readiness_audit.aggregate_readiness(**complete_components())

        self.assertTrue(result["complete"])
        self.assertEqual(result["blockers"], [])

    def test_archive_and_remote_pending_fail_closed(self) -> None:
        components = complete_components()
        components["memory"] = {
            "current_memory_ok": True,
            "archive_ready": False,
            "final_complete": False,
        }
        components["remote_configured"] = False
        components["remote_configuration_error"] = "region missing"
        components["remote_receipts"] = readiness_audit.audit_remote_receipts(
            plan=components["backup_plan"],
            backup_receipt=None,
            restore_receipt=None,
        )

        result = readiness_audit.aggregate_readiness(**components)

        self.assertFalse(result["complete"])
        self.assertIn("official_x_archive_pending", result["blockers"])
        self.assertIn("remote_backup_not_configured", result["blockers"])
        self.assertIn("remote_backup_receipt_missing", result["blockers"])
        self.assertIn("restore_smoke_receipt_missing", result["blockers"])

    def test_stale_backup_or_restore_is_rejected(self) -> None:
        components = complete_components()
        fingerprint = str(
            components["backup_plan"]["plan_fingerprint"]
        )
        components["remote_receipts"] = readiness_audit.audit_remote_receipts(
            plan=components["backup_plan"],
            backup_receipt={
                "snapshot_id": "b" * 64,
                "plan": {"plan_fingerprint": "c" * 64},
            },
            restore_receipt={
                "snapshot_id": "d" * 64,
                "plan_fingerprint": fingerprint,
                "restored_source_count": 4,
            },
        )

        result = readiness_audit.aggregate_readiness(**components)

        self.assertFalse(result["complete"])
        self.assertIn(
            "remote_backup_plan_is_not_current",
            result["blockers"],
        )
        self.assertIn(
            "restore_smoke_snapshot_mismatch",
            result["blockers"],
        )

    def test_active_response_queue_is_not_mislabeled_as_corruption(self) -> None:
        components = complete_components()
        components["memory"] = {
            "current_memory_ok": False,
            "archive_ready": False,
            "errors": ["initial_audit_incomplete"],
            "history": {"initial_audit": {"pending_events": 1}},
        }

        result = readiness_audit.aggregate_readiness(**components)

        self.assertIn("response_queue_pending", result["blockers"])
        self.assertNotIn("current_memory_invalid", result["blockers"])

    def test_commenter_memory_failure_blocks_completion(self) -> None:
        components = complete_components()
        components["commenter_memory"] = {
            "complete": False,
            "errors": ["stable_author_lookup_failed"],
        }

        result = readiness_audit.aggregate_readiness(**components)

        self.assertIn(
            "commenter_memory_lookup_invalid",
            result["blockers"],
        )

    def test_latest_snapshot_must_match_current_memory(self) -> None:
        memory = {"current_memory_ok": True, "archive_ready": False}
        result = readiness_audit.audit_latest_snapshot(
            {
                "snapshots": [
                    {
                        "snapshot": "/snapshot",
                        "created_at": "2026-07-25T00:00:00Z",
                        "memory_audit": {"different": True},
                    }
                ]
            },
            memory,
        )

        self.assertFalse(result["complete"])
        self.assertIn(
            "latest_snapshot_does_not_match_current_memory",
            result["errors"],
        )


if __name__ == "__main__":
    unittest.main()
