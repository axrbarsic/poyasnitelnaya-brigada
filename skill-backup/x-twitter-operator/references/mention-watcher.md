# X mention watcher

## Purpose

Use `/Users/alexlane/Developer/x-mention-watcher` for deterministic polling of
new direct replies to `@axrbarsic`. Polling and watchdog execution consume no
model tokens and create no Codex tasks. Official X API resource charges still
apply.

The watcher is read-only. It detects events and maintains state. It never
drafts, posts, deletes, likes, follows, or changes X account state.

## Security contract

- Keep the Bearer Token only in macOS Keychain under service
  `axrbarsic-x-mention-watcher` and account `axrbarsic`.
- Prefer `var/keychain-helper`, compiled from
  `scripts/keychain_helper.swift`, over the `security` CLI.
- Never print, log, screenshot, hash, measure, or place the token in argv.
- `config.json`, `var/`, databases, queues, health files, and secrets are
  ignored by Git.

## Queue contract

- The first successful live poll establishes `since_id` and queues every
  available direct reply for an initial audit.
- Never treat a fresh cursor or an empty incremental queue as proof that old
  replies were handled.
- After the initial audit starts, only direct replies whose
  `in_reply_to_user_id` equals
  `16337609` enter the queue.
- Other mentions and nested replies are stored as `ignored`.
- Deduplicate by immutable X event ID.
- A successful poll alone may advance `since_id`. Failed polls never do.
- The legacy `baseline` command is disabled because it could hide unresolved
  history. Use `initial-audit-start` to requeue unresolved direct replies.

## Operational commands

```bash
cd /Users/alexlane/Developer/x-mention-watcher
python3 xmention_watcher.py --config config.json preflight
python3 xmention_watcher.py --config config.json poll
python3 xmention_watcher.py --config config.json status
python3 xmention_watcher.py --config config.json initial-audit-next \
  --conversations 1
python3 xmention_watcher.py --config config.json initial-audit-expire \
  --hours 12 --as-of 2026-07-25T10:00:00Z --dry-run
python3 xmention_watcher.py --config config.json initial-audit-expire \
  --hours 12 --as-of 2026-07-25T10:00:00Z
python3 xmention_watcher.py --config config.json browser-handoff-sync \
  --history-file /absolute/path/to/conversation-history.jsonl \
  --ledger-file /absolute/path/to/run-ledger.jsonl
python3 xmention_watcher.py --config config.json watchdog
```

`status` is compact by default. Do not use `status --full` for routine backlog
work because it can flood model context. Use `initial-audit-next` instead.

Never use `ack` for a direct reply. The watcher rejects that path. Use
`resolve EVENT_ID --disposition published|skip|blocked` so every removal has a
durable reason and verified reply URL when applicable. `blocked` is reserved
for an actionable event whose mandatory workflow dependency is unavailable.

## Installation gate

Before loading LaunchAgents, require all of the following:

1. Unit tests and plist validation pass.
2. A live poll succeeds with the Keychain token.
3. Repeated live polls return no duplicate IDs.
4. A temporary invalid environment token causes the configured failure state.
5. A following Keychain poll restores healthy state.
6. Manual Browser comparison finds no missed direct replies.
7. X API credits are positive and the spending cap is understood.

The installed poll interval is 300 seconds. The independent watchdog interval
is 60 seconds. Both write durable state and send local notifications only on
meaningful changes. Background stdout and stderr go to `/dev/null`.

If health is `billing_blocked`, do not keep polling. Restore X API credits
before loading or restarting the LaunchAgents.

## Event handoff

When the queue becomes non-empty:

1. The Browser owner opens each exact X event URL.
2. It performs the full thread, context, duplicate, and safety checks from this
   skill.
3. It reads the complete stored chain with `history-show STATUS_ID` and checks
   the live X thread for any missing or changed context.
4. It checks the ledger for the parent publication type and historical
   ChatGPT conversation URL.
5. A follow-up to a prior Pro reply continues in that exact conversation with
   one screenshot and zero text.
6. A follow-up to a short reply is classified by Sol High using the complete
   ordered X chain, exact earlier reply text, and prior source URLs.
7. Before any `resolve`, including `skip`, append and import the exact inspected
   user turn from live X with parent, author, timestamps, canonical URL,
   unnormalized text, and `media_json`. An empty `media_json` is valid only
   after live inspection confirms no media. For a verified publication, append
   and import the exact new Alex turn as well. Flat
   `initial_audit_event_turn` records must include `chain_provenance` when a
   chain is new. Existing chains inherit their stored provenance.
8. Call `resolve` only after a skip, contract blocker, or verified publication
   is durably recorded. Store the stable broad class in `stance` and the exact
   Sol label in `stance-detail`. Do not call `ack`.

