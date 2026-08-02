# Architecture

## Runtime module boundary

`xmention_watcher.py` is the composition root and compatibility facade. It
owns the process lock, opens exactly one SQLite connection for a CLI command,
wires explicit dependencies, and closes that connection in `finally`. Domain
logic is split by durable responsibility:

| Module | Single responsibility |
| --- | --- |
| `watcher_database` | Schema creation and SQLite connection lifecycle |
| `watcher_events` | Ingestion, deduplication, eligibility, queue projection |
| `watcher_polling` | Mentions, conversation tails, budgets, pagination state |
| `watcher_history` | Append-only conversation history |
| `watcher_resolution` | Validated terminal event transitions and audit export |
| `watcher_audit_state`, `watcher_audit_lifecycle` | Read-only audit state and transactional audit changes |
| `watcher_memory` | Commenter memory and memory consistency checks |
| `watcher_health` | Failure recording, health projection, watchdog decisions |
| `watcher_auth`, `watcher_http` | Keychain boundary and minimal X HTTP client |
| `watcher_cli` | Declarative CLI schema without runtime mutation |

Cross-domain side effects are injected at the composition root. A domain
module cannot silently open a second database, operate the Browser, or resolve
an event through an alternate path. `browser_owner_evidence` is the only
semantic evidence validator and per-event committer; aggregate finalization
derives its records from those already committed event records.

The control plane follows the same boundary rule. Public script names remain
compatibility facades for LaunchAgent, tests, and recovery commands, while
their implementation is divided by durable authority:

| Public boundary | Internal modules | Responsibility |
| --- | --- | --- |
| `app_server_dispatch` | `app_server_desktop`, `app_server_external` | Desktop ownership and disabled external app-server diagnostics |
| `autopilot_supervisor` | `autopilot_supervisor_incidents`, `autopilot_supervisor_routes` | Incident persistence and deterministic recovery routing |
| `system_doctor` | `system_doctor_runtime`, `system_doctor_contract` | Runtime projections and versioned deployment checks |
| `autopilot_bridge` | `autopilot_dispatch`, `event_dispatch` | Queue lease transitions and model-free wake delivery |
| `browser_owner_evidence` | `browser_handoff_sync`, `build_outbound_history` | Per-event evidence commit and idempotent aggregate replay |

The compatibility facade may bind dependencies, preserve legacy imports, and
translate CLI arguments. It must not become a second state owner. Each mutable
operation follows one order: parse and validate complete input, acquire the
domain lock, confirm the expected owner or version, perform one durable write,
then expose a derived result. In particular, session finalization checks an
existing aggregate for conflicts before handoff replay can mutate SQLite.

`session_janitor`, `resource_guard`, `keychain_bundle`, and `outbound_cycle`
remain single public modules because they each own one bounded capability.
Their orchestration is split internally into typed snapshots, pure decision
helpers, and narrow side-effect functions. Native process enumeration and
CoreAudio access use shared adapters instead of duplicate ctypes setup.

## Control-plane authority

The runtime has one router, `x-relay`, and three disjoint state owners. No
operator task, doctor run, or retired automation may duplicate their authority.

| State | Sole owner | Meaning |
| --- | --- | --- |
| Inbound queue and global X lease | `autopilot_bridge` | Which exact X events are pending or owned |
| Repair incident and repair lease | `autopilot_supervisor` | Whether a system defect needs doctor work |
| Ten-minute outbound slot | `outbound_cycle` | Whether one idle-only outbound attempt is due |
| Final route | `x-relay` through `relay-reserve-handoff` | Exactly one of inbound X, repair, outbound, or idle |

The retired `x-15` automation is permanently `PAUSED`. Its status is a
deployment invariant, not a runtime switch. The ten-minute cadence lives only
in `outbound-cycle.json`; normal operation must never activate or pause an
automation to express queue pressure.

Doctor observations are classified by recovery owner in
`autopilot_state_model.recovery_owner`. `runtime.relay_progress` and
`runtime.queue_latency` belong to X delivery, so they must release the relay to
the inbound route instead of creating competing doctor work. Any mixed failure
containing a storage, contract, credential, or deployment defect belongs to
doctor because publication may no longer be durable or safe.

