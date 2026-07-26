# Architecture

## Boundary

The watcher is a read-only detector. It never drafts, classifies, or publishes
an X reply.

1. A one-minute Python poll calls the official X user mentions endpoint.
2. The first successful live poll stores available history, advances
   `since_id`, and queues every direct reply for an initial audit.
3. The initial audit remains incomplete until every direct reply has a durable
   `published`, proven `already-answered` skip, or terminal contract-level
   `blocked` resolution. Queue state alone cannot satisfy this invariant.
4. Later polls queue direct replies and replies in any conversation whose exact
   history already contains an `alex` turn. In mandatory mode, every reply
   returned by the authenticated mentions endpoint is queued for live
   inspection, even when its immediate parent is another participant.
5. SQLite deduplicates immutable event IDs and advances `since_id`.
6. `wake-request.json` exposes only queued event metadata and canonical URLs.
7. A macOS notification reports a new queue item without invoking a model.
8. Under explicit standing authority, a five-minute Luna Low Codex Desktop
   automation runs the read-only `autopilot_bridge gate`. It exits for an empty,
   leased, voice-paused, or resource-deferred queue.
9. A ready gate sends one follow-up to a pinned Sol High Browser-owner task.
   That task executes the atomic claim and remains the only publication owner.
10. A separate one-minute watchdog checks poll freshness and failure count.
11. Sol High opens the complete live X subtree, classifies text and media in
    context, and resolves an event only after publication, exact proof of an
    existing direct Alex child reply, or a terminal blocker.
12. `initial-audit-next` performs only free mechanical grouping and exact-text
    extraction. It never decides stance, relevance, or whether to publish.
13. `initial-audit-expire --hours H --as-of UTC` fixes Alex's requested
    per-run lookback cutoff without Browser or model use. Dry-run and apply
    reuse the same timestamp. In one transaction it imports exact stored API
    history and records an age-policy skip only for unresolved events strictly
    older than that cutoff.
14. New API events preserve expanded attachment metadata and alt text. Missing
    media metadata still requires live Browser inspection and is never treated
    as proof that no media exists.
15. `browser-handoff-sync` imports exact Browser history and applies only
    already confirmed Sol dispositions. It cannot draft, classify, or publish,
    and it fails closed on missing history or mismatched chain metadata. A
    publication must include both the inspected user turn and the exact
    verified Alex turn linked to that user event. When the handoff files are
    inside the canonical Browser evidence tree, the same command atomically
    creates and verifies the evidence manifest after durable resolution.
    Every handoff proves exactly one authorization route: a direct reply to
    Alex, a reply in a conversation with a stored Alex turn, or an explicit
    `@axrbarsic` mention returned by the authenticated mentions endpoint.
16. `autopilot_bridge` enriches each claimed event with compact
    `commenter_memory` keyed by stable X user ID. It contains source-linked
    public turns and exact Alex children from any stored conversation. A deeper
    `commenter-history` query can search all retained years without adding the
    full archive to every Sol prompt.
17. `x_archive_import.py` stages Alex's historical public posts and replies
    from an official X archive. It validates the archive account against the
    configured numeric X user ID, ignores direct-message members, and rejects
    append-only conflicts. Archive replies are exposed separately from exact
    incoming interaction history because the archive does not contain a
    complete copy of other users' turns.
18. `candidate_corpus.py` stores externally collected public posts in a
    quarantined index keyed by stable X user ID. It exposes at most three
    compact search hints per event. Unverified hints have
    `usable_as_evidence=false`; only an append-only live X or official API
    verification can promote the exact observed record.

Each event resolution can preserve stance, confidence, media meaning, and
multiple evidence notes. This prevents a media-only reply from disappearing
into an undifferentiated skip category.

An explicit later scope expansion or recovered contract dependency may revise
`skip` to `skip`, `skip` to `published`, `skip` to `blocked`, `blocked` to
`published`, or `blocked` to `skip`. A published resolution is terminal. The immutable audit
row stores both versions and the revision reason before the current resolution
changes. Browser synchronization requires explicit
`supersedes_existing_resolution` and `resolution_revision_reason` fields, so
an ordinary duplicate or stale handoff cannot overwrite a decision.

`blocked` is not a content classification and not a synonym for `skip`. In
mandatory response mode it requires an allowed terminal `blocker_code`.
Temporary Browser, Pro, rate, and validation failures remain queued for retry.

`mandatory-response-requeue` selects recent content-based skips and previously
ignored mention replies with no exact direct Alex child reply, records their
prior state in `response_policy_requeues`, and returns them to the ordinary wake
queue without receiving target IDs.

## Clean reliability experiment

When a screenshot appears to show a missed event, inspect every visible
candidate and identify the first failing pipeline layer. A clean replay never
injects the known target ID into the queue or automation prompt.

1. Preserve live X, API, SQLite, queue, lease and Browser evidence.
2. Fix a universal eligibility or lifecycle rule with synthetic regression
   data.
