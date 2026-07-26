# Unified readiness audit

`readiness_audit.py` is the single fail-closed completion gate for the complete
X memory system. A green individual test does not mean that the whole system is
ready.

Run:

```bash
python3 readiness_audit.py
```

The command makes no network requests, reads no secrets, and does not mutate
the live database. It verifies all of these components together:

- the canonical layout and the installed skill copy;
- SQLite integrity, foreign keys, identities, history, and resolutions;
- a complete official archive import for the configured numeric X user ID;
- watcher health;
- equality between the latest durable snapshot and current memory state;
- the local backup plan, including the archive vault after archive import;
- a remote backup receipt for the exact current plan fingerprint;
- a successful restore smoke test for that same restic snapshot.

`complete: true` is possible only when every component passes. Expected pending
states appear as machine-readable codes in `blockers`.
`official_x_archive_pending` is normal before the official archive arrives.
Before B2 is configured, `remote_backup_not_configured`,
`remote_backup_receipt_missing`, and `restore_smoke_receipt_missing` are
expected. `response_queue_pending` can appear temporarily while a new reply is
being processed.

A successful restore smoke test now writes the durable receipt
`var/backup-state/last-successful-restore-smoke.json`. It is not lost when a
later check or retention operation replaces `latest.json`. The readiness audit
matches its snapshot ID, plan fingerprint, and source count against the exact
last successful backup receipt.
