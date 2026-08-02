# Codex Desktop autopilot setup

[English](autopilot-setup.md) | [Русский](autopilot-setup.ru.md)

## Purpose

The system separates token-free mechanics from content decisions:

1. The X watcher polls the official mentions endpoint every minute and runs a
   bounded tail scan for recent conversations containing an exact Alex turn.
   It deduplicates events and updates SQLite plus the durable queue.
2. The token-free supervisor checks polling freshness, the recovery contract,
   and API failures. It attempts one allowlisted stale-poll repair.
3. A Python dispatcher runs the model-free repair gate, then the X gate.
4. Empty, busy, or resource-deferred queues exit without a model, Browser, or
   a new Codex task.
5. A ready queue starts Codex Desktop in the canonical workspace when the app
   is closed. Failure keeps the queue intact, retries later, and alerts Alex.
6. One existing in-app heartbeat is attached directly to the Sol Max owner.
   It checks a durable repair incident, the X queue, and idle-only outbound,
   then runs one `reserve-handoff` and the matching claim in the same task.
7. The Sol Max task restores the live thread,
   publishes, and stores exact history.
8. The supervisor may later stop only a Desktop process it launched itself.

The external app-server performs neither relay nor Browser work. It has no
Codex Desktop built-in Browser session or desktop-only cross-thread tools. The
authenticated Browser always belongs to the persistent in-app owner.

> Current deployment status, 2026-08-01: the persistent Sol Max owner and its
> self-owned heartbeat pass doctor with zero failures. The explainer route runs as a
> local skill without ChatGPT, uses exact durable history, and validates one
> non-empty reply capped at 4000 Unicode code points without length padding.
>
> The same live checkpoint also ran an isolated empty cycle through the real
> dispatcher entry point. It returned `idle`; task IDs, relay and owner turns,
> app-server count, node helper count, renderer count, and dispatcher count
> were unchanged before and after.

Official architecture references:

