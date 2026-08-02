# X mention watcher

## Purpose

Use `/Users/alexlane/Developer/x-mention-watcher` for deterministic polling of
new X replies involving `@axrbarsic`. Polling and watchdog execution consume no
model tokens and create no Codex tasks. Official X API resource charges still
apply.

The watcher is read-only. It detects events and maintains state. It never
drafts, posts, deletes, likes, follows, or changes X account state.

## Security contract

- Keep the Bearer Token only in macOS Keychain under service
  `axrbarsic-x-mention-watcher` and account `axrbarsic`.
- Prefer `var/keychain-helper`, installed by
  `scripts/install_keychain_helper.sh`, over the `security` CLI. The helper
  must resolve inside its signed app-like bundle and carry a valid Mac
  provisioning profile for its Keychain access group.
- Never print, log, screenshot, hash, measure, or place the token in argv.
- `config.json`, `var/`, databases, queues, health files, and secrets are
  ignored by Git.

## Queue contract

- The first successful live poll establishes `since_id` and queues every
  available direct reply for an initial audit.
- Never treat a fresh cursor or an empty incremental queue as proof that old
  replies were handled.
- After the initial audit starts, queue every direct reply whose
  `in_reply_to_user_id` equals `16337609`.
- Also queue a nested reply when its `conversation_id` has an exact stored
  `conversation_turns` row with `actor=alex`.
- When `mandatory_response_mode=true`, queue every reply returned by the
  authenticated mentions endpoint. The immediate parent may be another
  participant and an older local chain may be missing its Alex root turn.
- When `conversation_tail_enabled=true`, run a bounded recent search only for
  recent conversation IDs that contain an exact stored Alex turn. Queue nested
  replies even when they address another participant and do not repeat
  `@axrbarsic`.
- Outside mandatory mode, store unrelated mentions and untracked nested replies
  as `ignored`.
- A post authored by the configured `user_id` is an exact Alex turn, never an
  inbound event. Import its exact text and chain metadata with `actor=alex`,
  set `delivery_state=self_authored`, and never queue or resolve it.
- Never use static topic names, post IDs, authors, or special article lists as
  an eligibility rule.
- Deduplicate by immutable X event ID.
- A successful mentions poll alone may advance `since_id`. Conversation tail
  search keeps an independent success timestamp and never advances the
  mentions cursor. Failed polls never advance either boundary.
- The legacy `baseline` command is disabled because it could hide unresolved
  history. Use `initial-audit-start` to requeue unresolved direct replies.

## Operational commands

```bash
cd /Users/alexlane/Developer/x-mention-watcher
python3 xmention_watcher.py --config config.json preflight
python3 xmention_watcher.py --config config.json poll
python3 xmention_watcher.py --config config.json status
python3 xmention_watcher.py --config config.json self-authored-reconcile
python3 xmention_watcher.py --config config.json initial-audit-next \
  --conversations 1
python3 xmention_watcher.py --config config.json initial-audit-expire \
  --hours 12 --as-of 2026-07-25T10:00:00Z --dry-run
python3 xmention_watcher.py --config config.json initial-audit-expire \
  --hours 12 --as-of 2026-07-25T10:00:00Z
python3 scripts/commit_browser_owner_event.py --config config.json \
  --claim-token CLAIM_TOKEN \
  --event-dir var/evidence/browser-owner/CLAIM_TOKEN/EVENT_ID
python3 scripts/finalize_browser_owner_session.py --config config.json \
  --session-dir var/evidence/browser-owner/CLAIM_TOKEN
python3 xmention_watcher.py --config config.json commenter-history EVENT_ID \
  --limit 50
python3 candidate_corpus.py --config config.json history EVENT_ID --limit 20
python3 xmention_watcher.py --config config.json watchdog
```

`status` is compact by default. Do not use `status --full` for routine backlog
work because it can flood model context. Use `initial-audit-next` instead.

`self-authored-reconcile` idempotently changes legacy unresolved rows authored
by the configured `user_id` to `delivery_state=self_authored`. It creates no
`event_resolutions` row, performs no X mutation, and preserves the exact
imported Alex turn for later conversation continuity.

Never use `ack` for a direct reply. The watcher rejects that path. Use
`resolve EVENT_ID --disposition published|skip|blocked` so every removal has a
durable reason and verified reply URL when applicable. `blocked` is reserved
for an actionable event whose mandatory workflow dependency is unavailable.

