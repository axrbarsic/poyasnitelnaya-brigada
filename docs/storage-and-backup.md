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

Store `intake-plan.json` and `intake-receipt.json` in `reports/` beside the ZIP.
Do not extract the archive for normal import. `archive_intake.py` first runs a
dry-run, records the ZIP SHA-256, and refuses apply after any source change. It
creates a consistent memory snapshot before the append-only import, requires
the full archive audit, and creates another snapshot after import. The importer
reads only public account and tweet members and ignores direct-message members.

```bash
python3 archive_intake.py \
  --config config.json \
  --archive /Users/alexlane/Archives/x-mention-watcher/YYYY-MM-DD/source.zip
python3 archive_intake.py \
  --config config.json \
  --archive /Users/alexlane/Archives/x-mention-watcher/YYYY-MM-DD/source.zip \
  --apply
```

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

### Backup orchestrator

`restic_backup.py` enforces the storage contract instead of passing the whole
project directory to restic. It rejects corrupt or incomplete snapshots,
audits every manifested Browser evidence tree, excludes the live SQLite/WAL/SHM
combination, rejects broad archive-vault paths, and passes credentials only in
the child-process environment. It limits restic to two S3 connections and two
Go scheduler threads for the 8 GB iMac.

Copy `backup.example.json` to ignored `backup.json`, then set the B2 bucket
region and a harmless Keychain service name. Keep `archive_vault` equal to
`null` until the official archive exists. After it arrives, set the exact
external vault path. A configured missing or empty vault fails closed. Store
these four generic-password items under that service:

| Account | Value |
| --- | --- |
| `repository` | `s3:https://s3.<region>.backblazeb2.com/<bucket>/<dedicated-prefix>` |
| `restic-password` | independent restic repository password |
| `aws-access-key-id` | bucket-scoped B2 application key ID |
| `aws-secret-access-key` | bucket-scoped B2 application key |

The values never belong in JSON, Git, shell history, or command arguments.
Use the bundled Keychain helper and provide each value on standard input.

Before any remote write:

```bash
python3 restic_backup.py plan
python3 restic_backup.py preflight
```

After creating and verifying an existing private B2 bucket, initialize its
restic prefix once:

```bash
python3 restic_backup.py init --confirm-existing-private-bucket
```

Routine commands:

```bash
python3 restic_backup.py backup
python3 restic_backup.py check
python3 restic_backup.py retention
python3 restic_backup.py retention --apply
python3 restic_backup.py check --read-data
python3 restic_backup.py restore-smoke
```

`retention` is a dry run unless `--apply` is present. Every successful backup
stores the exact restic snapshot ID and a fingerprinted source receipt.
`restore-smoke` restores only that exact snapshot, verifies every source
against the receipt, checks free disk space with a safety margin, records a
secret-free result under `var/backup-state/latest.json` and the durable
`var/backup-state/last-successful-restore-smoke.json` receipt, and removes the
temporary restore after a successful or failed drill.

Check end-to-end readiness with one command:

```bash
python3 readiness_audit.py
```

See [readiness-audit.md](readiness-audit.md) for the complete component and
machine-readable blocker contract.

## Official references

- [restic repository setup and B2 guidance](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)
- [restic backup and exclusions](https://restic.readthedocs.io/en/stable/040_backup.html)
- [restic repository checks](https://restic.readthedocs.io/en/stable/077_troubleshooting.html)
- [Backblaze B2 pricing](https://www.backblaze.com/cloud-storage/pricing)
- [Backblaze B2 Object Lock](https://www.backblaze.com/docs/cloud-storage-object-lock)
- [Backblaze B2 lifecycle rules](https://www.backblaze.com/docs/en/cloud-storage-lifecycle-rules)