The broad `stance` field accepts only `supportive`, `opposing`, `neutral`, or
`ambiguous`. Descriptions such as `corrective`, `hostile`, `sarcastic`, or
`supportive_then_corrective` belong in `stance_detail`. When a user correctly
narrows or corrects Alex's claim, use broad `neutral` unless the complete chain
clearly supports another stable class.

For a large audit, the Browser owner may append a structured
`initial_audit_disposition` after the exact event turn. Root may then use
`browser-handoff-sync` instead of manually repeating each `resolve`. The
sync command only transfers Sol's durable decision. It does not inspect,
classify, draft, or publish. Every handoff must contain:

- `direct_reply_to_axrbarsic=true`;
- `history_status=exact_user_turn_appended`;
- `alex_history_status=exact_alex_turn_appended` for a publication;
- a pending root resolution marker;
- exact `event_id` and `conversation_id`;
- `disposition`, `reason`, broad `stance`, `stance_detail`, `confidence`,
  evidence, media meaning when applicable, and verified `reply_url` for a
  publication.

Missing history, a mismatched chain, an invalid field, or conflicting
unresolved handoffs blocks synchronization. Repeated synchronization is
idempotent. A publication also blocks unless its verified reply URL resolves to
an imported Alex turn whose parent is the inspected event.

When live X shows that Alex already answered an event before the current audit,
record the audit disposition as `skip` with `reply_url=null`. Preserve the
existing Alex URL in `evidence` and exact conversation history. The `reply_url`
field is reserved for a `published` disposition created by the current
resolution handoff. If an invalid skip already contains a reply URL, append a
corrected handoff with `supersedes_invalid_handoff=true`; never rewrite the old
line.

If a required historical Pro conversation URL cannot be recovered, do not
create a replacement conversation and do not classify the event as an ordinary
skip. Append a `blocked` handoff with
`watcher_disposition=durable_blocked_pending_root_resolve`, exact history,
evidence of the recovery attempt, and no reply URL.

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
Browser subtree, media review, or Sol High classification.

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
direct reply and preserve active short or Pro conversations until resolved.
Never rerun expiry to age out an event that is queued, being researched, or
waiting for Pro.

While a historical audit is still running, every newly polled direct reply is
a hot-wave priority. After completing each live conversation branch, call
`initial-audit-next` again before opening another old branch. Process newly
created events first and preserve their exact parent chain so a fast dialogue
does not become stale. A queued Pro event in `thinking` or
`unverified_background_pending` state must remain unresolved, but it must not
prevent processing other fresh short or skip events returned in the same
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

The incremental five-minute watcher is authoritative only after this gate.

Without standing authority, do not wake a model merely because polling
occurred. Use the durable queue and local notification as the boundary between
free mechanical detection and model work.

## Standing autopilot

Enable this mode only after Alex explicitly grants continuing publication
authority for queued direct replies.

1. Keep the Python watcher read-only and token-free.
2. Run the read-only `scripts/autopilot_dispatch.py snapshot` from one Codex
   scheduled task every five minutes using Luna with low reasoning.
3. In a projectless sandbox, keep `leased_event_ids`, `lease_expires_at`, and
   `last_delivery_at` in that automation's persistent `memory.md`.
4. Let Luna read only the compact snapshot JSON. It must not open Browser, X, or
   ChatGPT and must not research, classify, draft, or publish.
5. If every pending ID has an active lease, finish without sending a message.
6. For eligible IDs, record a 30-minute lease before sending one wake message
   to the exact existing pinned
   Browser-owner task and override that turn to Sol High.
7. Include only eligible event IDs, canonical URLs, and the standing workflow
   contract in the wake message. Never create a new Codex task per event.
8. If wake delivery fails, remove the newly leased IDs. Otherwise keep the
   lease until the Browser owner resolves the events or 30 minutes pass.
9. Use one X tab, one ChatGPT tab, and at most one active Pro conversation on
   Alex's 8 GB iMac.
10. Never let Luna or the watcher publish. Sol High must perform live context
   inspection, fact checking, duplicate prevention, routing, composer
   validation, publication, URL verification, history storage, and durable
   resolution.
11. Treat the lease as crash recovery, not permission to post twice. Every Sol
    turn still runs live X and ledger duplicate checks before composer fill.

## Conversation history commands

Use the watcher SQLite database as the source of truth:

```bash
cd /Users/alexlane/Developer/x-mention-watcher
python3 xmention_watcher.py --config config.json history-show STATUS_ID
python3 xmention_watcher.py --config config.json history-import \
  --file /absolute/path/to/snapshot.jsonl
python3 xmention_watcher.py --config config.json history-export \
  --output history-backup/conversation-history.jsonl
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

Use `short`, `pro`, or `mixed` only after checking the canonical chain. The
importer pre-scans this correction and still rolls back the entire file on any
remaining conflict.

After every completed response batch:

1. Export canonical history JSONL.
2. Verify no token, cookie, Browser storage, or private credential is present.
3. Commit the code, skill backup, and history export to the private GitHub
   repository.