With `mandatory_response_mode=true`, `skip` is not a content classification.
It is accepted only when exact history proves an existing direct Alex child
reply for that event. A terminal `blocked` resolution requires an allowed
`blocker_code`. Temporary Browser, generation, rate, and validation failures
stay queued for retry.

## Installation gate

Before loading LaunchAgents, require all of the following:

1. Unit tests and plist validation pass.
2. A live poll succeeds with the Keychain token.
3. Repeated live polls return no duplicate IDs.
4. A temporary invalid environment token causes the configured failure state.
5. A following Keychain poll restores healthy state.
6. Manual Browser comparison finds no missed direct replies.
7. X API credits are positive and the spending cap is understood.

The installed poll and independent watchdog intervals are both 60 seconds.
Conversation tail search can use the same interval while limiting the first
lookback, active conversation window, overlap and maximum conversation count.
All sources deduplicate by immutable event ID. Background stdout and stderr go
to `/dev/null`.

If health is `billing_blocked`, do not keep polling. Restore X API credits
before loading or restarting the LaunchAgents.

## Event handoff

When the queue becomes non-empty:

1. The Browser owner opens each exact X event URL.
2. It performs the full thread, context, duplicate, and safety checks from this
   skill.
3. It reads the complete stored chain with `history-show STATUS_ID` and checks
   the live X thread for any missing or changed context.
4. It reads `commenter_memory` from the claim. When a prior public turn may
   reveal a contradiction, changed criterion, double standard, or repeated
   claim, it may use `commenter-history EVENT_ID --limit N` for deeper exact
   history across other stored conversations.
5. It checks the ledger for the parent publication type and generation
   provenance.
6. A follow-up to a prior local Sol Max reply continues through the local
   explainer skill with the complete exact X history.
7. A follow-up to a short reply is classified by Sol using the complete
   ordered X chain, exact earlier reply text, and prior source URLs.
8. Before any `resolve`, including `skip`, append and import the exact inspected
   user turn from live X with parent, author, timestamps, canonical URL,
   unnormalized text, and `media_json`. An empty `media_json` is valid only
   after live inspection confirms no media. For a verified publication, append
   and import the exact new Alex turn as well. Flat
   `initial_audit_event_turn` records must include `chain_provenance` when a
   chain is new. Existing chains inherit their stored provenance.
9. Call `resolve` only after an exact already-answered proof, terminal contract
   blocker, or verified publication is durably recorded. Store the stable
   broad class in `stance` and the exact Sol label in `stance-detail`. Do not
   call `ack`.

The broad `stance` field accepts only `supportive`, `opposing`, `neutral`, or
`ambiguous`. Descriptions such as `corrective`, `hostile`, `sarcastic`, or
`supportive_then_corrective` belong in `stance_detail`. When a user correctly
narrows or corrects Alex's claim, use broad `neutral` unless the complete chain
clearly supports another stable class.

For a claimed event, the Browser owner writes one structured terminal
`evidence.json`. It does not choose a route or write JSONL. The deterministic
`commit_browser_owner_event.py` command validates the record, derives exactly
one route from the stored event, generates both event JSONL files, imports the
exact history, and applies the durable resolution. The generated handoff
contains exactly one proven route:

- `direct_reply_to_axrbarsic=true`; or
- `tracked_conversation_reply=true`; or
- `mention_reply_to_axrbarsic=true`;
- `history_status=exact_user_turn_appended`;
- `alex_history_status=exact_alex_turn_appended` for a publication;
- a pending root resolution marker;
- exact `event_id` and `conversation_id`;
- `disposition`, `reason`, broad `stance`, `stance_detail`, `confidence`,
  evidence, media meaning when applicable, and verified `reply_url` for a
  publication.

The target in `evidence.json` must match the stored API event by exact text,
author ID, parent status ID and conversation ID. A published
`generation_profile=local_sol_max` must name
`generation_skill=poyasnitelnaya-brigada-v2`; `sol_short` names no skill;
`commenter_requested_image` names skill `imagegen`; `satirical_377` names skill
`377`; and `lozhkin_web` is valid only after Alex explicitly authorizes that
web visual bot. Every visual profile must preserve the generated image file,
SHA-256, MIME, `composer_attachment_verified=true`, and exact official
`reply.media`. Every profile keeps the owner on `gpt-5.6-sol` with effort
`max`. ChatGPT web is forbidden outside `lozhkin_web`.

