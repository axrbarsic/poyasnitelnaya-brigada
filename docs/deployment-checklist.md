# Deployment checklist

## Gate 1: local validation

- [x] Python compile succeeds.
- [x] Unit tests cover replay, deduplication, pagination, API errors,
  acknowledgement, recovery, stale polling, long silence, and plist rendering.
- [x] Accumulated-event replay finds the same three IDs as the manual ledger.
- [x] Repeating the replay finds zero new IDs.
- [x] LaunchAgent templates pass plist validation.
- [x] The system Python 3.9 runtime passes the complete unit test suite.
- [x] Overlapping pollers, pagination loops, token redaction, and missing-token
  health reporting are covered.

## Gate 2: live shadow

- [x] Confirm official X Developer account, project, and existing app for
  `@axrbarsic`.
- [x] Confirm the existing app has 0 current-period requests or reads and no
  recent activity.
- [x] Regenerate the existing Bearer Token only after explicit approval because
  X warns that the old token will be invalidated.
- [x] Put the bearer token in macOS Keychain, never in the repository.
- [x] Install the signed app-like Keychain helper with a Mac provisioning
  profile that authorizes its Keychain access group.
- [x] Store the bearer item in the macOS Data Protection Keychain as
  `AfterFirstUnlockThisDeviceOnly` so the poll LaunchAgent keeps working after
  the screen is locked without migrating the token to another device.
- [x] Lock the screen and prove that the helper, poll LaunchAgent, and doctor
  stay healthy without an authentication prompt.
- [x] Configure the numeric X user ID.
- [x] Start with a new empty shadow database.
- [x] Run one live baseline poll and compare relevant IDs with the manual
  Browser scan and durable publication ledger.
- [x] Run at least three more polls and confirm no duplicates or missed
  eligible replies.
- [x] Inject two invalid-token failures and confirm the watchdog changes to
  `failing`.
- [x] Restore the token and confirm a successful poll clears the old error.
- [x] Complete the supervised trial with healthy recovery and zero pending
  eligible replies.

## Gate 3: production

- [x] Render the five machine-specific LaunchAgent plists.
- [x] Inspect their absolute paths. Keep the dispatcher plist unloaded until
  its live relay gate succeeds.
- [x] Confirm the loaded poll, watchdog, and janitor complete with exit code 0.
- [x] Confirm the retired CLI launcher exits nonzero before claiming an event.
- [x] Create the Sol Max Codex Desktop Browser owner.
- [x] Pause the five-minute Luna Low dispatcher automation.
- [x] Remove the retired external app-server and cross-thread Luna transport
  from production source, config examples, tests, and recovery contracts.
- [x] Require `desktop_relay_mode=in_app_heartbeat` and fail closed before gate
  or Desktop work for a missing or retired transport value.
- [x] Confirm the ready-only self-owned heartbeat routes work inside the exact
  pinned Sol Max owner without a cross-thread message.
- [x] Confirm an atomic reservation suppresses a second handoff before the
  owner claim appears.
- [x] Confirm one isolated empty dispatcher cycle spends no model turn,
  creates no task, and starts no helper bundle.
- [x] Keep the retired dispatcher automation `x` paused as a recovery marker.
  The current Desktop build exposes the official `automation_update` tool, and
  automation state is changed only through that tool.
- [x] Pause the retired outbound heartbeat `x-pro-15`.
- [x] Keep standalone local cron `x-15` PAUSED as a deployment marker with its
  versioned prompt and route live idle-only outbound through `x-relay`.
- [x] Prove one independent `x-15` run publishes a locally generated reply,
  deterministically capped at 4000 code points, without opening ChatGPT.
- [x] Protect each relay-created outbound attempt with the atomic
  `outbound_cycle.py` lease and a one-target limit.
- [x] Preempt outbound on any inbound event without recording catch-up debt.
- [x] Confirm real new replies create exact queue items.
- [x] Acknowledge the event only after the existing X workflow records its
  disposition.
- [x] Keep all publishing inside the claimed Sol Max Browser run.
- [x] Confirm four naturally rediscovered events produce one dispatcher claim.
- [x] Confirm a second dispatcher run inside the lease does not start a
  duplicate Sol turn.
- [x] Confirm efficiency, balanced, and performance modes are selected in live
  resource samples without changing Sol Max quality.
- [x] Confirm unit coverage makes active voice and the post-voice hold defer
  Browser work while the durable queue remains available.
- [x] Confirm the dispatcher process lock stops a second manual or launchd
  instance before gate or model startup.
- [x] Confirm a failed pre-resolution worker releases the exact claim.
- [x] Confirm the Browser worker durably resolves every event and the
  dispatcher prunes the claim.
- [x] Confirm postflight Keychain failure is represented as a warning after
  durable resolution, not as a rolled-back result.
- [x] Confirm mandatory response mode rejects content-based skips.
- [x] Confirm a clean API canary rediscovers prior content skips and publishes
  exactly one reply to each without receiving target IDs.
- [x] Update Codex CLI from the official digest-bearing release asset, run
  `codex doctor`, notify Alex, and append local plus GitHub history.
- [x] Install and load the event dispatcher and Codex CLI updater LaunchAgents.
- [x] Prove the loaded dispatcher remains model-free and process-neutral across
  repeated empty cycles.
- [x] Prove on two organic events that the loaded dispatcher launches an
  absent Desktop, marks the exact PID as supervisor-managed, lets the existing
  self-owned heartbeat wake the pinned Sol owner, preserves the queue through
  a resource deferral, and closes the managed Desktop after durable resolution.

## Gate 4: backup

- [x] Initialize a clean Git repository with runtime state ignored.
- [x] Verify that config, tokens, SQLite, queues, health, alerts, and logs are
  excluded.
- [x] Run every external candidate import in dry-run mode before `--apply`.
- [x] Confirm candidate records use the expected stable X user ID and remain
  `usable_as_evidence=false` until live X or official API verification.
- [x] Keep candidate corpora and exact-text verification scratch files out of
  Git when their redistribution status is unknown.
- [x] Create a public GitHub repository under the MIT license.
- [x] Push source, tests, templates, and documentation.
- [x] Add the watcher contract to the X skill only after the live gate is green.
- [x] Generate transactionally consistent, manifested SQLite snapshots.
- [x] Require a matching SHA-256 dry-run plan before official archive apply.
- [x] Snapshot and audit memory before and after official archive intake.
- [x] Import legacy Browser evidence into one manifested canonical tree.
- [x] Implement a fail-closed restic source plan that excludes live SQLite.
- [x] Cover secret-free command execution and a real local restore smoke test.
- [ ] Create the private B2 bucket and bucket-scoped application key.
- [ ] Initialize the encrypted remote repository after explicit approval.
- [ ] Run the first remote backup, `check --read-data`, and restore drill.
