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
- [x] Configure the numeric X user ID.
- [x] Start with a new empty shadow database.
- [x] Run one live baseline poll and compare relevant IDs with the manual
  Browser scan and durable publication ledger.
- [x] Run at least three more polls and confirm no duplicates or missed direct
  replies.
- [x] Inject two invalid-token failures and confirm the watchdog changes to
  `failing`.
- [x] Restore the token and confirm a successful poll clears the old error.
- [x] Complete the supervised trial with healthy recovery and zero pending
  direct replies.

## Gate 3: production

- [x] Render the two machine-specific LaunchAgent plists.
- [x] Inspect their absolute paths and load them explicitly with `launchctl`.
- [x] Confirm poll and watchdog complete independently with exit code 0.
- [ ] Confirm a real new mention creates one queue item and one notification.
- [ ] Acknowledge the event only after the existing X workflow records its
  disposition.
- [ ] Keep all publishing in the Sol High Browser-owner session.

## Gate 4: backup

- [x] Initialize a clean Git repository with runtime state ignored.
- [x] Verify that config, tokens, SQLite, queues, health, alerts, and logs are
  excluded.
- [x] Create a private GitHub repository.
- [x] Push source, tests, templates, and documentation.
- [x] Add the watcher contract to the X skill only after the live gate is green.