Live route classification is persisted separately in
`var/inbound-route-control.json`. It is scheduling state, not a resolution and
not conversation history. In `simple-wave` mode, known `local-max` events stay
queued but are excluded from the cleanup snapshot. Known non-v2 events are
selected before unclassified events. When the snapshot has no unfinished
ordinary replies, `normal` resumes automatically and every deferred local-max
event becomes eligible without replay or database edits.

For an existing conversation, omit `chain_provenance` unless it is needed for
human-readable evidence. The committer always canonicalizes that field from
the durable `conversation_chains` row before history import. A supplied value
is used only when the conversation has no stored chain yet, and it must be one
of `short`, `pro`, or `mixed`.

Missing history, a mismatched chain, an invalid field, or conflicting canonical
evidence blocks the commit. Repeating the same commit is idempotent. Before the
claim manifest exists, per-event history and ledger JSONL are generated
projections: after a successful idempotent SQLite sync, the committer repairs a
stale projection from `evidence.json`. Never edit those JSONL files by hand. A
publication also blocks unless its verified reply URL resolves to an imported
Alex turn whose parent is the inspected event. Do not call
`browser-handoff-sync`, history import, or resolve directly in the
Browser-owner workflow.

For canonical runtime evidence, the same successful sync must also return a
complete `evidence_manifest`. It atomically creates and self-audits
`manifest.json` only after every handoff in that evidence directory is durably
resolved. Do not create runtime manifests by hand.

For a multi-event autopilot claim, write each event's `evidence.json` below
`var/evidence/browser-owner/CLAIM_TOKEN/EVENT_ID/` and immediately run the
event committer. Continue only after `status=committed`. Before `completed`,
run `finalize_browser_owner_session.py` exactly once for the claim directory.
It validates that the event directories exactly match the active claim,
builds the two stable aggregate JSONL files atomically from generated records,
runs the idempotent handoff sync, and requires a complete immutable manifest.
Do not rediscover this flow from source code or assemble JSONL manually.

When live X shows that Alex already answered an event before the current audit,
record the audit disposition as `skip` with `reply_url=null`,
`existing_alex_reply_url=<canonical URL>`, and
`alex_history_status=exact_alex_turn_appended`. Import the exact direct Alex
child turn first. The `reply_url` field is reserved for a `published`
disposition created by the current resolution handoff. If an invalid skip
already contains a reply URL, append a corrected handoff with
`supersedes_invalid_handoff=true`; never rewrite the old line.

Historical ChatGPT Pro conversation URLs are no longer runtime dependencies.
Restore the exact X chain from SQLite and append-only JSONL, then continue
through the local `poyasnitelnaya-brigada-v2` skill by default. Keep
`poyasnitelnaya-brigada` v1 unchanged for explicit requests only. Keep old
ChatGPT URLs as audit
metadata. Do not create a replacement ChatGPT conversation.

If Alex explicitly expands the response scope or a blocked dependency is later
recovered, keep the old ledger entry unchanged. Append a replacement handoff
with `supersedes_existing_resolution=true` and a precise
`resolution_revision_reason`. Synchronization may auditably revise `skip` to
`published`, `skip` to `blocked`, `blocked` to `published`, or `blocked` to
`skip`. A published resolution is terminal. Never use this mechanism to
silently rewrite a classification, remove a publication, or bypass the normal
duplicate checks.

For the historical backlog, use `initial-audit-next --conversations 1` to get
the next deterministic newest-first conversation group with exact stored text
and parent IDs. For new events it also exposes any expanded media metadata and
alt text returned by X. This command is mechanical only. Missing media fields
are not proof that an event has no media, and it does not replace the live
Browser subtree, media review, or Sol classification.

Begin each explicitly requested response cycle with `initial-audit-start`.
This creates a fresh cycle boundary while preserving every durable resolution.
Alex then selects the initial lookback, for example
`--hours 3` or `--hours 12`. Use the same explicit value for dry-run and
apply, together with one identical timezone-aware `--as-of` timestamp. The
command fixes one UTC cutoff when it starts and closes only
unresolved direct events with `created_at < cutoff`. It imports an exact user
history turn from the stored API payload and records an auditable age-policy
skip. It never invokes Browser or a model and never publishes. Events at the
cutoff or newer remain queued. Missing or naive timestamps, future timestamps,
missing expanded attachment metadata, invalid stored payloads, and append-only
history conflicts stay pending.