3. Deploy the change.
4. Run target-agnostic `mandatory-response-requeue` with one fixed lookback and
   `as-of`, first dry-run and then apply.
5. Let the five-minute Luna dispatcher rediscover the event and wake the pinned
   Sol owner.
6. Verify one live direct Alex child, exact history, durable resolution, empty
   queue and no active lease.

The full operator procedure is stored in
[`reliability-debugging.md`](../skill-backup/x-twitter-operator/references/reliability-debugging.md).

The stable `stance` field keeps one of four broad classes. `stance_detail`
preserves the exact Sol classification without weakening deterministic
filtering.

## Conversation memory

The same SQLite database stores append-only conversation chains:

- one chain row maps the root X status, short or Pro provenance, ledger
  reference, and exact ChatGPT conversation URL when applicable;
- an Alex reply posted manually is still an `alex` turn. When a later mention
  points to it, the Browser owner must restore that exact live parent and the
  surrounding subtree before drafting, then persist the manual turn before the
  new disposition;
- one turn row stores exact public X text, parent status, actor, author, URL,
  timestamps, and provenance;
- source rows map factual replies to the primary sources used to prepare them;
- idempotent imports enrich missing nullable metadata but reject conflicting
  rewrites;
- an append-only chain provenance correction can neutralize an invalid flat
  turn hint without deleting or rewriting the original JSONL record;
- canonical JSONL export provides a reviewable private Git backup without
  including API credentials, cookies, or Browser state.

The author-memory layer is evidence memory, not a psychological profile. Sol
may cite an exact prior public statement to identify a contradiction, changed
standard, or repeated claim. It may not infer hidden motives, sensitive
attributes, or optimize political persuasion around personal vulnerabilities.

The archive layer stores only filtered public-post fields. It never extracts an
archive to disk, never reads a direct-message member, and never stores the raw
archive payload. Stable `in_reply_to_user_id` values can link an old Alex reply
to a present commenter. Such a record proves only what Alex wrote and when. It
does not reconstruct or invent the missing incoming post.

The candidate layer is deliberately separate from exact conversation and
archive history. Its immutable source record preserves provenance and source
verification state. A later verification stores the exact observed public text
in a second table, without rewriting the candidate. Until that verification
exists, the record may guide a live search but may not support a quotation,
contradiction claim, or factual conclusion.

## Failure containment

- API credentials come from an environment variable or macOS Keychain.
- Credentials, SQLite, queue, health, alerts, and logs are excluded from Git.
- An API error never advances `since_id`.
- A duplicate API response never creates a duplicate queue item.
- Dispatcher claims are serialized with a file lock and atomic state replace.
  Queue snapshots and watcher replacements share a separate wake-file lock.
  A live lease prevents duplicate Browser-owner runs. Failure before durable
  resolution removes the claim token for an immediate retry.
- Resolved events disappear from the wake file and are pruned from dispatcher
  state. An unresolved event becomes eligible again after the lease expires,
  so a crashed Browser-owner turn cannot strand the queue forever.
- The built-in Browser is not available in Codex CLI. The scheduled Desktop
  automation therefore performs Browser work inside its own Sol High run and
  never delegates through a CLI resume or a cross-thread app call.
- An IAB timeout is classified per execution turn. One fresh turn in the same
  Browser-owner task may run the official bootstrap once; publication resumes
  only after an authenticated read-only preflight succeeds.
- `work_in_progress` survives overlapping empty scheduled runs. `completed`
  requires every claimed ID to leave the wake queue. A postflight warning never
  overwrites a durable resolution with failure.
- Generic acknowledgement rejects eligible replies. Only a durable `resolve`
  operation can remove them from the X workflow.
- `resolve` requires the exact inspected event turn in conversation history.
  The final audit gate also rejects legacy resolutions missing that turn.
- In mandatory response mode, `resolve` rejects every content-based skip and
  accepts skip only after exact history proves the direct Alex child reply.
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
  `skip` to `skip`, `skip` to `published`, `skip` to `blocked`, `blocked` to
  `published`, and `blocked` to `skip` are permitted. The skip-to-skip
  transition is allowed only in mandatory mode with existing-reply proof.
- A process lock prevents overlapping pollers from racing the cursor.
- Repeated pagination tokens and excessive page counts fail closed.
- Background output is discarded so LaunchAgent logs cannot grow without bound.
- The watchdog runs independently, so a dead poller cannot conceal its death.
- Long silence is a review signal, not evidence that X has no new replies.
- LaunchAgents are loaded only after live shadow output matches a manual
  Browser scan.

## Token model

Polling, deduplication, queueing, health checks, lease checks, and notifications
use no model calls. A five-minute Luna Low scheduled run spends a small amount
of context on the read-only gate. Sol High receives a turn only when the gate
proves that a ready event exists.
