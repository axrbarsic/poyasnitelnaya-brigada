# X mention watcher

Read-only polling for new X mentions and replies with zero model-token use.

The watcher uses the official X API user mentions endpoint with `since_id`. It
stores immutable event IDs in SQLite, writes a durable pending queue, and
maintains a health file. A separate watchdog detects stale polling and repeated
failures. The official X API still requires an X API Bearer Token.

It does not:

- draft or publish replies;
- access Browser cookies, storage, or credentials;
- wake Codex autonomously;
- store API tokens in files;
- decide whether an event deserves a response.

## Current checkpoint

The live gate passed on 2026-07-24. The poll and watchdog LaunchAgents are
installed for `@axrbarsic`. Polling runs every five minutes and the watchdog
runs every minute. A complete initial review remains a separate gate and must
finish before an empty incremental queue is treated as proof of completeness.

Live polling also requires a positive prepaid X API credit balance. With a
recognized token and no credits, X returns HTTP 402 with the
`credits-depleted` problem type. The watcher reports this as
`billing_blocked` and must remain unloaded until credits are available.

## Configure

Copy `config.example.json` to `config.json`, then set the numeric X user ID.
Provide the bearer token only through one of these environment variables:

- `X_BEARER_TOKEN`
- `X_API_BEARER_TOKEN`
- `TWITTER_BEARER_TOKEN`

For a macOS background installation, prefer a Keychain generic-password item
matching `keychain_service` and `keychain_account` in the config. The watcher
prefers the bundled native Security Framework helper, keeps the secret out of
configuration and logs, and falls back to `/usr/bin/security` only when the
helper is unavailable. Keychain is used only when no supported environment
variable is present.

Compile the helper locally before live use:

```bash
mkdir -p var
xcrun swiftc -framework Security \
  scripts/keychain_helper.swift \
  -o var/keychain-helper
```

Store a token without putting it in process arguments:

```bash
printf '%s' "$X_BEARER_TOKEN" | \
  var/keychain-helper set axrbarsic-x-mention-watcher axrbarsic
```

The helper also supports metadata-only verification:

```bash
var/keychain-helper exists axrbarsic-x-mention-watcher axrbarsic
```

`config.json`, `var/`, SQLite state, health files, queues, and secrets are
excluded from Git.

Verify readiness without printing the token:

```bash
python3 xmention_watcher.py --config config.json preflight
```

## Replay accumulated events

Use disposable replay state. Never reuse its database as the first live cursor,
because fixture IDs intentionally advance `since_id`.

```bash
python3 xmention_watcher.py --config config.example.json poll \
  --fixture tests/fixtures/mentions_accumulated.json
python3 xmention_watcher.py --config config.example.json status
```

Running the same fixture again must report zero new events.

`status` is compact by default so a large backlog does not flood agent
context. Use `status --full` only for a deliberate full queue diagnostic.
Prefer `initial-audit-next` for normal backlog work.

## Live one-shot poll

```bash
python3 xmention_watcher.py --config config.json poll
```

On a new empty live database, the first successful poll stores the available
mention history, establishes `since_id`, and queues every direct reply to the
configured account. It does not silently mark history as handled.

For an existing database created by the older baseline behavior, requeue every
unresolved historical direct reply:

```bash
python3 xmention_watcher.py --config config.json initial-audit-start
python3 xmention_watcher.py --config config.json initial-audit-status
python3 xmention_watcher.py --config config.json initial-audit-next \
  --conversations 1
python3 xmention_watcher.py --config config.json initial-audit-expire \
  --hours 12 --as-of 2026-07-25T10:00:00Z --dry-run
python3 xmention_watcher.py --config config.json initial-audit-expire \
  --hours 12 --as-of 2026-07-25T10:00:00Z
```