This gives one fixed priority rule:

1. Continue an already active authenticated writer transaction.
2. Recover pending inbound X delivery.
3. Repair a defect that blocks safe durable work.
4. Run one due outbound attempt only when inbound is exactly idle.
5. Otherwise do nothing.

The doctor observes these owners and verifies their leases. It does not become
a second scheduler. The root Codex task may change versioned source and deploy
an explicit repair, but it does not toggle runtime automation state during
normal queue operation.

## Boundary

The watcher is a read-only detector. It never drafts, classifies, or publishes
an X reply.

1. A one-minute Python poll calls the official X user mentions endpoint.
   When conversation tail monitoring is enabled, the same token-free poll also
   runs a bounded recent search over conversation IDs with a recent exact
   `alex` turn. This finds nested replies that continue the live discussion
   without repeating `@axrbarsic`.
2. The first successful live poll stores available history, advances
   `since_id`, and queues every direct reply for an initial audit.
3. The initial audit remains incomplete until every direct reply has a durable
   `published`, proven `already-answered` skip, or terminal contract-level
   `blocked` resolution. Queue state alone cannot satisfy this invariant.
4. Later polls queue direct replies and replies in any conversation whose exact
   history already contains an `alex` turn. In mandatory mode, every reply
   returned by the authenticated mentions endpoint is queued for live
   inspection, even when its immediate parent is another participant.
5. SQLite deduplicates immutable event IDs. The mentions cursor and the
   conversation tail scan timestamp are independent, so a newer nested reply
   can never hide a direct mention.
6. `wake-request.json` exposes only queued event metadata and canonical URLs.
7. A macOS notification reports a new queue item without invoking a model.
8. After a live poll durably queues new event IDs, it performs one non-killing
   `launchctl kickstart` of the existing dispatcher LaunchAgent. The periodic
   minute launch remains active as a fallback. The kick does not reserve,
   claim, inspect, or remove queue events.
9. Under explicit standing authority, a one-minute LaunchAgent runs the
   read-only `autopilot_bridge gate` in Python. Empty, leased, voice-paused, and
   resource-deferred queues use no model and create no Codex task.
10. A ready gate checks Codex Desktop. If Desktop is absent, the supervisor
   launches it in the canonical repository and records the exact PID it owns.
11. One existing in-app heartbeat is attached directly to the dedicated Sol
    Max service worker and calls one deterministic `relay-reserve-handoff`.
    Python selects repair, inbound X, or idle-only outbound, creates at most one
    reservation, and returns one unambiguous `dispatch` plus `route`. The same
    task executes the corresponding claim. There is no cross-thread message,
    relay task, or process-local `hostId` dependency. Codex serializes a new
    heartbeat behind any active owner turn.
12. The service worker remains the only authenticated Browser publication
    owner. Atomic reservation and the global owner claim prevent adjacent
    heartbeat ticks from creating concurrent Browser owners. After the queue
    is empty, the supervisor may close only the exact Desktop PID it launched.
13. A separate one-minute watchdog checks poll freshness and failure count.
14. Sol Max opens the complete live X subtree, classifies text and media in
    context, and resolves an event only after publication, exact proof of an
    existing direct Alex child reply, or a terminal blocker.
15. `initial-audit-next` performs only free mechanical grouping and exact-text
    extraction. It never decides stance, relevance, or whether to publish.
16. `initial-audit-expire --hours H --as-of UTC` fixes Alex's requested
    per-run lookback cutoff without Browser or model use. Dry-run and apply
    reuse the same timestamp. In one transaction it imports exact stored API
    history and records an age-policy skip only for unresolved events strictly
    older than that cutoff.
17. New API events preserve expanded attachment metadata and alt text. Missing
    media metadata still requires live Browser inspection and is never treated
    as proof that no media exists.