- [Codex app-server](https://learn.chatgpt.com/docs/app-server)
- [Scheduled tasks](https://learn.chatgpt.com/docs/automations)
- [Project config files](https://learn.chatgpt.com/docs/config-file/config-advanced#project-config-files-codexconfigtoml)
- [Built-in Browser](https://learn.chatgpt.com/docs/browser?surface=app)

## Components

| Component | Frequency | Model | Responsibility |
| --- | --- | --- | --- |
| X watcher LaunchAgent | 1 minute | none | Mentions, tracked conversation tails, dedupe, SQLite, queue |
| Supervisor LaunchAgent | 1 minute | none | Doctor, repair allowlist, durable incident |
| Session janitor LaunchAgent | 1 minute | none | Archive service tasks, recover claims |
| Event dispatcher LaunchAgent | 1 minute | none while idle | Gate, Desktop launch, managed shutdown |
| Self-owned heartbeat | while Desktop is open | Sol Max | Reservation and claim in the same task |
| Browser owner | per event | Sol Max | Browser, sources, publication |
| Local explainer skill | local-max only | Sol Max | Reply capped at 4000 points, history, sources |
| Codex CLI updater | 6 hours | none without update | Version, SHA-256, doctor, history |

Idle terminal monitoring spends zero model tokens and creates no task. The
normal unattended state keeps Desktop closed. A real event starts the existing
owner directly. A supervisor-managed Desktop closes after the queue
and owner lease clear. A Desktop opened by Alex is never closed automatically.

## Configuration

The project must be trusted or Codex ignores its local `.codex/config.toml`.
Add this to `~/.codex/config.toml`:

```toml
[projects."/absolute/path/to/x-mention-watcher"]
trust_level = "trusted"
```

Create ignored `config.json` from the example and set:

```json
{
  "browser_owner_cwd": ".",
  "browser_owner_thread_id": "PINNED_SOL_OWNER_THREAD_ID",
  "codex_project_id": "CODEX_PROJECT_ID",
  "autopilot_owner_rotation_after_runs": 20,
  "autopilot_owner_rotation_state_file": "var/browser-owner-rotation.json",
  "desktop_relay_mode": "in_app_heartbeat",
  "desktop_auto_quit_after_work": true,
  "desktop_auto_quit_grace_seconds": 180,
  "relay_handoff_reservation_seconds": 180,
  "app_server_dispatch_interval_seconds": 60,
  "app_server_dispatch_state_file": "var/app-server-dispatch.json",
  "app_server_dispatch_lock_file": "var/app-server-dispatch.lock",
  "autopilot_supervisor_state_file": "var/autopilot-supervisor.json",
  "autopilot_supervisor_repair_cooldown_seconds": 90,
  "autopilot_supervisor_escalation_retry_seconds": 1800,
  "codex_cli_update_channel": "preview",
  "codex_cli_update_interval_seconds": 21600,
  "resource_mode": "auto",
  "memory_guard_enabled": true,
  "memory_guard_swap_blocks_dispatch": false,
  "voice_priority_enabled": true,
  "voice_priority_hold_seconds": 300,
  "session_janitor_interval_seconds": 60,
  "session_janitor_minimum_age_seconds": 60,
  "commenter_memory_limit": 12,
  "conversation_tail_enabled": true,
  "conversation_tail_poll_interval_seconds": 300,
  "conversation_tail_watch_hours": 24,
  "conversation_tail_initial_lookback_hours": 2,
  "conversation_tail_overlap_seconds": 120,
  "conversation_tail_max_conversations": 80,
  "conversation_tail_daily_post_read_limit": 200,
  "conversation_tail_max_post_reads_per_poll": 50,
  "poll_interval_seconds": 60,
  "watchdog_interval_seconds": 60
}
```

Owned mentions remain on the one-minute poll. The more expensive Recent Search
conversation tail runs every five minutes, requests no expanded User or Media
resources, and has a separate read budget. Exhausting the tail budget does not
stop owned mentions. Inspect counters with
`python3 xmention_watcher.py --config config.json status`.

`browser_owner_thread_id` identifies the current Sol Max owner. `x-relay` is a
heartbeat attached to that same task, not a standalone automation or a relay
thread. After the configured number of completed runs, one durable transaction
creates a clean replacement, retargets the existing heartbeat through the
official Codex app API, switches the runtime role pointer, verifies the new
task, and archives the old task. A rotation token embedded in the initialization
prompt lets an interrupted run rediscover the same replacement instead of
creating a duplicate. `reserve-handoff` prevents an adjacent run for 180
seconds, then the same task executes the matching claim. There is no
cross-thread send or process-local `hostId` dependency. Atomic reservation plus
the global owner claim prevent concurrent Browser owners. The restorable heartbeat prompt is tracked in
[`macos/x-relay.prompt.txt`](../macos/x-relay.prompt.txt).

A reply posted manually by Alex is an `alex` turn. If somebody answers it, the
Browser owner restores the exact live branch, stores any previously unseen
manual reply, and uses the complete history before drafting the continuation.
Origin and continuation mode are separate. The claim payload includes a
`manual_parent_continuation` profile built from the exact stored parent. A
proven local-max origin, 500 or more Unicode code points, three or more
paragraphs, or at least one source URL selects the local explainer skill. A
concise parent without those signals may remain Sol short. Unknown manual
origin stays unknown, and neither route opens ChatGPT web.

Local-max history now comes from SQLite and append-only JSONL. Historical custom-GPT
URLs and migration records remain audit metadata only. The compatibility
command `pro-model-recovery-requeue` restores legacy model, conversation, and
screenshot blockers to the durable queue without receiving an event ID.

An event authored by the configured `user_id` is Alex's own turn, never inbound
work. The watcher imports its exact text and chain metadata as an `alex` turn,
sets `delivery_state=self_authored`, and neither queues nor resolves it. Repair
legacy unresolved own rows idempotently with
`python3 xmention_watcher.py --config config.json self-authored-reconcile`.
This command creates no `event_resolutions` row and performs no X mutation.

## Install LaunchAgents

Render five plist files:

```bash
python3 scripts/render_launchd.py \
  --config /absolute/path/to/config.json \
  --output-dir /absolute/path/to/staging
```

Install:

- `com.axrbarsic.xmention.poll.plist`
- `com.axrbarsic.xmention.watchdog.plist`
- `com.axrbarsic.xmention.janitor.plist`
- `com.axrbarsic.xmention.dispatch.plist`
- `com.axrbarsic.xmention.codex-update.plist`

The renderer uses the absolute `launchagent_python_executable` from local
config. On Alex's current host this is the stable Homebrew symlink
`/opt/homebrew/bin/python3`. Rendering fails if the interpreter is missing or
if its Python or SQLite version is below the configured minimum. The current
SQLite floor is 3.51.3. `system_doctor` repeats the exact `--help` import probe
for every installed entrypoint and verifies that all plists use the same safe
runtime.

Do not create a five-minute Codex automation for X. Every standalone scheduled
run creates a separate task and may leave helper processes attached to the
long-lived Codex Desktop runtime. Pause the old automation, prove the new
dispatcher, then delete it through the official `automation_update` API.

## State machine

1. LaunchAgent invokes `scripts/app_server_dispatch.py`.
2. Python runs `gate`.
3. `idle`, `work_in_progress`, and `deferred_resources` are recorded and exit
   without app-server.
4. On `ready`, the supervisor starts `codex app CANONICAL_ROOT` if Desktop is
   absent.
5. The self-owned heartbeat runs one `relay-reserve-handoff`. Python selects
   repair or X, creates at most one reservation, and returns one `dispatch`
   with an exact `route`.
6. The same Sol Max task executes the matching claim. If claim startup fails,
   it runs `release-handoff` with the exact reservation token. The queue
   remains pending.
7. An adjacent heartbeat receives `handoff_reserved`, and the global owner
   claim prevents concurrent Browser owners.
8. The Browser owner runs `claim`, then `started`.
9. The owner loads `x-twitter-operator`, opens the minimum tabs, performs double
   dedupe, and executes the publication transaction.
10. After exact history and durable resolution, the owner runs `completed`.
    If dispatcher cleanup has already removed that lease, `completed`
    automatically reconciles only after proving a zero pending queue and a
    durable SQLite resolution for every claimed event.
11. If Desktop was supervisor-launched, an empty queue and cleared owner lease
    start a grace timer before that exact managed PID is stopped.

While Desktop is ready and no owner exists, dispatcher state preserves one
`waiting_since` timestamp across minute ticks. `system_doctor` raises
`runtime.relay_progress` only when that unowned wait exceeds the versioned
`max_relay_wait_seconds` contract. Resource deferral and an active owner do not
count as a stalled relay.

## Memory and quality contracts

- Idle: zero model tokens, zero Browser tabs, zero new tasks.
- A production inbound claim: at most three oldest events and up to three
  task-owned X tabs for independent read-only inspection. Composer,
  publication, verification and durable resolve remain one ordered writer
  lane, one event at a time.
- Short and local-max: one ordered composer/publication lane, immediate durable
  resolution after each event, and zero ChatGPT tabs for local-max.
- Only Sol makes publication decisions. The local-max route always requires
  reasoning effort `max`.
- No helper model transports work, analyzes X content, or drafts responses.
  The self-owned Sol Max task reads the deterministic route and executes it.
- The resource guard pauses Browser work without deleting queued events.
- Renderer limits use 256 MiB RSS equivalents, while the raw process count and
  total renderer RSS remain visible. Cached lightweight renderer processes do
  not block Browser work merely because a Codex release creates more of them.
- `node_repl_count` and `mcp_process_count` use 64 MiB RSS equivalents.
  Their raw counts remain visible as `node_repl_process_count` and
  `mcp_raw_process_count`, so idle helper shells cannot exhaust the limits.
- An active voice conversation selects the efficiency profile and holds a
  post-voice pause.
- Display lock is not an error. On an awake Mac, the owner performs one
  read-only Browser preflight and continues when `iab` is available.
- The handoff reservation and global owner lease prevent overlapping relay and
  Browser-owner runs.
- Python creates no reservation while the durable canonical owner is active.
  If another turn starts during delivery, Codex queues or steers the direct
  follow-up. Mobile Remote, local Desktop, and automated Browser work use the
  same durable thread, while the global owner claim serializes publication.
- The janitor never terminates the current Browser owner or an ambiguous
  process.

## Live canary

1. Pause the old X automation.
2. Use one real unresolved event without giving its ID to the model.
3. Require natural watcher discovery.
4. Confirm one relay turn and one Sol Max owner turn.
5. Verify the X URL, exact history, and durable resolution.
6. Confirm the event leaves the queue.
7. Run one idle dispatcher cycle and verify no task or helper count increase.
8. Only then delete the old automation and keep the LaunchAgent.

Never simulate success by inserting a resolution directly. A post-restart
check must begin from the live durable queue and exact X thread.