Every new response cycle begins with `initial-audit-start`. It requeues only
unresolved direct replies and creates a fresh expiry boundary without changing
durable resolutions. Alex then selects the lookback window for that run. Use
the same explicit
timezone-aware `--as-of` value for the dry-run and apply command. The command
fixes one UTC cutoff at startup and
atomically closes unresolved direct replies whose API `created_at` is strictly
older. It imports the exact stored event text, parent ID, timestamp, URL, and
expanded media into conversation history, records a durable age-policy skip,
and refreshes wake and health once. It never opens Browser, classifies stance,
drafts, or publishes. Events exactly at the cutoff and newer remain queued.
Invalid timestamps, missing attachment metadata, and append-only history
conflicts remain pending and make the command fail closed.

Run expiry once at the start of a response cycle. Do not rerun it while a
reply is being researched or Pro is thinking. After the cutoff is fixed, every
new direct reply belongs to that active cycle until Alex stops it, even if an
earlier target later becomes older than the initial lookback interval.

The Browser owner must open every remaining queued status, inspect the full
reply subtree, classify text and media in context, and durably resolve it.
`initial-audit-next` provides a deterministic newest-first conversation batch
with exact stored event text, parent IDs, attachment media keys, expanded media
URLs, previews, dimensions, type, and alt text when X supplies them. It does
not classify or publish:

```bash
python3 xmention_watcher.py --config config.json resolve STATUS_ID \
  --disposition skip \
  --reason supportive_without_new_claim \
  --stance supportive \
  --stance-detail supportive_confirmation \
  --confidence high \
  --evidence live_thread_parent_verified

python3 xmention_watcher.py --config config.json resolve STATUS_ID \
  --disposition published \
  --reason verified_reply_published \
  --reply-url https://x.com/axrbarsic/status/REPLY_ID \
  --stance opposing \
  --stance-detail opposing_substantive_claim \
  --confidence high \
  --media-meaning "Known reaction meme aimed at the parent claim" \
  --evidence "OCR and visual context"
```

For large batches, the Browser owner can append one
`initial_audit_disposition` ledger record after the exact inspected turn is
written. Root can then import the history and resolve every confirmed handoff
deterministically:

```bash
python3 xmention_watcher.py --config config.json browser-handoff-sync \
  --history-file /absolute/path/to/conversation-history.jsonl \
  --ledger-file /absolute/path/to/run-ledger.jsonl
```

This command never classifies or publishes. It accepts only confirmed direct
replies with `history_status=exact_user_turn_appended` and a pending root
resolution marker. Missing history, mismatched conversation IDs, invalid
resolution fields, and conflicting unresolved handoffs fail closed. Repeated
runs are idempotent. A `published` handoff additionally requires
`alex_history_status=exact_alex_turn_appended` and a matching stored Alex turn
whose parent is the resolved event.

Use `blocked` only for an actionable event whose mandatory workflow dependency
is unavailable. For example, a follow-up to a Pro reply is blocked when its
exact historical ChatGPT conversation URL cannot be recovered and opening a
new conversation would violate the continuity contract. It is not an ordinary
skip. The Browser handoff marker must be
`durable_blocked_pending_root_resolve`, and `blocked` must not include a reply
URL.

If live X proves that Alex already answered before the current audit, use
`skip` with `reply_url=null`; keep the existing Alex URL in evidence and
conversation history. The `reply_url` field belongs only to a `published`
handoff created by the current resolution. Correct a malformed append-only
handoff by appending `supersedes_invalid_handoff=true`, never by editing the old
line.

Never rewrite an old ledger record when scope changes or a contract dependency
is recovered. Append a replacement handoff with
`supersedes_existing_resolution=true` and a non-empty
`resolution_revision_reason`. The sync command records both versions in
`event_resolution_revisions`. It permits only `skip` to `published`, `skip` to
`blocked`, `blocked` to `published`, and `blocked` to `skip`. A published
resolution is terminal. Any implicit rewrite fails closed.

Only complete the initial audit after every direct reply has a durable
`published`, `skip`, or `blocked` resolution and the queue reaches zero:

```bash
python3 xmention_watcher.py --config config.json initial-audit-complete
```