18. `evidence.json` is the only semantic terminal record written by the
    Browser owner for one event. `commit_browser_owner_event.py` validates its
    exact target, local generation profile, reply file, official X report and
    active claim membership. It derives the authorization route from SQLite,
    generates history and ledger records, imports the exact chain, and durably
    resolves the event as one idempotent operation. The owner must never write
    either JSONL file or call history import and resolve separately.
    `finalize_browser_owner_session.py` is only the claim aggregation boundary.
    After every event is committed, it verifies the active claim set, merges
    generated per-event JSONL atomically, replays the idempotent handoff sync,
    and creates the immutable manifest before `completed` can run. This
    removes the previous dual-write window between evidence, history and the
    resolution ledger.
19. `autopilot_bridge` enriches each claimed event with compact
    `commenter_memory` keyed by stable X user ID. It contains source-linked
    public turns and exact Alex children from any stored conversation. A deeper
    `commenter-history` query can search all retained years without adding the
    full archive to every Sol prompt.
20. `x_archive_import.py` stages Alex's historical public posts and replies
    from an official X archive. It validates the archive account against the
    configured numeric X user ID, ignores direct-message members, and rejects
    append-only conflicts. Archive replies are exposed separately from exact
    incoming interaction history because the archive does not contain a
    complete copy of other users' turns.
21. `candidate_corpus.py` stores externally collected public posts in a
    quarantined index keyed by stable X user ID. It exposes at most three
    compact search hints per event. Unverified hints have
    `usable_as_evidence=false`; only an append-only live X or official API
    verification can promote the exact observed record.
22. Conversation tail search is not broad discovery. It watches only recent
    chains already containing an exact Alex turn, excludes Alex's own posts,
    applies a bounded first lookback, and reuses immutable event ID
    deduplication. X bills read endpoints per returned resource and normally
    deduplicates the same resource within one UTC day.
23. Scheduled outbound reuses the existing one-minute self-owned `x-relay`; standalone
    cron `x-15` remains paused. After repair and inbound routing, the relay may
    atomically reserve one outbound attempt for the current 10-minute window
    only when the inbound queue is exactly empty and both writer leases are
    idle. The Sol Max owner repeats the inbound gate after target selection,
    after generation, and immediately before publication. Any single inbound
    event releases the outbound claim through `pause-slot`. Skipped windows do
    not accumulate catch-up debt and are never replayed later.

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
Temporary Browser, local-max generation, rate, and validation failures remain
queued for retry.

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
5. Let the model-free dispatcher rediscover the event, launch Desktop when
   needed, and let the existing Sol owner heartbeat claim it directly.
6. Verify one live direct Alex child, exact history, durable resolution, empty
   queue and no active lease.

The full operator procedure is stored in
[`reliability-debugging.md`](../skill-backup/x-twitter-operator/references/reliability-debugging.md).

The stable `stance` field keeps one of four broad classes. `stance_detail`
preserves the exact Sol classification without weakening deterministic
filtering.

## Conversation memory

The same SQLite database stores append-only conversation chains:

- one chain row maps the root X status, short provenance or the legacy `pro`
  compatibility marker, ledger reference, and any legacy ChatGPT conversation
  URL as audit metadata only;
- an Alex reply posted manually is still an `alex` turn. When a later mention
  points to it, the Browser owner must restore that exact live parent and the
  surrounding subtree before drafting, then persist the manual turn before the
  new disposition;
- manual origin provenance remains separate from continuation mode. The claim
  payload deterministically routes a substantive exact parent to the local
  explainer skill while preserving unknown origin as unknown;
- one turn row stores exact public X text, parent status, actor, author, URL,
  timestamps, and provenance;
- source rows map factual replies to the primary sources used to prepare them;
- idempotent imports enrich missing nullable metadata but reject conflicting
  rewrites;
- an append-only chain provenance correction can neutralize an invalid flat
  turn hint without deleting or rewriting the original JSONL record;
- append-only ChatGPT migration records remain historical audit evidence and
  are not runtime dependencies;
- `pro-model-recovery-requeue` requeues legacy model, conversation, and
  screenshot blockers now covered by the local explainer skill, without
  accepting an event ID;
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

