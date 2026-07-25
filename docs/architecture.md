# Architecture

## Boundary

The watcher is a read-only detector. It never drafts, classifies, or publishes
an X reply.

1. A five-minute Python poll calls the official X user mentions endpoint.
2. The first successful live poll stores available history, advances
   `since_id`, and queues every direct reply for an initial audit.
3. The initial audit remains incomplete until every direct reply has a durable
   `published`, `skip`, or contract-level `blocked` resolution. Queue state
   alone cannot satisfy this invariant.
4. Later polls queue only direct replies newer than the durable cursor.
5. SQLite deduplicates immutable event IDs and advances `since_id`.
6. `wake-request.json` exposes only queued event metadata and canonical URLs.
7. A macOS notification reports a new queue item without invoking a model.
8. A separate one-minute watchdog checks poll freshness and failure count.
9. Sol High opens the complete live X subtree, classifies text and media in
   context, and resolves an event only after publication or skip is durable.
10. `initial-audit-next` performs only free mechanical grouping and exact-text
    extraction. It never decides stance, relevance, or whether to publish.
11. `initial-audit-expire --hours H --as-of UTC` fixes Alex's requested
    per-run lookback cutoff without Browser or model use. Dry-run and apply
    reuse the same timestamp. In one transaction it imports exact stored API
    history and records an age-policy skip only for unresolved events strictly
    older than that cutoff.
12. New API events preserve expanded attachment metadata and alt text. Missing
    media metadata still requires live Browser inspection and is never treated
    as proof that no media exists.
13. `browser-handoff-sync` imports exact Browser history and applies only
    already confirmed Sol dispositions. It cannot draft, classify, or publish,
    and it fails closed on missing history or mismatched chain metadata. A
    publication must include both the inspected user turn and the exact
    verified Alex turn linked to that user event.

Each event resolution can preserve stance, confidence, media meaning, and
multiple evidence notes. This prevents a media-only reply from disappearing
into an undifferentiated skip category.

An explicit later scope expansion or recovered contract dependency may revise
`skip` to `published`, `skip` to `blocked`, `blocked` to `published`, or
`blocked` to `skip`. A published resolution is terminal. The immutable audit
row stores both versions and the revision reason before the current resolution
changes. Browser synchronization requires explicit
`supersedes_existing_resolution` and `resolution_revision_reason` fields, so
an ordinary duplicate or stale handoff cannot overwrite a decision.

`blocked` is not a content classification and not a synonym for `skip`. It
means the event requires action, but a required workflow dependency is
currently unavailable, such as the exact historical Pro conversation URL. A
blocked event remains visible in status and export data and may be revised
auditably after the dependency is recovered.

The stable `stance` field keeps one of four broad classes. `stance_detail`
preserves the exact Sol classification without weakening deterministic
filtering.

## Conversation memory

The same SQLite database stores append-only conversation chains:

- one chain row maps the root X status, short or Pro provenance, ledger
  reference, and exact ChatGPT conversation URL when applicable;
- one turn row stores exact public X text, parent status, actor, author, URL,
  timestamps, and provenance;
- source rows map factual replies to the primary sources used to prepare them;
- idempotent imports enrich missing nullable metadata but reject conflicting
  rewrites;
- an append-only chain provenance correction can neutralize an invalid flat
  turn hint without deleting or rewriting the original JSONL record;
- canonical JSONL export provides a reviewable private Git backup without
  including API credentials, cookies, or Browser state.

## Failure containment

- API credentials come from an environment variable or macOS Keychain.
- Credentials, SQLite, queue, health, alerts, and logs are excluded from Git.
- An API error never advances `since_id`.
- A duplicate API response never creates a duplicate queue item.
- Generic acknowledgement rejects direct replies. Only a durable `resolve`
  operation can remove them from the X workflow.
- `resolve` requires the exact inspected event turn in conversation history.
  The final audit gate also rejects legacy resolutions missing that turn.
- Published resolutions require the exact Alex turn, verified reply URL, and
  parent link back to the inspected event.
- Browser handoff synchronization accepts only explicit pending resolution
  records and remains idempotent after a partial run or retry.
- Age expiry is transactional and idempotent. It leaves exact-cutoff, newer,
  future-dated, malformed, missing-media, and history-conflicting events
  pending instead of guessing. It runs once at cycle start, so a queued or
  thinking event is never aged out during active work.
- A resolution revision is append-only. A transition to `published` requires a
  matching exact Alex publication turn. Only the audited transitions
  `skip` to `published`, `skip` to `blocked`, `blocked` to `published`, and
  `blocked` to `skip` are permitted.
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