Completion fails closed while any direct reply lacks an `event_resolutions`
row, even if an old client changed its delivery state. `initial-audit-start`
requeues legacy baseline or acknowledged direct replies that have no durable
resolution. The generic `ack` command rejects direct replies, so the X workflow
must use `resolve`. `resolve` itself rejects an event until its exact inspected
turn is present in conversation history. Completion also rejects any legacy
resolution missing that history turn, or any published reply missing its exact
Alex turn and parent link. Later polls use `since_id` and queue only new direct
replies.

Use `stance` for the stable broad class and `stance-detail` for the exact
Sol classification, such as `supportive_confirmation`,
`opposing_substantive_claim`, or `hostile_personal_attack`.
The broad field accepts only `supportive`, `opposing`, `neutral`, or
`ambiguous`; labels such as `corrective` belong in `stance-detail`.

## Watchdog

One-shot health check:

```bash
python3 xmention_watcher.py --config config.json watchdog
```

Continuous shadow watchdog:

```bash
python3 xmention_watcher.py --config config.json watchdog --loop
```

The watchdog is intentionally separate from the poller. If the poller dies, it
cannot report its own death. The watchdog detects an old `last_success_at`,
repeated failures, and a long period with no new events that deserves a manual
cross-check. On macOS it sends a local notification only when health status
changes. New queued X events also produce one local notification per successful
poll that finds new IDs.

The poller uses a process lock, rejects repeated or excessive pagination, and
never advances `since_id` when a request fails. Background stdout and stderr go
to `/dev/null`; durable diagnostics remain in SQLite and the health JSON so log
files cannot grow without bound.

## Conversation history

SQLite stores an append-only conversation graph for both self-authored and Pro
follow-ups. Each turn contains the exact public X text, status ID, parent status
ID, URL, actor, author, provenance, timestamp, and factual source URLs. Pro
chains also retain the exact ChatGPT conversation URL.

Import one JSON object, an array, or JSONL snapshots:

```bash
python3 xmention_watcher.py --config config.json history-import \
  --file /absolute/path/to/conversation-history.jsonl
```

Read the full stored chain before preparing another follow-up:

```bash
python3 xmention_watcher.py --config config.json history-show STATUS_ID
```

Export a canonical reviewable backup after a completed response batch:

```bash
python3 xmention_watcher.py --config config.json history-export \
  --output history-backup/conversation-history.jsonl
```

Imports are idempotent and atomic for the complete input file. A conflicting
rewrite fails closed and rolls back the whole file instead of leaving a partial
history.

Flat turns for an existing chain inherit its stored chain provenance. If an
already appended flat turn supplied an incorrect hint, keep that record and
append one `chain_provenance_correction` metadata record with `chain_id`,
`corrected_provenance`, and `correction_reason`. Import pre-scans the whole file
and applies the correction atomically, so append-only history remains intact.

## Background service

The `macos/` directory contains LaunchAgent templates for a five-minute poll
and a one-minute independent watchdog. They run only Python and macOS system
tools, so idle checks consume no model tokens and create no Codex tasks.

Do not install the LaunchAgents on another machine until a live shadow run with
the official X API has matched a manual Browser scan.

After that gate, render machine-specific plists into a staging directory:

```bash
python3 scripts/render_launchd.py \
  --config /absolute/path/to/config.json \
  --output-dir /absolute/path/to/staging
```

Rendering validates both plists. Loading them with `launchctl` is a separate,
explicit production step.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Skill backup

`skill-backup/x-twitter-operator` is a restorable snapshot of the installed
Codex skill contract that governs detection, Browser ownership, duplicate
checks, Pro continuity, and publication. Runtime state and credentials remain
outside this repository.

Official references:

- https://docs.x.com/x-api/users/get-mentions
- https://docs.x.com/x-api/posts/timelines/quickstart/user-mention-quickstart
- https://docs.x.com/x-api/fundamentals/rate-limits
- https://docs.x.com/x-api/getting-started/pricing
