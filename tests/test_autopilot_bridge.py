from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import xmention_watcher as watcher
import candidate_corpus
import evidence_import
from scripts import autopilot_bridge, autopilot_dispatch
from scripts import resource_guard


class AutopilotBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.owner = self.root
        self.config = self.root / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "user_id": "16337609",
                    "wake_file": "var/wake-request.json",
                    "autopilot_state_file": "var/autopilot-dispatch.json",
                    "autopilot_health_file": "var/autopilot-health.json",
                    "database": "var/watcher.sqlite3",
                    "browser_owner_cwd": ".",
                }
            ),
            encoding="utf-8",
        )
        self.wake = self.root / "var" / "wake-request.json"
        self.write_events([])

    def test_claim_rejects_external_browser_workspace(self) -> None:
        external = self.root / "owner"
        external.mkdir()
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload["browser_owner_cwd"] = str(external)
        self.config.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(
            ValueError,
            "must equal the canonical project root",
        ):
            autopilot_bridge.claim(self.config, lease_seconds=1800)

    def test_claim_requires_provenance_recovery_for_manual_parent(self) -> None:
        self.write_events([self.event()])

        result = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertTrue(result["dispatch"])
        self.assertIn("разделяй происхождение", result["prompt"])
        self.assertIn("хода и способ продолжения", result["prompt"])
        self.assertIn("poyasnitelnaya-brigada-v2", result["prompt"])
        self.assertIn("прямой просьбе Alex применить именно v1", result["prompt"])
        self.assertIn("gpt-5.6-sol", result["prompt"])
        self.assertIn("reasoning_effort=max", result["prompt"])
        self.assertIn(
            "generation_skill=poyasnitelnaya-brigada-v2",
            result["prompt"],
        )
        self.assertIn("одним непустым целостным", result["prompt"])
        self.assertIn("4000 Unicode code points", result["prompt"])
        self.assertIn("--non-empty --max 4000", result["prompt"])
        self.assertIn("scripts/verify_x_note_tweet.py", result["prompt"])
        self.assertIn("valid=true", result["prompt"])
        self.assertIn("MAX_PARALLEL_X_READ_TABS=1", result["prompt"])
        self.assertIn("Не готовь весь пакет целиком", result["prompt"])
        self.assertIn("Никогда не держи заполненный composer", result["prompt"])
        self.assertIn(
            "Запрещено выбирать page-global `tweetButtonInline.first()`",
            result["prompt"],
        )
        self.assertIn(
            "ровно одна клавиатурная активация Enter",
            result["prompt"],
        )
        self.assertIn(
            "Никогда не отправляй shortcut из самого поля",
            result["prompt"],
        )
        self.assertNotIn("Жди готовый ответ до", result["prompt"])

    def test_claim_exposes_auditable_resolution_recovery(self) -> None:
        current = self.event()
        connection = watcher.connect_database(
            self.root / "var" / "watcher.sqlite3"
        )
        payload = {
            "id": current["event_id"],
            "author_id": "901",
            "username": current["username"],
            "text": "Follow-up",
            "created_at": current["created_at"],
            "conversation_id": current["conversation_id"],
            "in_reply_to_user_id": "16337609",
            "referenced_tweets": [
                {
                    "type": "replied_to",
                    "id": "2081050838240211433",
                }
            ],
        }
        with connection:
            connection.execute(
                """
                INSERT INTO events(
                    event_id, author_id, username, created_at,
                    conversation_id, in_reply_to_user_id, is_reply,
                    payload_json, first_seen_at, delivery_state
                ) VALUES(?, '901', ?, ?, ?, '16337609', 1, ?, ?, 'queued')
                """,
                (
                    current["event_id"],
                    current["username"],
                    current["created_at"],
                    current["conversation_id"],
                    json.dumps(payload, sort_keys=True),
                    watcher.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO event_resolutions(
                    event_id, disposition, reason, blocker_code,
                    evidence_json, resolved_at
                ) VALUES(?, 'blocked', 'Legacy model', ?,
                         '["exact history"]', ?)
                """,
                (
                    current["event_id"],
                    "required_pro_model_unavailable",
                    watcher.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO response_policy_requeues(
                    event_id, previous_resolution_json, reason, requeued_at
                ) VALUES(?, '{}', ?, ?)
                """,
                (
                    current["event_id"],
                    "local_poyasnitelnaya_brigada_skill_recovery",
                    watcher.isoformat(),
                ),
            )
        connection.close()
        self.write_events([current])

        result = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertIn('"resolution_recovery"', result["prompt"])
        self.assertIn(
            '"supersedes_existing_resolution":true',
            result["prompt"],
        )
        self.assertIn(
            "The local poyasnitelnaya-brigada skill removed "
            "the legacy ChatGPT dependency",
            result["prompt"],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_events(self, events: list[dict]) -> None:
        self.wake.parent.mkdir(parents=True, exist_ok=True)
        self.wake.write_text(
            json.dumps({"pending_count": len(events), "events": events}),
            encoding="utf-8",
        )

    def write_durable_resolutions(self, event_ids: list[str]) -> None:
        database = self.root / "var" / "watcher.sqlite3"
        connection = watcher.connect_database(database)
        connection.close()
        with closing(sqlite3.connect(database)) as connection, connection:
            for event_id in event_ids:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO event_resolutions(
                        event_id, disposition, reason, evidence_json,
                        resolved_at
                    ) VALUES(?, 'skip', 'verified test resolution', '[]', ?)
                    """,
                    (event_id, watcher.isoformat()),
                )

    def write_claim_manifest(
        self,
        claim_token: str,
        event_ids: list[str],
    ) -> None:
        session_dir = (
            self.root / "var/evidence/browser-owner" / claim_token
        )
        session_dir.mkdir(parents=True)
        (session_dir / "conversation-history.jsonl").write_text(
            "".join(
                json.dumps({"event_id": event_id}) + "\n"
                for event_id in event_ids
            ),
            encoding="utf-8",
        )
        (session_dir / "run-ledger.jsonl").write_text(
            "".join(
                json.dumps(
                    {
                        "event": "initial_audit_disposition",
                        "event_id": event_id,
                    }
                )
                + "\n"
                for event_id in event_ids
            ),
            encoding="utf-8",
        )
        source_manifest = evidence_import.build_file_manifest(
            session_dir,
            evidence_import.collect_source_files(session_dir),
        )
        manifest = {
            "format_version": 1,
            "label": claim_token,
            "source": "runtime_browser_owner",
            "finalized_at": watcher.isoformat(),
            "resolution_proof": [
                {
                    "event_id": event_id,
                    "disposition": "skip",
                    "reply_url": None,
                }
                for event_id in sorted(event_ids, key=int)
            ],
            **source_manifest,
        }
        (session_dir / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def prepare_completion(
        self,
        claim_token: str,
        event_ids: list[str],
    ) -> None:
        self.write_durable_resolutions(event_ids)
        self.write_claim_manifest(claim_token, event_ids)

    def event(self) -> dict:
        return {
            "event_id": "2081050838240211434",
            "event_url":
                "https://x.com/Timyr316661/status/2081050838240211434",
            "username": "Timyr316661",
            "conversation_id": "2080312847230210375",
            "created_at": "2026-07-25T16:15:58Z",
            "first_seen_at": "2026-07-25T16:19:00Z",
            "is_reply": True,
        }

    def test_empty_queue_is_token_free_idle(self) -> None:
        result = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertFalse(result["dispatch"])
        self.assertEqual(result["status"], "idle")
        health = json.loads(
            (self.root / "var" / "autopilot-health.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(health["status"], "idle")

    def test_gate_is_read_only_and_ready_for_pending_event(self) -> None:
        self.write_events([self.event()])

        result = autopilot_bridge.gate(self.config, lease_seconds=1800)

        self.assertTrue(result["dispatch"])
        self.assertEqual(result["status"], "ready")
        self.assertFalse(
            (self.root / "var" / "autopilot-dispatch.json").exists()
        )

    def test_gate_and_claim_default_to_one_oldest_event(self) -> None:
        events = []
        for offset in range(5):
            event = dict(self.event())
            event_id = str(2081050838240211434 + offset)
            event["event_id"] = event_id
            event["event_url"] = (
                f"https://x.com/Timyr316661/status/{event_id}"
            )
            event["first_seen_at"] = (
                f"2026-07-25T16:{19 + offset:02d}:00Z"
            )
            events.append(event)
        self.write_events(list(reversed(events)))

        gated = autopilot_bridge.gate(self.config, lease_seconds=1800)
        claimed = autopilot_bridge.claim(self.config, lease_seconds=1800)

        expected = [events[0]["event_id"]]
        self.assertEqual(gated["pending_count"], 5)
        self.assertEqual(gated["event_ids"], expected)
        self.assertEqual(gated["queued_event_ids"], [
            event["event_id"] for event in reversed(events)
        ])
        self.assertEqual(gated["claim_limit"], 1)
        self.assertEqual(claimed["event_ids"], expected)
        self.assertIn("MAX_PARALLEL_X_READ_TABS=1", claimed["prompt"])
        self.assertIn(
            "PARALLEL_TAB_PREFLIGHT=single_tab_expected",
            claimed["prompt"],
        )
        self.assertIn(
            "DUPLICATE_SEARCH_POLICY=live_target_plus_ledger_first",
            claimed["prompt"],
        )
        self.assertIn(
            "FACTCHECK_POLICY=one_bounded_search_batch_first",
            claimed["prompt"],
        )
        self.assertIn(
            "POST_PUBLICATION_TEXT_POLICY=ui_identity_then_official_api",
            claimed["prompt"],
        )
        self.assertNotIn(
            "На iMac 8 GB открывай только одну вкладку X",
            claimed["prompt"],
        )

    def test_gate_does_not_dispatch_while_owner_is_active(self) -> None:
        self.write_events([self.event()])
        claimed = autopilot_bridge.claim(self.config, lease_seconds=1800)

        result = autopilot_bridge.gate(self.config, lease_seconds=1800)

        self.assertFalse(result["dispatch"])
        self.assertEqual(result["status"], "leased_waiting")
        self.assertEqual(
            result["event_ids"],
            claimed["event_ids"],
        )

    def test_gate_does_not_dispatch_while_doctor_owner_is_active(self) -> None:
        self.write_events([self.event()])
        supervisor = self.root / "var" / "autopilot-supervisor.json"
        supervisor.write_text(
            json.dumps(
                {
                    "version": 1,
                    "incident": {
                        "id": "doctor-incident",
                        "status": "work_in_progress",
                        "owner": {
                            "claim_token": "doctor-token",
                            "lease_expires_at": "2099-07-25T18:00:00Z",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        result = autopilot_bridge.gate(self.config, lease_seconds=1800)

        self.assertFalse(result["dispatch"])
        self.assertEqual(result["status"], "repair_waiting")
        self.assertEqual(result["repair_incident_id"], "doctor-incident")
        self.assertEqual(result["repair_status"], "work_in_progress")

    def test_gate_ignores_expired_doctor_owner(self) -> None:
        self.write_events([self.event()])
        supervisor = self.root / "var" / "autopilot-supervisor.json"
        supervisor.write_text(
            json.dumps(
                {
                    "version": 1,
                    "incident": {
                        "id": "doctor-incident",
                        "status": "work_in_progress",
                        "owner": {
                            "claim_token": "doctor-token",
                            "lease_expires_at": "2020-07-25T18:00:00Z",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        result = autopilot_bridge.gate(self.config, lease_seconds=1800)

        self.assertTrue(result["dispatch"])
        self.assertEqual(result["status"], "ready")

    def test_relay_handoff_reservation_blocks_duplicate_wake(self) -> None:
        self.write_events([self.event()])

        first = autopilot_bridge.reserve_handoff(
            self.config,
            lease_seconds=1800,
        )
        second = autopilot_bridge.reserve_handoff(
            self.config,
            lease_seconds=1800,
        )

        self.assertTrue(first["dispatch"])
        self.assertEqual(first["status"], "handoff_reserved_ready")
        self.assertFalse(second["dispatch"])
        self.assertEqual(second["status"], "handoff_reserved")
        self.assertEqual(
            second["reservation_token"],
            first["reservation_token"],
        )

    def test_relay_reserves_without_reading_owner_rollout(self) -> None:
        self.write_events([self.event()])
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["codex_state_database"] = str(
            self.root / "missing-state.sqlite"
        )
        self.config.write_text(json.dumps(config), encoding="utf-8")

        result = autopilot_bridge.reserve_handoff(
            self.config,
            lease_seconds=1800,
        )

        self.assertTrue(result["dispatch"])
        self.assertEqual(result["status"], "handoff_reserved_ready")

    def test_relay_prompt_runs_owner_in_same_heartbeat(self) -> None:
        prompt = (
            Path(__file__).resolve().parents[1]
            / "macos"
            / "x-relay.prompt.txt"
        ).read_text(encoding="utf-8")

        self.assertIn("self-owned heartbeat", prompt)
        self.assertIn("relay-reserve-handoff", prompt)
        self.assertIn("route=repair", prompt)
        self.assertIn("route=x", prompt)
        self.assertIn("route=outbound", prompt)
        self.assertIn("route=rotation", prompt)
        self.assertIn("started с точным claim-token", prompt)
        self.assertIn("renew с точным claim-token", prompt)
        self.assertIn("create_thread", prompt)
        self.assertIn("owner_rotation_marker", prompt)
        self.assertIn("list_threads", prompt)
        self.assertIn("automation_update", prompt)
        self.assertIn("set_thread_archived", prompt)
        self.assertIn("system_doctor.py", prompt)
        self.assertIn("FAIL 0", prompt)
        self.assertIn("browser_owner_rotation.py", prompt)
        self.assertIn("Не запускай второй reservation gate", prompt)
        self.assertNotIn("codex_app__send_message_to_thread", prompt)
        self.assertNotIn("tools.codex_app__send_message_to_thread", prompt)
        self.assertNotIn("tool_search", prompt)

    def test_relay_contract_targets_dynamic_browser_owner_role(self) -> None:
        contract = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "recovery"
                / "system-contract.json"
            ).read_text(encoding="utf-8")
        )
        owner = contract["threads"]["browser_owner"]
        automation = contract["automations"]["active_relay"]

        self.assertEqual(
            owner["id_source"],
            "config.browser_owner_thread_id",
        )
        self.assertEqual(owner["minimum_reasoning_effort"], "max")
        self.assertEqual(
            automation["target_thread_role"],
            "browser_owner",
        )
        self.assertNotIn("target_thread_id", automation)

    def test_owner_claim_is_not_blocked_by_its_own_active_turn(self) -> None:
        self.write_events([self.event()])

        result = autopilot_bridge.claim(
            self.config,
            lease_seconds=1800,
        )

        self.assertTrue(result["dispatch"])
        self.assertIsInstance(result["claim_token"], str)

    def test_owner_claim_clears_relay_handoff_reservation(self) -> None:
        self.write_events([self.event()])
        reservation = autopilot_bridge.reserve_handoff(
            self.config,
            lease_seconds=1800,
        )

        claimed = autopilot_bridge.claim(
            self.config,
            lease_seconds=1800,
        )

        state = json.loads(
            (self.root / "var/relay-handoff.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(reservation["dispatch"])
        self.assertTrue(claimed["dispatch"])
        self.assertEqual(state["status"], "claimed")
        self.assertEqual(state["claim_token"], claimed["claim_token"])

    def test_relay_can_release_only_its_exact_handoff_reservation(self) -> None:
        self.write_events([self.event()])
        reservation = autopilot_bridge.reserve_handoff(
            self.config,
            lease_seconds=1800,
        )

        released = autopilot_bridge.release_handoff_reservation(
            self.config,
            reservation_token=reservation["reservation_token"],
            reason="owner_thread_active",
        )
        retried = autopilot_bridge.reserve_handoff(
            self.config,
            lease_seconds=1800,
        )

        self.assertTrue(released["released"])
        self.assertEqual(released["status"], "handoff_released")
        self.assertTrue(retried["dispatch"])
        self.assertNotEqual(
            retried["reservation_token"],
            reservation["reservation_token"],
        )

    def test_relay_release_rejects_a_different_reservation_token(self) -> None:
        self.write_events([self.event()])
        reservation = autopilot_bridge.reserve_handoff(
            self.config,
            lease_seconds=1800,
        )

        with self.assertRaisesRegex(
            ValueError,
            "does not match active handoff",
        ):
            autopilot_bridge.release_handoff_reservation(
                self.config,
                reservation_token="different-token",
                reason="owner_thread_active",
            )

        state = json.loads(
            (self.root / "var/relay-handoff.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["status"], "reserved")
        self.assertEqual(
            state["reservation_token"],
            reservation["reservation_token"],
        )

    def test_relay_release_never_clears_a_claimed_handoff(self) -> None:
        self.write_events([self.event()])
        reservation = autopilot_bridge.reserve_handoff(
            self.config,
            lease_seconds=1800,
        )
        claimed = autopilot_bridge.claim(
            self.config,
            lease_seconds=1800,
        )

        released = autopilot_bridge.release_handoff_reservation(
            self.config,
            reservation_token=reservation["reservation_token"],
            reason="late_delivery_failure",
        )

        self.assertFalse(released["released"])
        self.assertEqual(released["reservation_status"], "claimed")
        state = json.loads(
            (self.root / "var/autopilot-dispatch.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            state["owner"]["claim_token"],
            claimed["claim_token"],
        )

    def test_claim_returns_exact_desktop_handoff(self) -> None:
        self.write_events([self.event()])

        result = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertTrue(result["dispatch"])
        self.assertIn(self.event()["event_id"], result["prompt"])
        self.assertIn(
            "Не выбирай и не записывай handoff route вручную",
            result["prompt"],
        )
        self.assertIn(
            "scripts/commit_browser_owner_event.py",
            result["prompt"],
        )
        self.assertNotIn("tracked_conversation_reply=true", result["prompt"])
        self.assertIn(
            "Следующий X API poll выполняет LaunchAgent",
            result["prompt"],
        )
        self.assertIn("Content-based skip запрещен", result["prompt"])
        self.assertIn("`satirical-media`", result["prompt"])
        self.assertIn("локальный skill `377`", result["prompt"])
        self.assertIn("`Ложкин`", result["prompt"])
        self.assertIn("`commenter_memory`", result["prompt"])
        self.assertIn('"commenter_memory":', result["prompt"])
        self.assertIn("Выбери short, local-max", result["prompt"])
        self.assertIn(
            "не означает модель ChatGPT Pro",
            result["prompt"],
        )
        self.assertNotIn("Выбери short, Pro", result["prompt"])
        self.assertNotIn("обычного Pro route", result["prompt"])
        self.assertNotIn("сделай один свежий poll", result["prompt"])
        self.assertNotIn("\u2013", result["prompt"])
        self.assertNotIn("\u2014", result["prompt"])

        self.write_events([])
        self.prepare_completion(
            result["claim_token"],
            result["event_ids"],
        )
        completed = autopilot_bridge.mark_completed(
            self.config,
            result["claim_token"],
        )
        self.assertEqual(completed["status"], "completed")
        health = json.loads(
            (self.root / "var" / "autopilot-health.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(health["handoff_count"], 1)
        dispatch = json.loads(
            (self.root / "var" / "autopilot-dispatch.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(dispatch["events"], {})

    def test_claim_includes_cross_thread_commenter_memory(self) -> None:
        watcher_config = watcher.load_config(self.config)
        connection = watcher.connect_database(watcher_config.database)
        current = self.event()
        prior_id = "2080000000000000001"
        with connection:
            for event_id, text, conversation_id, created_at in (
                (
                    prior_id,
                    "Prior exact public claim",
                    "2080000000000000000",
                    "2020-01-02T03:04:05Z",
                ),
                (
                    current["event_id"],
                    "Current exact public claim",
                    current["conversation_id"],
                    current["created_at"],
                ),
            ):
                payload = {
                    "id": event_id,
                    "author_id": "901",
                    "text": text,
                    "created_at": created_at,
                    "conversation_id": conversation_id,
                    "in_reply_to_user_id": "16337609",
                    "referenced_tweets": [
                        {"type": "replied_to", "id": str(int(event_id) - 1)}
                    ],
                }
                connection.execute(
                    """
                    INSERT INTO events(
                        event_id, author_id, username, created_at,
                        conversation_id, in_reply_to_user_id, is_reply,
                        payload_json, first_seen_at, delivery_state
                    ) VALUES(?, '901', 'Timyr316661', ?, ?, '16337609', 1,
                             ?, ?, 'queued')
                    """,
                    (
                        event_id,
                        created_at,
                        conversation_id,
                        json.dumps(payload, sort_keys=True),
                        watcher.isoformat(),
                    ),
                )
        watcher.import_history_snapshot(
            connection,
            {
                "chain_id": "2080000000000000000",
                "root_status_id": "2080000000000000000",
                "provenance": "short",
                "turns": [
                    {
                        "status_id": prior_id,
                        "actor": "user",
                        "author": "@Timyr316661",
                        "url": f"https://x.com/Timyr316661/status/{prior_id}",
                        "exact_text": "Prior exact public claim",
                    },
                    {
                        "status_id": "2080000000000000002",
                        "parent_status_id": prior_id,
                        "actor": "alex",
                        "author": "@axrbarsic",
                        "url": (
                            "https://x.com/axrbarsic/status/"
                            "2080000000000000002"
                        ),
                        "exact_text": "Prior exact Alex reply",
                    },
                ],
            },
        )
        connection.close()
        self.write_events([current])

        result = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertIn('"identity_kind":"x_user_id"', result["prompt"])
        self.assertIn("Prior exact public claim", result["prompt"])
        self.assertIn("Prior exact Alex reply", result["prompt"])

    def test_manual_parent_is_stored_and_live_chain_is_required(self) -> None:
        watcher_config = watcher.load_config(self.config)
        connection = watcher.connect_database(watcher_config.database)
        current = self.event()
        manual_parent_id = "2081050000000000001"
        prior_user_id = "2081050000000000000"
        payload = {
            "id": current["event_id"],
            "author_id": "901",
            "text": "Reply to Alex manual post",
            "created_at": current["created_at"],
            "conversation_id": current["conversation_id"],
            "in_reply_to_user_id": "16337609",
            "referenced_tweets": [
                {"type": "replied_to", "id": manual_parent_id}
            ],
        }
        with connection:
            connection.execute(
                """
                INSERT INTO events(
                    event_id, author_id, username, created_at,
                    conversation_id, in_reply_to_user_id, is_reply,
                    payload_json, first_seen_at, delivery_state
                ) VALUES(?, '901', 'Timyr316661', ?, ?, '16337609', 1,
                         ?, ?, 'queued')
                """,
                (
                    current["event_id"],
                    current["created_at"],
                    current["conversation_id"],
                    json.dumps(payload, sort_keys=True),
                    watcher.isoformat(),
                ),
            )
        watcher.import_history_snapshot(
            connection,
            {
                "chain_id": current["conversation_id"],
                "root_status_id": current["conversation_id"],
                "provenance": "short",
                "turns": [
                    {
                        "status_id": prior_user_id,
                        "actor": "user",
                        "author": "@Timyr316661",
                        "url": (
                            "https://x.com/Timyr316661/status/"
                            f"{prior_user_id}"
                        ),
                        "exact_text": "Earlier exact user turn",
                    },
                    {
                        "status_id": manual_parent_id,
                        "parent_status_id": prior_user_id,
                        "actor": "alex",
                        "author": "@axrbarsic",
                        "url": (
                            "https://x.com/axrbarsic/status/"
                            f"{manual_parent_id}"
                        ),
                        "exact_text": "Exact reply Alex posted manually",
                    },
                ],
            },
        )
        stored = connection.execute(
            """
            SELECT actor, exact_text
            FROM conversation_turns
            WHERE status_id = ?
            """,
            (manual_parent_id,),
        ).fetchone()
        self.assertEqual(stored["actor"], "alex")
        self.assertEqual(
            stored["exact_text"],
            "Exact reply Alex posted manually",
        )
        connection.close()
        self.write_events([current])

        result = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertIn(
            "восстанови полную ветку и историю",
            result["prompt"],
        )
        self.assertIn('"manual_parent_continuation"', result["prompt"])
        self.assertIn('"recommended_route":"short"', result["prompt"])
        self.assertIn('"chatgpt_web_allowed":false', result["prompt"])
        self.assertIn(current["event_id"], result["prompt"])

    def test_substantive_manual_parent_adapts_to_local_max(self) -> None:
        watcher_config = watcher.load_config(self.config)
        connection = watcher.connect_database(watcher_config.database)
        current = self.event()
        parent_id = "2081050000000000101"
        payload = {
            "id": current["event_id"],
            "author_id": "901",
            "text": "Reply to Alex manual explainer",
            "created_at": current["created_at"],
            "conversation_id": current["conversation_id"],
            "in_reply_to_user_id": watcher_config.user_id,
            "referenced_tweets": [
                {"type": "replied_to", "id": parent_id}
            ],
        }
        with connection:
            connection.execute(
                """
                INSERT INTO events(
                    event_id, author_id, username, created_at,
                    conversation_id, in_reply_to_user_id, is_reply,
                    payload_json, first_seen_at, delivery_state
                ) VALUES(?, '901', 'Timyr316661', ?, ?, ?, 1, ?, ?, 'queued')
                """,
                (
                    current["event_id"],
                    current["created_at"],
                    current["conversation_id"],
                    watcher_config.user_id,
                    json.dumps(payload, sort_keys=True),
                    watcher.isoformat(),
                ),
            )
        substantive = (
            "Первый подробный абзац о проверяемом тезисе.\n\n"
            "Второй абзац сохраняет аргументы и контекст.\n\n"
            "Третий абзац содержит источник: https://example.org/report"
        )
        watcher.import_history_snapshot(
            connection,
            {
                "chain_id": current["conversation_id"],
                "root_status_id": current["conversation_id"],
                "provenance": "short",
                "turns": [
                    {
                        "status_id": parent_id,
                        "actor": "alex",
                        "author": "@axrbarsic",
                        "url": f"https://x.com/axrbarsic/status/{parent_id}",
                        "exact_text": substantive,
                        "provenance": "self_authored_official_api",
                    }
                ],
            },
        )

        profile = autopilot_bridge.manual_parent_continuation_profile(
            connection,
            current["event_id"],
            configured_user_id=watcher_config.user_id,
        )
        chain = connection.execute(
            "SELECT provenance FROM conversation_chains WHERE chain_id = ?",
            (current["conversation_id"],),
        ).fetchone()
        connection.close()

        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile["recommended_route"], "local-max")
        self.assertEqual(
            profile["continuation_basis"],
            "adaptive_manual_parent_content",
        )
        self.assertEqual(
            profile["parent_origin_provenance"],
            "self_authored_official_api",
        )
        self.assertTrue(profile["content_profile"]["substantive"])
        self.assertFalse(profile["chatgpt_web_allowed"])
        self.assertEqual(chain["provenance"], "short")

    def test_missing_direct_parent_requires_exact_restore(self) -> None:
        watcher_config = watcher.load_config(self.config)
        connection = watcher.connect_database(watcher_config.database)
        current = self.event()
        parent_id = "2081050000000000201"
        payload = {
            "id": current["event_id"],
            "author_id": "901",
            "text": "Reply to unseen Alex parent",
            "created_at": current["created_at"],
            "conversation_id": current["conversation_id"],
            "in_reply_to_user_id": watcher_config.user_id,
            "referenced_tweets": [
                {"type": "replied_to", "id": parent_id}
            ],
        }
        with connection:
            connection.execute(
                """
                INSERT INTO events(
                    event_id, author_id, username, created_at,
                    conversation_id, in_reply_to_user_id, is_reply,
                    payload_json, first_seen_at, delivery_state
                ) VALUES(?, '901', 'Timyr316661', ?, ?, ?, 1, ?, ?, 'queued')
                """,
                (
                    current["event_id"],
                    current["created_at"],
                    current["conversation_id"],
                    watcher_config.user_id,
                    json.dumps(payload, sort_keys=True),
                    watcher.isoformat(),
                ),
            )

        profile = autopilot_bridge.manual_parent_continuation_profile(
            connection,
            current["event_id"],
            configured_user_id=watcher_config.user_id,
        )
        connection.close()

        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(
            profile["parent_history_status"],
            "missing_exact_alex_parent",
        )
        self.assertEqual(
            profile["recommended_route"],
            "pending_exact_parent_restore",
        )
        self.assertFalse(profile["chatgpt_web_allowed"])

    def test_claim_includes_official_archive_alex_reply_memory(self) -> None:
        watcher_config = watcher.load_config(self.config)
        connection = watcher.connect_database(watcher_config.database)
        current = self.event()
        payload = {
            "id": current["event_id"],
            "author_id": "901",
            "text": "Current exact public claim",
            "created_at": current["created_at"],
            "conversation_id": current["conversation_id"],
            "in_reply_to_user_id": "16337609",
        }
        with connection:
            connection.execute(
                """
                INSERT INTO events(
                    event_id, author_id, username, created_at,
                    conversation_id, in_reply_to_user_id, is_reply,
                    payload_json, first_seen_at, delivery_state
                ) VALUES(?, '901', 'Timyr316661', ?, ?, '16337609', 1,
                         ?, ?, 'queued')
                """,
                (
                    current["event_id"],
                    current["created_at"],
                    current["conversation_id"],
                    json.dumps(payload, sort_keys=True),
                    watcher.isoformat(),
                ),
            )
            import_id = connection.execute(
                """
                INSERT INTO archive_imports(
                    archive_fingerprint, account_user_id, username,
                    source_name, imported_at, post_count, inserted_post_count
                ) VALUES(
                    'fixture-archive', '16337609', 'axrbarsic',
                    'fixture.zip', ?, 1, 1
                )
                """,
                (watcher.isoformat(),),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO archive_posts(
                    status_id, first_import_id, author_id,
                    username_at_import, posted_at, exact_text,
                    parent_status_id, counterparty_user_id,
                    counterparty_username, conversation_id,
                    canonical_url, source_member, payload_sha256
                ) VALUES(
                    '7000000000000000001', ?, '16337609', 'axrbarsic',
                    '2020-01-02T03:04:05Z',
                    'Exact old Alex archive reply',
                    '6999999999999999999', '901', 'Timyr316661', NULL,
                    'https://x.com/i/web/status/7000000000000000001',
                    'data/tweets.js', 'fixture-sha'
                )
                """,
                (import_id,),
            )
        connection.close()
        self.write_events([current])

        result = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertIn("Exact old Alex archive reply", result["prompt"])
        self.assertIn("official_x_archive_alex_reply", result["prompt"])

    def test_claim_quarantines_external_candidate_memory(self) -> None:
        watcher_config = watcher.load_config(self.config)
        connection = watcher.connect_database(watcher_config.database)
        current = self.event()
        candidate_path = self.root / "candidate.jsonl"
        candidate_path.write_text(
            json.dumps(
                {
                    "record_type": "x_post",
                    "status_id": "7000000000000000001",
                    "account_handle": "Timyr316661",
                    "created_at_utc": "2020-01-02T03:04:05Z",
                    "canonical_url": (
                        "https://x.com/Timyr316661/status/"
                        "7000000000000000001"
                    ),
                    "text_state": "full_as_returned",
                    "text_verbatim": "Unverified external candidate",
                    "verification_state": "model_native_search_result",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        payload = {
            "id": current["event_id"],
            "author_id": "901",
            "text": "Current exact public claim",
            "created_at": current["created_at"],
            "conversation_id": current["conversation_id"],
            "in_reply_to_user_id": "16337609",
        }
        with connection:
            connection.execute(
                """
                INSERT INTO events(
                    event_id, author_id, username, created_at,
                    conversation_id, in_reply_to_user_id, is_reply,
                    payload_json, first_seen_at, delivery_state
                ) VALUES(?, '901', 'Timyr316661', ?, ?, '16337609', 1,
                         ?, ?, 'queued')
                """,
                (
                    current["event_id"],
                    current["created_at"],
                    current["conversation_id"],
                    json.dumps(payload, sort_keys=True),
                    watcher.isoformat(),
                ),
            )
        plan = candidate_corpus.plan_candidate_import(
            [candidate_path],
            subject_user_id="901",
            expected_handle="Timyr316661",
        )
        candidate_corpus.apply_candidate_import(connection, plan)
        connection.close()
        self.write_events([current])

        result = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertIn("Unverified external candidate", result["prompt"])
        self.assertIn('"usable_as_evidence":false', result["prompt"])
        self.assertIn(
            "Verify the exact live X post before quoting",
            result["prompt"],
        )
        self.assertIn(
            "запрещено цитировать запись",
            result["prompt"],
        )

    def test_overlapping_claim_preserves_work_in_progress_health(self) -> None:
        self.write_events([self.event()])
        first = autopilot_bridge.claim(self.config, lease_seconds=1800)
        started = autopilot_bridge.mark_started(
            self.config,
            first["claim_token"],
        )

        second = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertEqual(started["status"], "work_in_progress")
        self.assertFalse(second["dispatch"])
        health = json.loads(
            (self.root / "var" / "autopilot-health.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(health["status"], "work_in_progress")
        self.assertEqual(health["event_ids"], [self.event()["event_id"]])
        self.assertEqual(health["claim_token"], first["claim_token"])

    def test_new_event_cannot_replace_active_owner_or_health(self) -> None:
        first_event = self.event()
        second_event = {
            **self.event(),
            "event_id": "2081050838240211435",
            "event_url":
                "https://x.com/Timyr316661/status/2081050838240211435",
        }
        self.write_events([first_event])
        first = autopilot_bridge.claim(self.config, lease_seconds=1800)
        autopilot_bridge.mark_started(self.config, first["claim_token"])
        self.write_events([first_event, second_event])

        second = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertFalse(second["dispatch"])
        self.assertTrue(second["owner_busy"])
        self.assertEqual(
            second["active_claim_token"],
            first["claim_token"],
        )
        health = json.loads(
            (self.root / "var" / "autopilot-health.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(health["status"], "work_in_progress")
        self.assertEqual(health["event_ids"], [first_event["event_id"]])

    def test_memory_guard_defers_without_claiming(self) -> None:
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload.update(
            {
                "memory_guard_enabled": True,
                "memory_guard_max_codex_rss_mb": 1000,
                "memory_guard_max_renderer_count": 8,
                "memory_guard_max_node_repl_count": 6,
                "memory_guard_max_mcp_process_count": 10,
                "memory_guard_min_free_percent": 12,
            }
        )
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        self.write_events([self.event()])
        original_collect = resource_guard.collect
        resource_guard.collect = lambda: resource_guard.ResourceSample(
            codex_rss_mb=2500,
            renderer_count=6,
            node_repl_count=4,
            mcp_process_count=6,
            free_percent=20,
        )
        try:
            result = autopilot_bridge.claim(
                self.config,
                lease_seconds=1800,
            )
        finally:
            resource_guard.collect = original_collect

        self.assertFalse(result["dispatch"])
        self.assertEqual(result["status"], "memory_deferred")
        self.assertFalse(
            (self.root / "var" / "autopilot-dispatch.json").exists()
        )
        self.assertTrue(result["resource_guard"]["defer"])

    def test_voice_priority_defers_without_claiming(self) -> None:
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload.update(
            {
                "voice_priority_enabled": True,
                "memory_guard_enabled": False,
            }
        )
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        self.write_events([self.event()])
        original_collect = resource_guard.collect
        resource_guard.collect = lambda: resource_guard.ResourceSample(
            codex_rss_mb=900,
            renderer_count=2,
            node_repl_count=1,
            mcp_process_count=2,
            free_percent=60,
            voice_active=True,
            voice_input_pids=(123,),
        )
        try:
            result = autopilot_bridge.gate(
                self.config,
                lease_seconds=1800,
            )
        finally:
            resource_guard.collect = original_collect

        self.assertFalse(result["dispatch"])
        self.assertEqual(result["status"], "resource_deferred")
        self.assertIn(
            "voice_active=true",
            result["resource_guard"]["reasons"][0],
        )
        self.assertFalse(
            (self.root / "var" / "autopilot-dispatch.json").exists()
        )

    def test_gate_keeps_active_owner_when_resource_guard_defers(self) -> None:
        self.write_events([self.event()])
        first = autopilot_bridge.claim(self.config, lease_seconds=1800)
        autopilot_bridge.mark_started(self.config, first["claim_token"])
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload.update(
            {
                "memory_guard_enabled": True,
                "memory_guard_max_codex_rss_mb": 1000,
                "memory_guard_max_renderer_count": 8,
                "memory_guard_max_node_repl_count": 6,
                "memory_guard_max_mcp_process_count": 10,
                "memory_guard_min_free_percent": 12,
            }
        )
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        original_collect = resource_guard.collect
        resource_guard.collect = lambda: resource_guard.ResourceSample(
            codex_rss_mb=2500,
            renderer_count=9,
            node_repl_count=4,
            mcp_process_count=6,
            free_percent=20,
        )
        try:
            result = autopilot_bridge.gate(
                self.config,
                lease_seconds=1800,
            )
        finally:
            resource_guard.collect = original_collect

        self.assertFalse(result["dispatch"])
        self.assertEqual(result["status"], "leased_waiting")
        self.assertTrue(result["owner_busy"])
        self.assertEqual(result["leased_count"], 1)
        self.assertTrue(result["resource_guard"]["defer"])

    def test_completed_with_warning_is_success_after_queue_resolves(self) -> None:
        self.write_events([self.event()])
        first = autopilot_bridge.claim(self.config, lease_seconds=1800)
        autopilot_bridge.mark_started(self.config, first["claim_token"])
        self.write_events([])
        self.prepare_completion(
            first["claim_token"],
            first["event_ids"],
        )

        result = autopilot_bridge.mark_completed(
            self.config,
            first["claim_token"],
            warning="postflight poll unavailable",
        )

        self.assertEqual(result["status"], "completed_with_warning")
        health = json.loads(
            (self.root / "var" / "autopilot-health.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(health["handoff_count"], 1)
        self.assertEqual(health["error"], "postflight poll unavailable")

    def test_completed_rejects_event_without_durable_resolution(self) -> None:
        self.write_events([self.event()])
        claimed = autopilot_bridge.claim(self.config, lease_seconds=1800)
        self.write_events([])
        self.write_claim_manifest(
            claimed["claim_token"],
            claimed["event_ids"],
        )
        connection = watcher.connect_database(
            self.root / "var" / "watcher.sqlite3"
        )
        connection.close()

        with self.assertRaisesRegex(ValueError, "lack durable resolution"):
            autopilot_bridge.mark_completed(
                self.config,
                claimed["claim_token"],
            )

        state = autopilot_dispatch.load_state(
            self.root / "var/autopilot-dispatch.json"
        )
        self.assertEqual(
            state["owner"]["claim_token"],
            claimed["claim_token"],
        )

    def test_completed_rejects_missing_evidence_manifest(self) -> None:
        self.write_events([self.event()])
        claimed = autopilot_bridge.claim(self.config, lease_seconds=1800)
        self.write_events([])
        self.write_durable_resolutions(claimed["event_ids"])

        with self.assertRaisesRegex(ValueError, "manifest is incomplete"):
            autopilot_bridge.mark_completed(
                self.config,
                claimed["claim_token"],
            )

        state = autopilot_dispatch.load_state(
            self.root / "var/autopilot-dispatch.json"
        )
        self.assertEqual(
            state["owner"]["claim_token"],
            claimed["claim_token"],
        )

    def test_three_completed_batches_kick_remaining_queue_immediately(
        self,
    ) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.update(
            {
                "autopilot_max_claim_events": 1,
                "event_dispatch_on_new_events": True,
                "event_dispatch_state_file": "var/event-dispatch.json",
            }
        )
        self.config.write_text(json.dumps(config), encoding="utf-8")
        events = []
        for offset in range(4):
            event = dict(self.event())
            event_id = str(int(event["event_id"]) + offset)
            event["event_id"] = event_id
            event["event_url"] = (
                f"https://x.com/Timyr316661/status/{event_id}"
            )
            events.append(event)
        self.write_events(events)
        completed_process = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )

        with mock.patch.object(
            autopilot_bridge.event_dispatch,
            "DEFAULT_RUNNER",
            return_value=completed_process,
        ) as kick:
            remaining = list(events)
            for _cycle in range(3):
                ready = autopilot_bridge.gate(
                    self.config,
                    lease_seconds=1800,
                )
                self.assertTrue(ready["dispatch"])
                claimed = autopilot_bridge.claim(
                    self.config,
                    lease_seconds=1800,
                )
                autopilot_bridge.mark_started(
                    self.config,
                    claimed["claim_token"],
                )
                claimed_ids = set(claimed["event_ids"])
                remaining = [
                    event
                    for event in remaining
                    if event["event_id"] not in claimed_ids
                ]
                self.write_events(remaining)
                self.prepare_completion(
                    claimed["claim_token"],
                    claimed["event_ids"],
                )

                completed = autopilot_bridge.mark_completed(
                    self.config,
                    claimed["claim_token"],
                )

                expected_ids = sorted(
                    (event["event_id"] for event in remaining),
                    key=int,
                )
                self.assertEqual(completed["status"], "completed")
                self.assertEqual(completed["pending_count"], len(remaining))
                self.assertEqual(
                    completed["next_dispatch"]["status"],
                    "dispatch_kicked",
                )
                self.assertEqual(
                    completed["next_dispatch"]["event_ids"],
                    expected_ids,
                )
                state = json.loads(
                    (
                        self.root / "var" / "event-dispatch.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    state["reason"],
                    autopilot_bridge.event_dispatch.CLAIM_COMPLETED_REASON,
                )
                self.assertEqual(state["event_ids"], expected_ids)

        self.assertEqual(kick.call_count, 3)

    def test_reconcile_completed_requires_durable_resolution(self) -> None:
        database = self.root / "var" / "watcher.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE event_resolutions(
                    event_id TEXT PRIMARY KEY,
                    disposition TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO event_resolutions VALUES(?, 'skip')",
                (self.event()["event_id"],),
            )

        result = autopilot_bridge.reconcile_completed(
            self.config,
            [self.event()["event_id"]],
            warning="recovered postflight state",
        )

        self.assertEqual(result["status"], "completed_with_warning")
        self.assertEqual(result["event_ids"], [self.event()["event_id"]])

    def test_completed_reconciles_dispatcher_cleanup_after_resolution(
        self,
    ) -> None:
        current = self.event()
        self.write_events([current])
        claimed = autopilot_bridge.claim(self.config, lease_seconds=1800)
        autopilot_bridge.mark_started(
            self.config,
            claimed["claim_token"],
        )
        self.write_events([])
        state_path = self.root / "var" / "autopilot-dispatch.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["owner"] = None
        state["events"][current["event_id"]] = {"dispatch_count": 1}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        database = self.root / "var" / "watcher.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                """
                INSERT INTO event_resolutions(
                    event_id, disposition, reason, evidence_json, resolved_at
                )
                VALUES(?, 'published', 'verified', '[]', ?)
                """,
                (current["event_id"], watcher.isoformat()),
            )
        self.write_claim_manifest(
            claimed["claim_token"],
            claimed["event_ids"],
        )

        result = autopilot_bridge.mark_completed(
            self.config,
            claimed["claim_token"],
        )

        self.assertEqual(result["status"], "completed_with_warning")
        self.assertEqual(result["event_ids"], [current["event_id"]])
        self.assertEqual(
            result["requested_claim_token"],
            claimed["claim_token"],
        )
        self.assertTrue(result["reconciled"])
        self.assertIn("durable resolution", result["warning"])

    def test_reconciled_completion_reports_unrelated_new_pending_event(
        self,
    ) -> None:
        current = self.event()
        self.write_events([current])
        claimed = autopilot_bridge.claim(self.config, lease_seconds=1800)
        autopilot_bridge.mark_started(
            self.config,
            claimed["claim_token"],
        )
        new_event = self.event()
        new_event["event_id"] = "2081050000000000301"
        new_event["event_url"] = (
            "https://x.com/Timyr316661/status/2081050000000000301"
        )
        self.write_events([new_event])
        state_path = self.root / "var" / "autopilot-dispatch.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["owner"] = None
        state["events"][current["event_id"]] = {"dispatch_count": 1}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        database = self.root / "var" / "watcher.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                """
                INSERT INTO event_resolutions(
                    event_id, disposition, reason, evidence_json, resolved_at
                )
                VALUES(?, 'published', 'verified', '[]', ?)
                """,
                (current["event_id"], watcher.isoformat()),
            )
        self.write_claim_manifest(
            claimed["claim_token"],
            claimed["event_ids"],
        )

        result = autopilot_bridge.mark_completed(
            self.config,
            claimed["claim_token"],
        )

        self.assertEqual(result["pending_count"], 1)
        self.assertEqual(result["event_ids"], [current["event_id"]])


    def test_failed_handoff_releases_claim_for_immediate_retry(self) -> None:
        self.write_events([self.event()])
        first = autopilot_bridge.claim(self.config, lease_seconds=1800)

        failed = autopilot_bridge.mark_failed(
            self.config,
            first["claim_token"],
            "synthetic send failure",
        )
        second = autopilot_bridge.claim(self.config, lease_seconds=1800)

        self.assertEqual(failed["released"], [self.event()["event_id"]])
        self.assertTrue(second["dispatch"])


if __name__ == "__main__":
    unittest.main()
