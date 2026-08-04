from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import (
    autopilot_bridge,
    deleted_publication_replacement,
    watcher_database,
    watcher_resolution,
)


EVENT_ID = "2084427581441376514"
OLD_STATUS_ID = "2084434043760775263"
NEW_STATUS_ID = "2084439999999999999"
CHAIN_ID = "2082871616745120158"
OLD_URL = f"https://x.com/axrbarsic/status/{OLD_STATUS_ID}"
NEW_URL = f"https://x.com/axrbarsic/status/{NEW_STATUS_ID}"


class DeletedPublicationReplacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.connection = watcher_database.connect_database(root / "watcher.sqlite3")
        self.config = SimpleNamespace(
            api_base="https://api.x.com/2",
            request_timeout_seconds=30,
            mandatory_response_mode=True,
        )
        self._insert_published_event()

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def _insert_published_event(self) -> None:
        self.connection.execute(
            """
            INSERT INTO events(
                event_id, author_id, username, created_at, conversation_id,
                in_reply_to_user_id, is_reply, payload_json, first_seen_at,
                delivery_state
            ) VALUES(?, 'author', '_vielspas', ?, ?, 'alex', 1, ?, ?, 'acknowledged')
            """,
            (
                EVENT_ID,
                "2026-08-03T23:53:00Z",
                CHAIN_ID,
                '{"referenced_tweets":[{"type":"replied_to","id":"2084424982659027125"}]}',
                "2026-08-03T23:54:00Z",
            ),
        )
        self.connection.execute(
            """
            INSERT INTO conversation_chains(
                chain_id, root_status_id, provenance, created_at, updated_at
            ) VALUES(?, ?, 'short', ?, ?)
            """,
            (CHAIN_ID, CHAIN_ID, "2026-08-03T00:00:00Z", "2026-08-04T00:00:00Z"),
        )
        for status_id, parent_id, actor, url, text in (
            (
                EVENT_ID,
                "2084424982659027125",
                "commenter",
                f"https://x.com/_vielspas/status/{EVENT_ID}",
                "А или Б?",
            ),
            (OLD_STATUS_ID, EVENT_ID, "alex", OLD_URL, "deleted reply"),
        ):
            self.connection.execute(
                """
                INSERT INTO conversation_turns(
                    status_id, chain_id, parent_status_id, actor, author, url,
                    exact_text, media_json, posted_at, observed_at, provenance
                ) VALUES(?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, 'test')
                """,
                (
                    status_id,
                    CHAIN_ID,
                    parent_id,
                    actor,
                    actor,
                    url,
                    text,
                    "2026-08-04T00:00:00Z",
                    "2026-08-04T00:00:00Z",
                ),
            )
        self.connection.execute(
            """
            INSERT INTO event_resolutions(
                event_id, disposition, reason, reply_url, evidence_json,
                resolved_at
            ) VALUES(?, 'published', 'verified', ?, '[]', ?)
            """,
            (EVENT_ID, OLD_URL, "2026-08-04T00:01:00Z"),
        )
        self.connection.commit()

    def _dependencies(self, response: dict) -> deleted_publication_replacement.Dependencies:
        def refresh(_config, connection: sqlite3.Connection) -> dict:
            count = connection.execute(
                "SELECT COUNT(*) FROM events WHERE delivery_state = 'queued'"
            ).fetchone()[0]
            return {"pending_count": count}

        return deleted_publication_replacement.Dependencies(
            bearer_token=lambda _config: "secret",
            request_json=lambda _url, _token, _timeout: response,
            refresh_wake_file=refresh,
            write_health=lambda *_args, **_kwargs: {"status": "healthy"},
            isoformat=lambda: "2026-08-04T00:02:00Z",
        )

    @staticmethod
    def _not_found_response() -> dict:
        return {
            "errors": [
                {
                    "title": "Not Found Error",
                    "detail": f"Could not find post with id: [{OLD_STATUS_ID}].",
                    "type": deleted_publication_replacement.NOT_FOUND_TYPE,
                    "resource_type": "tweet",
                    "parameter": "id",
                    "value": OLD_STATUS_ID,
                }
            ]
        }

    def test_prepare_requires_exact_not_found_proof(self) -> None:
        with self.assertRaisesRegex(ValueError, "still exists"):
            deleted_publication_replacement.prepare_deleted_publication_replacement(
                self.config,
                self.connection,
                EVENT_ID,
                reason="Alex deleted the reply",
                dependencies=self._dependencies({"data": {"id": OLD_STATUS_ID}}),
            )
        with self.assertRaisesRegex(ValueError, "not exact resource-not-found"):
            deleted_publication_replacement.prepare_deleted_publication_replacement(
                self.config,
                self.connection,
                EVENT_ID,
                reason="Alex deleted the reply",
                dependencies=self._dependencies(
                    {"errors": [{"title": "Unauthorized"}]}
                ),
            )

    def test_prepare_is_idempotent_and_preserves_old_resolution(self) -> None:
        dependencies = self._dependencies(self._not_found_response())
        first = deleted_publication_replacement.prepare_deleted_publication_replacement(
            self.config,
            self.connection,
            EVENT_ID,
            reason="Alex deleted the reply and requested a corrected replacement",
            dependencies=dependencies,
        )
        second = deleted_publication_replacement.prepare_deleted_publication_replacement(
            self.config,
            self.connection,
            EVENT_ID,
            reason="Alex deleted the reply and requested a corrected replacement",
            dependencies=dependencies,
        )
        resolution = self.connection.execute(
            "SELECT disposition, reply_url FROM event_resolutions WHERE event_id = ?",
            (EVENT_ID,),
        ).fetchone()
        requeue_count = self.connection.execute(
            "SELECT COUNT(*) FROM response_policy_requeues WHERE event_id = ?",
            (EVENT_ID,),
        ).fetchone()[0]
        self.assertTrue(first["prepared"])
        self.assertFalse(second["prepared"])
        self.assertEqual(resolution["disposition"], "published")
        self.assertEqual(resolution["reply_url"], OLD_URL)
        self.assertEqual(requeue_count, 1)
        recovery = autopilot_bridge._resolution_recovery(
            self.connection,
            EVENT_ID,
        )
        self.assertTrue(recovery["replacement_authorized"])
        self.assertEqual(recovery["old_reply_url"], OLD_URL)
        self.assertEqual(recovery["old_status_id"], OLD_STATUS_ID)

    def test_published_revision_requires_and_consumes_authorization(self) -> None:
        dependencies = watcher_resolution.Dependencies(
            refresh_wake_file=lambda *_args: {"pending_count": 0},
            write_health=lambda *_args, **_kwargs: {"status": "healthy"},
        )
        self.connection.execute(
            """
            INSERT INTO conversation_turns(
                status_id, chain_id, parent_status_id, actor, author, url,
                exact_text, media_json, posted_at, observed_at, provenance
            ) VALUES(?, ?, ?, 'alex', 'alex', ?, 'replacement', '[]', ?, ?, 'test')
            """,
            (
                NEW_STATUS_ID,
                CHAIN_ID,
                EVENT_ID,
                NEW_URL,
                "2026-08-04T00:03:00Z",
                "2026-08-04T00:03:00Z",
            ),
        )
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "lacks active deletion authorization"):
            watcher_resolution.revise_event_resolution(
                self.config,
                self.connection,
                EVENT_ID,
                disposition="published",
                reason="corrected",
                reply_url=NEW_URL,
                revision_reason="replace deleted reply",
                dependencies=dependencies,
            )
        deleted_publication_replacement.prepare_deleted_publication_replacement(
            self.config,
            self.connection,
            EVENT_ID,
            reason="Alex deleted the reply",
            dependencies=self._dependencies(self._not_found_response()),
        )
        result = watcher_resolution.revise_event_resolution(
            self.config,
            self.connection,
            EVENT_ID,
            disposition="published",
            reason="corrected",
            reply_url=NEW_URL,
            revision_reason="replace deleted reply",
            dependencies=dependencies,
        )
        self.assertTrue(result["revised"])
        with self.assertRaisesRegex(ValueError, "lacks active deletion authorization"):
            watcher_resolution.revise_event_resolution(
                self.config,
                self.connection,
                EVENT_ID,
                disposition="published",
                reason="another",
                reply_url="https://x.com/axrbarsic/status/2084440000000000000",
                revision_reason="unauthorized second replacement",
                dependencies=dependencies,
            )


if __name__ == "__main__":
    unittest.main()