Run expiry exactly once when a cycle begins. After that, process every new
direct reply and preserve active short or local Sol Max chains until resolved.
Never rerun expiry to age out an event that is queued, being researched, or
waiting for local Sol Max generation.

While a historical audit is still running, every newly polled direct reply is
a hot-wave priority. After completing each live conversation branch, call
`initial-audit-next` again before opening another old branch. Process newly
created events first and preserve their exact parent chain so a fast dialogue
does not become stale. A queued local Sol Max event that has not passed local
generation and validation must remain unresolved, but it must not prevent
processing other fresh short or already-answered events returned in the same
newest-first batch.

## Initial audit

Before relying on incremental monitoring:

1. Run `initial-audit-start` to requeue unresolved direct replies stored by an
   older baseline build.
2. Apply Alex's requested `--hours H` once: choose one timezone-aware
   `--as-of`, reuse it for dry-run and apply, and verify the fixed UTC cutoff.
   Do not open or publish replies older than that initial cutoff.
3. Review every remaining queued event in the complete live X subtree.
4. Expand reply subtrees and inspect media-only replies even when `tweetText`
   is empty.
5. Classify media relative to its exact parent using OCR, visual meaning,
   nearby author replies, and internet research for unfamiliar memes.
6. Use `resolve` with a durable reason and verified reply URL when applicable.
   If a mandatory contract dependency is unavailable, use `blocked`, never
   `skip`, and preserve the exact recovery evidence.
7. Run `initial-audit-complete` only after the queue reaches zero and every
   direct reply has an `event_resolutions` row. Completion checks both facts.

The incremental one-minute watcher is authoritative only after this gate.

## Archive and candidate memory

Use `x_archive_import.py` only for Alex's official X archive. Dry-run first,
verify that the archive numeric account ID matches `config.json`, confirm
`direct_messages_imported=0`, then apply. Never commit the ZIP, extracted
archive, report, or runtime SQLite database.

Use `candidate_corpus.py` only for external public-post corpora. Every import
is quarantined as `unverified_candidate` and matched by stable subject X user
ID. It may guide a live search but cannot support a quote or factual claim.
After opening the exact live X post or reading the official X API record, use
the `verify` subcommand to append the exact observed text. Never rewrite the
candidate record to make it match.

The autopilot receives no more than three compact candidate hints for one
event. A hint is usable as evidence only when `usable_as_evidence=true`.

Without standing authority, do not wake a model merely because polling
occurred. Use the durable queue and local notification as the boundary between
free mechanical detection and model work.

## Standing autopilot

Enable this mode only after Alex explicitly grants continuing publication
authority for queued eligible replies.

1. Keep the Python watcher read-only and token-free.
2. Run poll, watchdog, and event dispatcher LaunchAgents every
   minute.
3. Let the event dispatcher run `autopilot_bridge gate` without a model.
4. Only for `dispatch=true`, launch Codex Desktop when it is absent. The
   existing self-owned heartbeat is attached directly to the dedicated Sol Max
   owner task and calls one `relay-reserve-handoff`. Python chooses rotation,
   repair, inbound X, or idle-only outbound and returns one unambiguous
   `dispatch` plus `route`. The same task executes the corresponding claim;
   no cross-task message or process-local `hostId` is involved.
5. If the heartbeat fails before a repair or X claim starts, run the matching
   `release-handoff` command with the exact reservation token and finish
   without Browser. Leave the durable queue pending. An outbound route already
   owns a slot claim and must use its exact failure or pause command instead.
6. Let the same Sol Max owner task atomically claim the queue with
   `scripts/autopilot_bridge.py claim`.
7. Run the mechanical claim before loading X skills and references. If the
   queue is empty, another global owner is active, or memory is deferred, use
   no Browser, no `list_threads`, and no task archival. Finish normally before
   opening Browser. Never self-archive the current active run.
8. For eligible IDs, atomically record one global 30-minute owner lease before
   Browser work. Renew the exact claim every 15 minutes during a long run.
   Renewal must preserve the original event set, while newly arriving events
   wait for the next owner.
9. Include only eligible event IDs, canonical URLs, local state paths, and the
   standing workflow contract. The owner must read exact history from
   SQLite and the append-only ledger.
10. Mark the claim `started`, execute the returned prompt in the same Sol turn,
   and remove the lease only if work fails before durable resolution.