- API credentials come from an environment variable or macOS Data Protection
  Keychain. The production helper runs from a signed app-like bundle with a
  Mac provisioning profile and uses
  `AfterFirstUnlockThisDeviceOnly`, so the user LaunchAgent can poll while the
  display is locked after the first login.
- Credentials, SQLite, queue, health, alerts, and logs are excluded from Git.
- An API error never advances `since_id`.
- A duplicate API response never creates a duplicate queue item.
- Dispatcher claims are serialized with a file lock and atomic state replace.
  Queue snapshots and watcher replacements share a separate wake-file lock.
  A live lease prevents duplicate Browser-owner runs. Failure before durable
  resolution removes the claim token for an immediate retry.
- Each production owner claim selects one oldest pending event and uses one
  task-owned X tab. Composer, publication, verification, history import and
  resolve form one short ordered transaction. The multi-event capability is
  retained only for an explicit bounded diagnostic override.
- Resolved events disappear from the wake file and are pruned from dispatcher
  state. An unresolved event becomes eligible again after the lease expires,
  so a crashed Browser-owner turn cannot strand the queue forever.
- The built-in Browser is unavailable in an external app-server runtime.
  Production therefore uses the existing in-app relay and dedicated Codex
  Desktop service worker. The legacy app-server path remains only as a disabled diagnostic
  fallback.
- The dispatcher never launches Desktop for an empty or deferred queue. It
  records the exact PID it starts and never closes a Desktop instance opened
  by Alex.
- A short handoff reservation closes the race between adjacent heartbeat
  ticks before the Browser owner can acquire its global claim.
- A model-free wake request is written before `launchctl kickstart`, then the
  same state file is completed with return code and elapsed time. A failed kick
  leaves the durable queue untouched and the periodic fallback remains active.
- Dispatcher state preserves the start of an unowned relay wait. The doctor
  escalates only after that wait exceeds the versioned recovery limit, while
  resource deferral and an active owner remain valid non-stalled states.
- Queue latency is measured independently from poll and dispatcher heartbeat
  freshness. An unowned oldest event over the versioned SLO is a failure. The
  same event inside an active owner claim is a warning that requires lease and
  durable-completion inspection.
- An owned queue-latency warning is not completion. The bounded oldest-first
  claim and per-event commit rule prevent one long preparation phase from
  hiding every short reply in the same batch.
- The relay uses an atomic reservation and the global owner claim around direct
  delivery. Mobile Remote, local Desktop, and automated Browser work are
  serialized on the same durable thread. The direct follow-up is queued or
  steered by Codex when the owner turn is active.
- A repair handoff never preempts an active X owner lease. The relay checks the
  owner before repair reservation and again after reservation; if X wins the
  race, it releases only the repair reservation and waits for the publication
  transaction to finish.
- An IAB timeout is classified per execution turn. One fresh turn in the same
  Browser-owner task may run the official bootstrap once; publication resumes
  only after an authenticated read-only preflight succeeds.
- `work_in_progress` survives overlapping empty dispatcher checks. `completed`
  requires every claimed ID to leave the wake queue. A postflight warning never
  overwrites a durable resolution with failure. If dispatcher cleanup removes
  the lease after durable resolution but before the owner's terminal call,
  `completed` automatically reconciles only after the wake queue is empty and
  SQLite contains a resolution for every claimed event. It never republishes.
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

Polling, deduplication, queueing, health checks, lease checks, notifications,
CLI update checks, and every terminal idle gate use no model calls. In normal
unattended idle, supervisor-owned Desktop is closed, so the in-app heartbeat
does not run either. A ready queue starts the existing Sol Max owner directly,
without a second model turn for transport. If Alex intentionally keeps Desktop
open, the self-owned heartbeat performs its small scheduled gate while the
terminal dispatcher remains model-free.

## Verification model

The full Python suite protects public compatibility surfaces and domain
invariants. The offline digital twin then explores both the finite state space
and long seeded traces without X, Browser, model calls, or secrets. The current
checkpoint checks 124,416 combinations, of which 77,760 are reachable, and one
million generated steps. Zero counterexamples is evidence only for the modeled
invariants, so it is followed by a read-only live doctor and an address canary
for every deployment-affecting change.
