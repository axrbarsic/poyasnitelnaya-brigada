# Architecture

## Boundary

The watcher is a read-only detector. It never drafts, classifies, or publishes
an X reply.

1. A five-minute Python poll calls the official X user mentions endpoint.
2. The first successful live poll stores an observed baseline and advances
   `since_id` without queueing the historical archive.
3. Later polls queue only direct replies to the configured user ID.
4. SQLite deduplicates immutable event IDs and advances `since_id`.
5. `wake-request.json` exposes only queued event metadata and canonical URLs.
6. A macOS notification reports a new queue item without invoking a model.
7. A separate one-minute watchdog checks poll freshness and failure count.
8. Sol High opens the live X thread, applies the existing X skill contract, and
   acknowledges an event only after disposition is durably recorded.

## Failure containment

- API credentials come from an environment variable or macOS Keychain.
- Credentials, SQLite, queue, health, alerts, and logs are excluded from Git.
- An API error never advances `since_id`.
- A duplicate API response never creates a duplicate queue item.
- A process lock prevents overlapping pollers from racing the cursor.
- Repeated pagination tokens and excessive page counts fail closed.
- Background output is discarded so LaunchAgent logs cannot grow without bound.
- The watchdog runs independently, so a dead poller cannot conceal its death.
- Long silence is a review signal, not evidence that X has no new replies.
- LaunchAgents are loaded only after live shadow output matches a manual
  Browser scan.

## Token model

Polling, deduplication, queueing, health checks, and notifications use no model
calls. Model usage begins only after a queued event is intentionally handed to
the existing X workflow.
