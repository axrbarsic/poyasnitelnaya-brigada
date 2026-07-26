# Storage and backup contract

## One canonical root

The canonical system root is:

`/Users/alexlane/Developer/x-mention-watcher`

Git tracks source code, tests, documentation, and the restorable skill backup.
Git must not track live databases, official X archive ZIP files, credentials,
runtime locks, or generated evidence. All mutable state lives below `var/`:

```text
x-mention-watcher/
  var/
    watcher.sqlite3
    evidence/
      browser-owner/<session-id>/
    snapshots/
      <utc-timestamp>/
        watcher.sqlite3
        conversation-history.jsonl
        initial-audit-resolutions.jsonl
        manifest.json
    backup-state/
```

See [project-layout.md](project-layout.md) for the complete project tree and
the required external deployment points.

The active Codex skill remains installed under
`~/.codex/skills/x-twitter-operator`, because Codex discovers skills there.
Its exact restorable source remains in the Git repository under
`skill-backup/x-twitter-operator`. The installed path is deployment state, not
a second data store.

## SQLite rule

Never sync or upload the live `watcher.sqlite3`, its WAL, or its SHM file while
the watcher is running. A file-level copy can capture an inconsistent
combination. Create a transactionally consistent snapshot through SQLite's
backup API:

```bash
python3 memory_snapshot.py --config config.json
```

The snapshot command refuses invalid memory, validates SQLite integrity and
foreign keys, runs the complete memory audit, exports human-readable history
and resolutions, hashes every durable file, and atomically publishes a
manifested directory under `var/snapshots/`.

## Official X archives

Official ZIP archives do not belong in the project directory. Keep them
immutable in a separate protected archive vault, for example:

`/Users/alexlane/Archives/x-mention-watcher/<request-date>/source.zip`

Store dry-run and apply reports beside the ZIP. Do not extract the archive for
normal import. The importer reads only the public account and tweet members and
ignores direct-message members.

## Online backup

The recommended independent target is an encrypted restic repository in a
private Backblaze B2 bucket through B2's S3-compatible API.

- restic protects the repository with its own password and supports multiple
  access keys.
- The restic documentation recommends the S3-compatible API for B2 instead of
  its legacy native B2 backend.
- Backblaze B2 is independent of the Apple account and currently starts at
  USD 6.95 per TB per month, with pay-as-you-go billing and no minimum storage
  duration.
- Store the B2 application key and the automation copy of the restic password
  in macOS Keychain. Store a second restic password or recovery copy outside
  Apple, for example in an independent password manager or a sealed offline
  recovery record. Losing all restic keys makes the repository unrecoverable.

Back up only:

- completed directories under `var/snapshots/`;
- immutable sources and reports from the separate archive vault;
- canonical raw evidence under `var/evidence/`.

Do not back up locks, health files, wake files, caches, live WAL/SHM files, or
temporary snapshot directories.

Suggested retention after the remote is configured:

- hourly snapshots for 48 hours;
- daily snapshots for 30 days;
- weekly snapshots for 12 weeks;
- monthly snapshots for 24 months.

Run `restic check` weekly. Run `restic check --read-data` and a real restore
drill monthly. A restore drill is successful only when the restored SQLite
passes:

```bash
python3 xmention_watcher.py --config restored-config.json \
  memory-audit --require-archive
```

Do not enable a default Object Lock policy on a live restic repository without
testing it: protected lock and pack objects can conflict with normal restic
cleanup. If immutable retention is required, use a separate bucket or prefix
for periodic exported snapshot bundles.

## Official references

- [restic repository setup and B2 guidance](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)
- [restic backup and exclusions](https://restic.readthedocs.io/en/stable/040_backup.html)
- [restic repository checks](https://restic.readthedocs.io/en/stable/077_troubleshooting.html)
- [Backblaze B2 pricing](https://www.backblaze.com/cloud-storage/pricing)
- [Backblaze B2 Object Lock](https://www.backblaze.com/docs/cloud-storage-object-lock)
- [Backblaze B2 lifecycle rules](https://www.backblaze.com/docs/en/cloud-storage-lifecycle-rules)
