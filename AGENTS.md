# X autopilot project contract

## Canonical workspace

- The only canonical project root is this repository.
- Run the Browser owner with this repository as its working directory.
- Keep source code, tests, documentation, launchd templates, Codex project
  configuration, and the restorable skill copy in this repository.
- Keep mutable runtime state below `var/`. Never create a second project
  workspace for Browser evidence, drafts, ledgers, or generated payloads.
- Store Browser evidence below `var/evidence/browser-owner/<session-id>/`.
- Store database snapshots below `var/snapshots/<utc-timestamp>/`.
- Official X archive ZIP files belong to the separate archive vault configured
  by the operator. They do not belong in this repository.

## Browser and publication

- Use the installed `x-twitter-operator` skill for every X or ChatGPT Browser
  operation.
- Keep the Browser owner on `gpt-5.6-sol` with high reasoning.
- Only the Browser owner may operate authenticated tabs, classify a live
  target, author a short reply, validate the composer, or publish.
- Keep one X tab, one ChatGPT tab, and at most one active Pro generation on
  Alex's 8 GB iMac.
- Use deterministic scripts for queue state, exact IDs, duplicate checks,
  Unicode length, forbidden characters, and timestamp filtering.
- Treat helper results as evidence. Sol High performs the final live-context
  decision and publication transaction.

## Data boundary

- Never commit `config.json`, credentials, SQLite, WAL, SHM, official X
  archives, runtime locks, generated evidence, or history exports.
- `var/watcher.sqlite3` is the live source of truth.
- Create consistent backups only with `memory_snapshot.py`.
- The installed skill under `~/.codex/skills/x-twitter-operator` is deployment
  state. Its restorable source is `skill-backup/x-twitter-operator`.
- The macOS LaunchAgent files under `~/Library/LaunchAgents` are deployment
  state generated from the tracked templates in `macos/`.

## Verification

- Before deployment, run the targeted tests for changed logic, the complete
  unit test suite once, `git diff --check`, and the forbidden U+2013/U+2014
  scan.
- Preserve user data and unrelated dirty state.
- Do not rewrite Git history, delete the archive vault, or remove the last
  valid database snapshot without explicit approval.
