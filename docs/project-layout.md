# Single project directory

## Canonical path

The complete executable project lives only at:

`/Users/alexlane/Developer/x-mention-watcher`

It contains:

```text
x-mention-watcher/
  .codex/                  Codex project session settings
  AGENTS.md                local operating contract
  docs/                    English and Russian documentation
  macos/                   source LaunchAgent templates
  scripts/                 dispatcher, bridge, and service utilities
  skill-backup/            restorable X skill source
  tests/                   complete test suite
  var/                     mutable local state, excluded from Git
  *.py                     watcher, importer, and snapshot tools
  config.json              local configuration, excluded from Git
  config.example.json      safe configuration template
```

`browser_owner_cwd` is `.`. The Browser owner therefore runs in the same
canonical directory and does not require a second project under
`Documents/Codex`. New screenshots, ledgers, payloads, and other evidence must
be stored below `var/evidence/browser-owner/<session-id>/`.

No separate manual step is required after durable synchronization.
`browser-handoff-sync` automatically creates and verifies `manifest.json`
when both input JSONL files are in the same canonical evidence directory. An
incomplete or inconsistent set fails closed and blocks the backup plan.

Import legacy evidence without modifying its source:

```bash
python3 evidence_import.py \
  --source /absolute/path/to/legacy-work \
  --label legacy-browser-owner-YYYY-MM-DD
python3 evidence_import.py \
  --audit var/evidence/browser-owner/legacy-browser-owner-YYYY-MM-DD
```

The importer rejects symlinks and likely credentials, verifies every copied
file by SHA-256, and atomically publishes a manifest. Its CLI always writes to
the canonical `var/evidence/browser-owner` tree.

## Required external deployment points

Only required deployment points remain outside the directory:

- `~/.codex/skills/x-twitter-operator`, the installed skill copy;
- `~/Library/LaunchAgents/com.axrbarsic.xmention.*.plist`, the loaded macOS
  services;
- macOS Keychain, which holds secrets;
- a separate archive vault for original X archive ZIP files.

These are not additional source trees. The skill is restored from
`skill-backup/`, LaunchAgents are rendered from `macos/`, and secrets are never
copied into the repository.

## Git boundary

Git contains only reproducible implementation. The following remain below
`var/` and are ignored:

- live SQLite, WAL, and SHM files;
- queue, health, and lock files;
- Browser evidence and temporary payloads;
- consistent database snapshots;
- conversation-history exports.

Official X archive ZIP files live in the separate archive vault. They are not
part of the project and are never committed.

## Development worktree rule

A temporary Git worktree is allowed only for one development checkpoint.
After verification, commit, push, and fast-forward deployment to the canonical
directory, the temporary worktree is removed. This prevents a second project
copy from becoming permanent.

Verify the contract mechanically:

```bash
python3 project_layout_audit.py --config config.json \
  --require-installed-skill
```