11. Claim at most the configured bounded oldest-first batch, currently three
    events. The resource profile caps independent read-only X tabs before
    preflight. Composer, publication, verification, history import and resolve
    remain one ordered writer lane, with immediate durable commit after each
    event. A local Sol Max route opens no ChatGPT tab.
12. Never let the watcher, dispatcher, or relay publish. Sol must perform
    live context inspection, fact checking, duplicate prevention, routing,
    composer validation, publication, URL verification, history storage, and
    durable resolution.
13. Treat the lease as crash recovery, not permission to post twice. Every Sol
    turn still runs live X and ledger duplicate checks before composer fill.
14. Never perform Browser work in Codex CLI or the external app-server
    fallback. Browser belongs to the single self-owned Codex Desktop task.
15. After durable history and resolution remove every claimed ID from the wake
    queue, mark `completed`. If a later API poll cannot read Keychain, mark
    `completed_with_warning`; do not release or republish resolved events.
16. Keep the owner turn free of a final X poll. The one-minute
    LaunchAgent owns token-free polling.
17. Close all task-owned Browser tabs and finish normally for every terminal
    outcome. Do not archive the persistent current owner. Transactional owner
    rotation archives only the exact retired owner after verified commit.
18. After a Codex restart, a changed runtime ID may immediately reclaim an old
    owner. In the same runtime an unfinished owner remains protected until its
    lease expires, then the next claim atomically reclaims the queue. A fresh
    `notLoaded` status is not sufficient.
19. When the queue becomes empty, close only the exact Desktop PID launched by
    the supervisor after its configured grace period. Never close a
    user-started Desktop instance.
20. A locked display is not a Browser blocker while macOS is awake. Active
    voice or system sleep defers Browser work and leaves every event durable.

Do not retire the paused fallback automation until the installed dispatcher
LaunchAgent proves one complete live cycle. Repeated live self-owned heartbeat
runs completed the full route, and the reservation suppressed the observed
adjacent-heartbeat race without cross-task delivery.

## Mandatory response reconciliation

After enabling mandatory response mode, run a target-agnostic reconciliation:

```bash
python3 xmention_watcher.py --config config.json mandatory-response-requeue \
  --hours 12 --as-of 2026-07-25T20:00:00Z --dry-run
python3 xmention_watcher.py --config config.json mandatory-response-requeue \
  --hours 12 --as-of 2026-07-25T20:00:00Z
```

Use the same `--as-of` for dry-run and apply. The command receives no event ID.
It selects recent eligible `skip` resolutions and previously ignored mention
replies with no exact direct Alex child, records the prior state in
`response_policy_requeues`, and returns them to the ordinary wake queue. The
model-free dispatcher then launches Desktop when needed, and the existing
self-owned heartbeat discovers the work from the same durable queue contract
as any new event. Existing publications and proven already-answered events
remain untouched.

When diagnosing a reported miss, do not manually queue its known ID. Fix the
universal eligibility rule, run the target-agnostic reconciliation, and let the
ordinary dispatcher and self-owned Sol owner discover and process it. See
[reliability-debugging.md](reliability-debugging.md).

## Conversation history commands

Use the watcher SQLite database as the source of truth:

```bash
cd /Users/alexlane/Developer/x-mention-watcher
python3 xmention_watcher.py --config config.json history-show STATUS_ID
python3 xmention_watcher.py --config config.json history-import \
  --file /absolute/path/to/snapshot.jsonl
python3 xmention_watcher.py --config config.json history-export \
  --output var/exports/conversation-history.jsonl
```

The import is idempotent and append-only. A different exact text, URL, actor,
chain mapping, provenance, or ChatGPT conversation URL for an existing record
is a blocker. Never resolve a conflict by overwriting the database. Reopen the
live X thread and ledger, determine which input is wrong, and import a corrected
snapshot.

If only a flat turn's chain provenance hint was wrong, do not edit the old
line. Append one metadata record:

```json
{"snapshot_type":"chain_provenance_correction","chain_id":"STATUS_ID","corrected_provenance":"short","correction_reason":"Exact reason"}
```

Use the legacy database enum `short`, `pro`, or `mixed` only after checking the
canonical chain. Here `pro` is a compatibility value for the local Sol Max
route, not a ChatGPT model. The importer pre-scans this correction and still
rolls back the entire file on any remaining conflict.

After every completed response batch:

1. Export canonical history JSONL.
2. Verify no token, cookie, Browser storage, or private credential is present.
3. Commit the code, skill backup, and history export to the private GitHub
   repository.
