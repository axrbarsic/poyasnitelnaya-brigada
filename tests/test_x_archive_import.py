from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import x_archive_import as archive_import
import xmention_watcher as watcher


class XArchiveImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
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
                }
            ),
            encoding="utf-8",
        )
        self.config = watcher.load_config(config_path)
        self.connection = watcher.connect_database(self.config.database)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    @staticmethod
    def js_assignment(name: str, value: object) -> str:
        return (
            f"window.YTD.{name}.part0 = "
            + json.dumps(value, ensure_ascii=False)
            + "\n"
        )

    def archive_members(
        self,
        *,
        user_id: str = "900",
        text: str = "Старый точный ответ Alex",
    ) -> dict[str, str]:
        return {
            "data/account.js": self.js_assignment(
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
            "data/tweets.js": self.js_assignment(
                "tweets",
                [
                    {
                        "tweet": {
                            "id_str": "7000000000000000001",
                            "created_at": "Thu Jan 02 03:04:05 +0000 2020",
                            "full_text": text,
                            "in_reply_to_status_id_str": "6999999999999999999",
                            "in_reply_to_user_id_str": "901",
                            "in_reply_to_screen_name": "target_user",
                        }
                    },
                    {
                        "tweet": {
                            "id_str": "7000000000000000002",
                            "created_at": "Fri Jan 03 04:05:06 +0000 2020",
                            "full_text": "Старый самостоятельный пост",
                        }
                    },
                ],
            ),
            "data/direct-messages.js": self.js_assignment(
                "direct_messages",
                [{"dmConversation": {"messages": [{"messageCreate": "private"}]}}],
            ),
        }

    def write_directory_archive(self, members: dict[str, str]) -> Path:
        archive = self.root / "archive"
        for name, content in members.items():
            target = archive / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return archive

    def write_zip_archive(self, members: dict[str, str]) -> Path:
        archive = self.root / "archive.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for name, content in members.items():
                bundle.writestr(name, content)
        return archive

    def insert_current_event(self) -> None:
        payload = {
            "id": "8000000000000000001",
            "author_id": "901",
            "username": "target_user",
            "text": "Новая реплика",
            "created_at": "2026-07-25T12:00:00Z",
            "conversation_id": "8000000000000000000",
            "in_reply_to_user_id": "900",
        }
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO events(
                    event_id, author_id, username, created_at,
                    conversation_id, in_reply_to_user_id, is_reply,
                    payload_json, first_seen_at, delivery_state
                ) VALUES(
                    '8000000000000000001', '901', 'target_user',
                    '2026-07-25T12:00:00Z', '8000000000000000000',
                    '900', 1, ?, ?, 'queued'
                )
                """,
                (json.dumps(payload), watcher.isoformat()),
            )

    def test_plan_accepts_zip_and_ignores_direct_messages(self) -> None:
        archive = self.write_zip_archive(self.archive_members())

        plan = archive_import.plan_archive_import(
            archive,
            expected_user_id="900",
        )

        self.assertEqual(plan.account_user_id, "900")
        self.assertEqual(plan.username, "axrbarsic")
        self.assertEqual(len(plan.posts), 2)
        self.assertEqual(
            plan.posts[0].canonical_url,
            "https://x.com/i/web/status/7000000000000000001",
        )
        self.assertEqual(
            list(plan.private_members_ignored),
            ["data/direct-messages.js"],
        )
        self.assertEqual(plan.summary()["direct_messages_imported"], 0)

    def test_archive_and_report_must_be_outside_project_root(self) -> None:
        archive = self.write_zip_archive(self.archive_members())
        report = self.root / "report.json"

        with self.assertRaisesRegex(
            ValueError,
            "archive must be stored outside",
        ):
            archive_import.require_outside_project(
                archive,
                project_root=self.root,
                label="archive",
            )
        with self.assertRaisesRegex(
            ValueError,
            "report must be stored outside",
        ):
            archive_import.require_outside_project(
                report,
                project_root=self.root,
                label="report",
            )

        external = self.root.parent / "archive-vault" / "archive.zip"
        self.assertEqual(
            archive_import.require_outside_project(
                external,
                project_root=self.root,
                label="archive",
            ),
            external.resolve(),
        )

    def test_plan_rejects_archive_for_other_account(self) -> None:
        archive = self.write_directory_archive(
            self.archive_members(user_id="999")
        )

        with self.assertRaisesRegex(ValueError, "does not match configured"):
            archive_import.plan_archive_import(
                archive,
                expected_user_id="900",
            )

    def test_apply_is_idempotent_and_enriches_commenter_memory(self) -> None:
        archive = self.write_directory_archive(self.archive_members())
        plan = archive_import.plan_archive_import(
            archive,
            expected_user_id="900",
        )

        first = archive_import.apply_archive_import(self.connection, plan)
        second = archive_import.apply_archive_import(self.connection, plan)
        self.insert_current_event()
        memory = watcher.commenter_history_for_event(
            self.connection,
            "8000000000000000001",
            limit=12,
        )

        self.assertEqual(first["status"], "imported")
        self.assertEqual(first["inserted_post_count"], 2)
        self.assertEqual(second["status"], "already_imported")
        self.assertEqual(
            archive_import.archive_status(self.connection)["post_count"],
            2,
        )
        self.assertEqual(memory["total_prior_archive_alex_replies"], 1)
        self.assertEqual(memory["returned_archive_alex_replies"], 1)
        self.assertEqual(
            memory["archive_alex_replies"][0]["text"],
            "Старый точный ответ Alex",
        )
        self.assertEqual(
            memory["archive_alex_replies"][0]["source_kind"],
            "official_x_archive_alex_reply",
        )

    def test_new_archive_rejects_conflicting_existing_post(self) -> None:
        first_archive = self.write_directory_archive(self.archive_members())
        first_plan = archive_import.plan_archive_import(
            first_archive,
            expected_user_id="900",
        )
        archive_import.apply_archive_import(self.connection, first_plan)
        changed_zip = self.write_zip_archive(
            self.archive_members(text="Переписанный текст")
        )
        changed_plan = archive_import.plan_archive_import(
            changed_zip,
            expected_user_id="900",
        )

        with self.assertRaisesRegex(ValueError, "Append-only archive conflict"):
            archive_import.apply_archive_import(
                self.connection,
                changed_plan,
            )


if __name__ == "__main__":
    unittest.main()
