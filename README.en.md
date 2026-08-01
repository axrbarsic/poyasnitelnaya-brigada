# Пояснительная бригада

[Русский](README.md) | [English](README.en.md)

<p align="center">
  <img src="docs/assets/poyasnitelnaya-brigada-logo.jpg"
       alt="Пояснительная бригада logo"
       width="160">
</p>

Reliable X reply autopilot with token-free detection, durable conversation
memory, Sol Max local-skill reasoning, and verified Browser publication.

> Deployment status on 2026-08-01: the inbound terminal-first path runs through
> the self-owned heartbeat of one persistent Sol Max Browser owner. The explainer route
> moved from the custom GPT to a local versioned skill. Generation no longer
> opens ChatGPT and instead uses Sol Max, exact SQLite history, and a
> deterministic 4000 Unicode code-point upper bound with no length padding.
> Scheduled outbound reuses that heartbeat only when the inbound queue is empty
> and the writer is idle. The standalone local `x-15` cron remains paused.

## License and attribution

The project is published under the permissive [MIT License](LICENSE). You may
clone, fork, modify, redistribute, and use it in other projects, including
commercial work. Copies and substantial portions must retain the copyright
notice and license text.

Author: [Alex Lane](https://x.com/axrbarsic), project
Poyasnitelnaya Brigada. A machine-readable [`CITATION.cff`](CITATION.cff) is
included for catalogs and citations.

## What this project is

This repository is a reliable bridge between X reply detection and a local
Codex Desktop Browser worker. It separates cheap mechanical work from expensive
content decisions:

- Python and the official X API detect, deduplicate, persist, and queue replies.
- After new event IDs are durably queued, the watcher performs a non-killing
  `launchctl kickstart` of the local dispatcher. The minute LaunchAgent remains
  an independent fallback, so a failed kick loses no work and creates no
  second owner.
- A token-free Python dispatcher leases only eligible events.
- A minute LaunchAgent runs the read-only gate in plain Python. Empty, busy,
  and resource-deferred queues use no model, open no Browser, and create no
  Codex task.
- Only a ready queue checks Codex Desktop. The terminal supervisor starts the
  canonical workspace when Desktop is closed. Launch failure keeps every event
  pending and raises a throttled local alert.
- One existing in-app heartbeat is attached directly to the Sol Max owner and
  calls `reserve-handoff`. Python selects one durable route, then the same task
  executes the matching claim. There is no cross-thread send, relay task, or
  process-local host dependency. A TTL reservation and global owner claim block
  adjacent heartbeat runs from creating concurrent Browser owners.
- A model-free LaunchAgent archives completed service runs and recovers stale
  owner claims without creating another Codex task.
- The supervisor may stop only a Desktop process that it launched itself, and
  only after the queue is empty, the owner lease is gone, and a grace period
  expires. It never closes a user-opened Desktop.
- The Sol Max owner opens the real X thread in the authenticated Codex
  Browser, checks context and sources, prevents duplicates, and publishes one
  response per eligible event.
- The dispatcher accepts success only after every dispatched event ID leaves
  the durable queue. A released unresolved claim is a failure.
- A reply posted manually by Alex is still an `alex` turn. When someone
  continues that branch, the owner restores the manual parent and full live
  context, persists them, and only then prepares the next reply.
- Explainer targets and follow-ups run locally through
  `poyasnitelnaya-brigada-v2` by default in Sol Max with exact durable history.
  V1 remains unchanged and runs only when Alex explicitly requests it.
- A local explainer reply is non-empty, contains at most 4000 Unicode code
  points, does not target the limit, and passes deterministic source and
  composer validation before publication.
- SQLite and append-only JSONL preserve conversation history and audit evidence.

Start with [the autopilot setup guide](docs/autopilot-setup.md) to adapt the
system to another X account and Codex task. The
[local explainer skill contract](docs/local-explainer-skill.md) documents the
Sol Max, maximum-length, history, and recovery rules.

The complete executable project uses one canonical directory. See
[the project layout contract](docs/project-layout.md) and
[the storage and backup contract](docs/storage-and-backup.md). The
[8 GB memory operations guide](docs/memory-operations.md) documents the global
owner lease, resource guard, Browser lifecycle, and service-task archival.

Import legacy Browser-owner evidence into the canonical ignored `var/evidence`
tree without modifying the source:

```bash
python3 evidence_import.py \
  --source /absolute/path/to/legacy-work \
  --label legacy-browser-owner-YYYY-MM-DD
python3 evidence_import.py \
  --audit var/evidence/browser-owner/legacy-browser-owner-YYYY-MM-DD
```

The importer rejects symlinks and likely credential material, copies through a
temporary directory, verifies SHA-256 for every file, and atomically publishes
one manifested evidence tree. Its CLI always writes to the canonical
`var/evidence/browser-owner` tree and exposes no arbitrary output path.

The watcher uses the official X API user mentions endpoint with `since_id`.
It can also run a bounded recent search over recent conversation IDs that
contain an exact Alex turn. This closes the source gap for nested replies that
continue the discussion without repeating `@axrbarsic`. The two sources keep
independent cursors. The watcher stores immutable event IDs in SQLite, writes a
durable pending queue, and maintains a health file. A token-free supervisor
detects stale polling and contract failures. It performs one allowlisted poll
kickstart, then creates one deduplicated durable incident for the existing Sol
Max owner if the failure persists. The official X API still requires an X API
Bearer Token.

It does not:

- draft or publish replies;
- access Browser cookies, storage, or credentials;
- store API tokens in files;
- make content-based skip decisions.

When Alex explicitly grants standing autopilot authority, a model-free
LaunchAgent checks the durable queue. Only a ready queue starts Desktop when
needed. One existing in-app heartbeat belongs to the persistent Sol Max Browser
owner. It reserves and claims work in the same durable task. Mobile Remote,
local Desktop, and the automated Browser owner use that thread in order, while
the global owner claim serializes publication. Sol remains the only publication
brain, and the local explainer route always runs with Max reasoning.

With `mandatory_response_mode=true`, every eligible available event inside the
requested lookback receives exactly one reply. Support, sarcasm, jokes, insults,
memes, and content-free reactions are not skip reasons. A `skip` is accepted
only when exact history proves an existing direct Alex child reply. Terminal
technical blockers require a machine-readable `blocker_code`.

In mandatory mode, every reply returned by the authenticated X mentions
endpoint is queued for live inspection, even when its immediate parent is
another participant. This prevents an incomplete historical backfill from
hiding a real continuation that still mentions the account.

Before drafting, Sol receives compact cross-thread memory keyed by the stable X
user ID: exact public turns, dates, URLs, and exact Alex replies. The
`commenter-history` command can retrieve deeper stored history without an age
cutoff. Prior statements may support a source-linked contradiction or expose a
changed standard, but must not become speculative profiling, sensitive-trait
inference, or persistent personal targeting.

An official X archive can extend this memory with Alex's older public posts and
replies. The archive importer verifies the numeric account ID, defaults to a
read-only dry-run, imports only public post records, ignores direct-message
files, and rejects conflicting rewrites. X archives do not provide a complete
copy of every other user's reply, so the API watcher and exact Browser history
remain authoritative for incoming turns.

An external public-post corpus can be loaded into a separate quarantined
candidate index. Its records are search hints, not evidence. The autopilot
receives at most three compact hints for the current stable X user ID, with
`usable_as_evidence=false`. A hint becomes usable only after its exact live X
post or official X API record is append-only verified.

## Current checkpoint

Polling, supervisor, janitor, and event dispatcher LaunchAgents run every minute
for `@axrbarsic`. The dispatcher checks the compact queue without a model and
starts the existing Sol owner only for ready work. A complete initial
review remains a separate gate and must finish before an empty incremental
queue is treated as proof of completeness.

Live polling also requires a positive prepaid X API credit balance. With a
recognized token and no credits, X returns HTTP 402 with the
`credits-depleted` problem type. The watcher reports this as
`billing_blocked` and must remain unloaded until credits are available.

## Configure

Copy `config.example.json` to `config.json`, then set the numeric X user ID.
Keep `mandatory_response_mode=true` for the no-content-skip contract.
Set `commenter_memory_limit` to the compact number of prior interactions placed
in each automation handoff. Deeper history remains available on demand:

Enable `conversation_tail_enabled` to watch only recent chains with an exact
Alex turn. Use `conversation_tail_initial_lookback_hours` to bound the first
scan, and keep the mentions cursor separate from the conversation tail scan
timestamp.

```bash
python3 xmention_watcher.py --config config.json commenter-history EVENT_ID \
  --limit 50
```

Verify that the runtime, Browser owner workspace, and installed skill use the
single canonical project root:

```bash
python3 project_layout_audit.py --config config.json \
  --require-installed-skill
```

## Import an official X archive

Do not commit the ZIP, extracted archive, runtime database, or import report to
Git. They can contain private account data. Keep them in a local protected
location.

Use the two-phase orchestrator for normal intake. It requires the original ZIP
outside the project, validates the owner and public members, hashes the source,
and writes a plan beside the archive:

```bash
python3 archive_intake.py \
  --config config.json \
  --archive /Users/alexlane/Archives/x-mention-watcher/YYYY-MM-DD/source.zip
```

The plan must show the configured numeric account ID, the expected username,
the public post count, and `direct_messages_imported: 0`. It also lists private
archive members that were deliberately ignored.

Apply the same unchanged ZIP after reviewing the plan:

```bash
python3 archive_intake.py \
  --config config.json \
  --archive /Users/alexlane/Archives/x-mention-watcher/YYYY-MM-DD/source.zip \
  --apply
```

Apply refuses to run without a matching dry-run plan. It creates a consistent
snapshot before mutation, performs the append-only import, requires the full
archive memory audit, and creates a second snapshot. The external
`reports/intake-receipt.json` binds the ZIP SHA-256, import result, and both
snapshots. `x_archive_import.py` remains the low-level library and diagnostic
CLI, while `archive_intake.py` is the normal production path.

The import is idempotent. Repeating the same archive returns
`already_imported`. A later archive may add new posts, but a changed immutable
record for an existing status ID fails closed. Imported historical replies by
Alex become a separate `archive_alex_replies` section in commenter memory,
matched through the stable counterparty X user ID. The absent incoming text is
never invented.

## Import an external candidate corpus

Use this only for public X data collected outside the official archive and the
live watcher. Every JSONL record must include a numeric status ID, the expected
account handle, an X status URL, and its source verification state.

Run a dry-run first:

```bash
python3 candidate_corpus.py \
  --config config.json \
  import \
  --input /absolute/path/to/public-posts.jsonl \
  --subject-user-id 123456789 \
  --handle example_user
```

Apply the validated corpus:

```bash
python3 candidate_corpus.py \
  --config config.json \
  import \
  --input /absolute/path/to/public-posts.jsonl \
  --subject-user-id 123456789 \
  --handle example_user \
  --apply
```

Review candidate memory for an existing incoming event:

```bash
python3 candidate_corpus.py \
  --config config.json \
  history EVENT_ID \
  --limit 20
```

An unverified candidate must never be quoted or treated as proof. After opening
the exact live post or reading it through the official X API, save the exact
text to a local file and append a verification:

```bash
python3 candidate_corpus.py \
  --config config.json \
  verify STATUS_ID \
  --url https://x.com/example_user/status/STATUS_ID \
  --exact-text-file /absolute/private/path/exact-text.txt \
  --observed-at 2026-07-25T20:00:00Z \
  --method live_x_dom
```

Verification stores the exact observed text separately and never rewrites the
candidate record. A conflicting second verification fails closed.

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

Install the signed helper locally before live use:

```bash
scripts/install_keychain_helper.sh
```

Store a token without putting it in process arguments:

```bash
printf '%s' "$X_BEARER_TOKEN" | \
  var/keychain-helper set axrbarsic-x-mention-watcher axrbarsic
```

The helper supports presence checks:

```bash
var/keychain-helper exists axrbarsic-x-mention-watcher axrbarsic
```

Verify the Data Protection Keychain accessibility without printing the token:

```bash
var/keychain-helper is-after-first-unlock \
  axrbarsic-x-mention-watcher axrbarsic
```

The installer builds an app-like helper, signs it with the configured Apple
Development team, embeds a Mac provisioning profile, and verifies its
Keychain access group and existing-item migration before atomically switching
`var/keychain-helper`. On the first legacy migration, macOS may ask once for
permission to let the signed helper read the old login-keychain item. Approve
that system prompt: the token is not printed, the DP copy stays in Keychain,
and later background reads do not require the prompt.
The helper stores secrets in the macOS Data Protection Keychain as
`AfterFirstUnlockThisDeviceOnly`. Add the Apple Account to Xcode before the
first install. A standalone unsigned CLI cannot use this Keychain mode.

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

## Standing autopilot worker

The optional dispatcher gives queued event IDs a 30-minute lease and returns
one compact JSON claim. It prevents duplicate task wakeups while the Browser
owner is working or a local-max response is still being prepared.

```bash
python3 scripts/autopilot_dispatch.py \
  --config config.json \
  --lease-seconds 1800 \
  claim

python3 scripts/autopilot_dispatch.py \
  --config config.json \
  --lease-seconds 1800 \
  status
```

The Desktop bridge returns the claim token and immutable wake prompt:

```bash
python3 scripts/autopilot_bridge.py \
  --config config.json \
  --lease-seconds 1800 \
  claim
```

If `dispatch` is true, the self-owned Sol Max heartbeat claims and marks the
work as started:

```bash
python3 scripts/autopilot_bridge.py \
  --config config.json \
  started \
  --claim-token CLAIM_TOKEN
```

After exact history and durable resolution remove all claimed IDs from the wake
queue, it records completion:

```bash
python3 scripts/autopilot_bridge.py \
  --config config.json \
  completed \
  --claim-token CLAIM_TOKEN
```

If work fails before durable resolution, the automation releases the exact
claim:

```bash
python3 scripts/autopilot_bridge.py \
  --config config.json \
  failed \
  --claim-token CLAIM_TOKEN \
  --error "Browser work failed"
```

An empty queue exits without a model or Browser. A non-empty claim contains
only eligible event metadata and the standing contract. The pinned Browser
owner reads exact conversation history from SQLite and the append-only ledger.
Historical custom GPT URLs remain audit metadata only. The watcher, dispatcher,
and relay never post directly.

`scripts/autopilot_resume.py` is a fail-closed retirement guard. It always
exits nonzero and never claims an event or starts Codex. This prevents an old
LaunchAgent from repeatedly spending Sol runs on a CLI session that cannot use
the built-in Browser.

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
reply is being researched or a local explainer draft is being generated. After
the cutoff is fixed, every new direct reply belongs to that active cycle until
Alex stops it, even if an earlier target later becomes older than the initial
lookback interval.

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

Use `blocked` only for an actionable event whose terminal mandatory workflow
dependency is unavailable, and include an allowed `blocker_code`. Legacy
ChatGPT conversation, model, and screenshot blockers are no longer terminal
because the local skill does not depend on those resources. The Browser handoff
marker must be
`durable_blocked_pending_root_resolve`, and `blocked` must not include a reply
URL.

Use the narrowest terminal code. The historical
`required_pro_model_unavailable`, `missing_historical_pro_conversation`, and
`target_screenshot_unavailable` records are requeued by the compatibility
command `pro-model-recovery-requeue`. A transient Browser or source error
remains queued and must
not use either terminal code.

If live X proves that Alex already answered before the current audit, use
`skip` with `reply_url=null`, `existing_alex_reply_url=<canonical URL>`, and
`alex_history_status=exact_alex_turn_appended`. The direct Alex child turn must
already be in exact conversation history. The `reply_url` field belongs only
to a `published` handoff created by the current resolution.

After enabling the strict policy, reconcile recent historical content skips
and previously ignored mention replies without supplying event IDs:

```bash
python3 xmention_watcher.py --config config.json mandatory-response-requeue \
  --hours 12 --as-of 2026-07-25T20:00:00Z --dry-run
python3 xmention_watcher.py --config config.json mandatory-response-requeue \
  --hours 12 --as-of 2026-07-25T20:00:00Z
```

This command is also the clean-experiment entry point after an eligibility fix.
Run dry-run and apply with one identical `as-of`, then let the model-free
dispatcher launch the existing Sol owner heartbeat. Do not manually inject
the known ID. The reusable diagnostic contract is stored in
[`reliability-debugging.md`](skill-backup/x-twitter-operator/references/reliability-debugging.md).

Never rewrite an old ledger record when scope changes or a contract dependency
is recovered. Append a replacement handoff with
`supersedes_existing_resolution=true` and a non-empty
`resolution_revision_reason`. The sync command records both versions in
`event_resolution_revisions`. It permits only `skip` to `published`, `skip` to
`blocked`, `blocked` to `published`, and `blocked` to `skip`. It also permits
`blocked` to `blocked` solely for an audited metadata correction such as
backfilling a terminal `blocker_code`; the previous and replacement payloads
remain in `event_resolution_revisions`. A published resolution is terminal.
Any implicit rewrite fails closed.

Only complete the initial audit after every direct reply has a durable
`published`, `skip`, or `blocked` resolution and the queue reaches zero:

```bash
python3 xmention_watcher.py --config config.json initial-audit-complete
```

Audit the complete durable memory independently of the Browser:

```bash
python3 xmention_watcher.py --config config.json memory-audit
python3 xmention_watcher.py --config config.json memory-audit --require-archive
```

The first command must keep the existing watcher, stable identities, exact
history, resolutions, quarantined candidate corpus, SQLite integrity, and
foreign keys valid while the official archive is pending. The second command
is the final fail-closed gate: it also requires at least one valid official X
archive import for the configured stable user ID, consistent post counts,
canonical post records, and a matching account alias. It never treats private
messages as imported memory.

Create a transactionally consistent backup source instead of copying the live
SQLite and WAL files:

```bash
python3 memory_snapshot.py --config config.json
```

The canonical local layout and the encrypted off-site backup design are
documented in [docs/storage-and-backup.md](docs/storage-and-backup.md).
The single fail-closed completion gate is documented in
[docs/readiness-audit.md](docs/readiness-audit.md) and runs with
`python3 readiness_audit.py`.
The executable backup contract is implemented by `restic_backup.py`. It backs
up only verified memory snapshots, manifested Browser evidence, and the
external official-archive vault. See the storage document for Keychain setup,
preflight, retention, repository checks, and restore drills.

Completion fails closed while any direct reply lacks an `event_resolutions`
row, even if an old client changed its delivery state. `initial-audit-start`
requeues legacy baseline or acknowledged direct replies that have no durable
resolution. The generic `ack` command rejects direct replies, so the X workflow
must use `resolve`. `resolve` itself rejects an event until its exact inspected
turn is present in conversation history. Completion also rejects any legacy
resolution missing that history turn, or any published reply missing its exact
Alex turn and parent link. Later polls use `since_id` and queue new direct
replies plus nested replies in conversations with an exact stored Alex turn.

Use `stance` for the stable broad class and `stance-detail` for the exact
Sol classification, such as `supportive_confirmation`,
`opposing_substantive_claim`, or `hostile_personal_attack`.
The broad field accepts only `supportive`, `opposing`, `neutral`, or
`ambiguous`; labels such as `corrective` belong in `stance-detail`.

## Supervisor

One-shot token-free system check:

```bash
python3 scripts/autopilot_supervisor.py \
  --config config.json \
  --contract recovery/system-contract.json \
  run
```

The supervisor remains silent while healthy. A stale or failing poll receives
one allowlisted LaunchAgent kickstart. A persistent or nonrepairable failure
becomes one durable incident. Dispatcher and the self-owned heartbeat expose
that incident to the existing Sol Max owner. Reservation, claim, cooldown,
and a required completion report prevent duplicate wakes and false success.

```bash
python3 scripts/autopilot_supervisor.py --config config.json status
```

The poller uses a process lock, rejects repeated or excessive pagination, and
never advances `since_id` when a request fails. Background stdout and stderr go
to `/dev/null`; durable diagnostics remain in SQLite and the health JSON so log
files cannot grow without bound.

## Conversation history

SQLite stores an append-only conversation graph for both self-authored and
local-max follow-ups. Each turn contains the exact public X text, status ID,
parent status ID, URL, actor, author, provenance, timestamp, and factual source
URLs. Legacy chains may also retain an exact ChatGPT conversation URL as
historical audit metadata. Local generation never opens that URL.

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
  --output var/exports/conversation-history.jsonl
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

The `macos/` directory contains five LaunchAgent templates whose intervals come
from `config.json`: poll, supervisor, session janitor, event dispatcher, and
Codex CLI updater. The idle terminal path is entirely model-free. A ready queue
starts Codex Desktop when needed, then the existing Sol Max heartbeat claims
the route. The janitor archives historical service tasks
and recovers orphaned claims without creating a Codex task or spending model
tokens.

Do not install the LaunchAgents on another machine until a live shadow run with
the official X API has matched a manual Browser scan.

After that gate, render machine-specific plists into a staging directory:

```bash
python3 scripts/render_launchd.py \
  --config /absolute/path/to/config.json \
  --output-dir /absolute/path/to/staging
```

Rendering validates all five plists. Loading them with `launchctl` is a
separate, explicit production step.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Skill backup

`skill-backup/x-twitter-operator` is a restorable snapshot of the installed
Codex skill contract that governs detection, Browser ownership, duplicate
checks, local-max continuity, and publication. Mutable runtime data remains under
the ignored `var/` directory in the same canonical project root. Credentials
remain in macOS Keychain, and official X archive ZIP files remain in the
separate archive vault.

Official references:

- https://learn.chatgpt.com/docs/browser?surface=app
- https://learn.chatgpt.com/docs/automations.md
- https://docs.x.com/x-api/users/get-mentions
- https://docs.x.com/x-api/posts/timelines/quickstart/user-mention-quickstart
- https://docs.x.com/x-api/fundamentals/rate-limits
- https://docs.x.com/x-api/getting-started/pricing
