# Codex Desktop autopilot setup

[English](autopilot-setup.md) | [Русский](autopilot-setup.ru.md)

## Purpose

The watcher detects X replies without model tokens. A Luna Low Codex Desktop
automation runs only a read-only gate every five minutes. Empty, busy, and
deferred queues exit before Sol and Browser. A ready queue wakes one pinned Sol
High task, which claims atomically and processes the events.

Each non-empty claim includes up to `commenter_memory_limit` source-linked prior
interactions for the same stable X user ID. Sol may query deeper retained
history with `commenter-history` only when it is useful to the current reply.
The same memory may include up to three quarantined external candidate hints.
They remain unusable as evidence until the exact public post is verified live
or through the official X API.

Codex CLI is intentionally excluded from Browser work. The official Codex
manual states that the built-in Browser is unavailable in Codex CLI and the
IDE extension. `scripts/autopilot_resume.py` is a fail-closed retirement guard.
See the official [Built-in browser](https://learn.chatgpt.com/docs/browser?surface=app)
and [Scheduled tasks](https://learn.chatgpt.com/docs/automations.md)
documentation.

## Components

| Component | Frequency | Model | Responsibility |
| --- | --- | --- | --- |
| X watcher LaunchAgent | 1 minute | none | Fetch, deduplicate, persist, queue |
| Watchdog LaunchAgent | 1 minute | none | Detect stale polling or API failures |
| Session janitor LaunchAgent | 1 minute | none | Archive tasks and recover an orphaned claim |
| Automation dispatcher | 5 minutes | Luna Low | Read-only gate and wake the owner only for a ready queue |
| Pinned Browser owner | per event | Sol High | Claim, inspect Browser, decide, publish, verify |
| Custom GPT | Pro only | configured Pro | Long answer in the exact historical conversation |

The minute poll is token-free. An empty scheduled run uses only a short Luna Low
context. Sol High receives work only when `dispatch` is true. The pinned owner
claims before loading skills and references. Archiving is not part of the
automation prompt and consumes no model. The local app-server janitor performs
it independently.

## Configure

Add local values to ignored `config.json`:

```json
{
  "autopilot_state_file": "var/autopilot-dispatch.json",
  "autopilot_health_file": "var/autopilot-health.json",
  "browser_owner_cwd": ".",
  "memory_guard_enabled": true,
  "voice_priority_enabled": true,
  "voice_priority_hold_seconds": 300,
  "memory_guard_state_file": "var/resource-health.json",
  "resource_mode": "auto",
  "resource_mode_idle_seconds": 900,
  "resource_mode_performance_min_free_percent": 35,
  "memory_guard_swap_recovery_free_percent": 25,
  "memory_guard_swap_recovery_codex_rss_mb": 2000,
  "memory_guard_helper_recovery_free_percent": 25,
  "memory_guard_helper_recovery_codex_rss_mb": 1800,
  "memory_guard_helper_recovery_total_rss_mb": 512,
  "session_janitor_interval_seconds": 60,
  "session_janitor_minimum_age_seconds": 60,
  "session_janitor_orphan_owner_seconds": 300,
  "session_janitor_reap_helpers": true,
  "session_janitor_helper_grace_seconds": 120,
  "commenter_memory_limit": 12,
  "poll_interval_seconds": 60,
  "watchdog_interval_seconds": 60
}
```

The dot resolves to the directory beside `config.json`, which is the canonical
project root. Do not create a separate Browser owner workspace.

Nested replies are tracked automatically once exact conversation history
contains an `alex` turn. No topic-specific root ID list is required. A nested
reply still requires live context classification before an author reply.

Keep runtime state under ignored `var/`. Never commit `config.json`, tokens,
cookies, SQLite, queue state, or local history.

## Install token-free services

Render all three LaunchAgents:

```bash
python3 scripts/render_launchd.py \
  --config /absolute/path/to/config.json \
  --output-dir /absolute/path/to/staging
```

Install and load:

- `com.axrbarsic.xmention.poll.plist`
- `com.axrbarsic.xmention.watchdog.plist`
- `com.axrbarsic.xmention.janitor.plist`

Do not install the retired `com.axrbarsic.xmention.autopilot.plist`.

## Create the Browser owner and automation

Create one ordinary task, pin it, and retain its exact thread ID:

- model: `gpt-5.6-sol`
- reasoning effort: `high`
- role: the only Browser owner

Add that ID to `session_janitor_protected_thread_ids`.

Then create one local scheduled task with:

- model: `gpt-5.6-luna`
- reasoning effort: `low`
- cadence: every five minutes
- failed-run notifications only

Its durable prompt must implement this state machine:

1. Run `autopilot_bridge.py ... gate`.
2. On `dispatch=false`, exit quietly without Browser, skills, or `list_threads`.
3. On `dispatch=true`, expose `send_message_to_thread` through `tool_search`.
4. Call that Codex app tool directly, never from `functions.exec`, JavaScript,
   or `tools.*`. Send one follow-up to the pinned task with explicit
   `gpt-5.6-sol` and high-effort overrides.
5. The Browser owner runs `claim`, then `started`, and executes the returned
   prompt.
6. After exact history and durable resolution remove every claimed event from
   `wake-request.json`, then run `completed`.
7. On an error before durable resolution, run `failed`.
8. The owner closes only its task-owned Browser tabs and remains pinned.
9. The dispatcher never creates an inbox item or handles X content itself.
10. Do not run the X API postflight poll inside the owner. The LaunchAgent
   owns polling and Keychain access.

The copy-ready production template is stored in
[`automation-prompts/luna-dispatcher.ru.md`](automation-prompts/luna-dispatcher.ru.md),
with an [English installation note](automation-prompts/luna-dispatcher.en.md).
Only the project path and pinned task ID are replaced.

Preserve these prompt invariants:

- one claim token for one pending batch;
- idle: zero Browser tabs;
- short: one X tab;
- Pro: one X tab, one ChatGPT tab, and one active generation;
- every run-owned tab closes before the run exits;
- Sol High writes and validates every short reply;
- no topic-specific exceptions;
- a postflight warning never rolls back a durable resolution.

## Failure behavior

- API failure does not advance the X cursor.
- Queue and dispatcher state use file locks and atomic writes.
- One global owner lease suppresses overlapping scheduled runs, including a
  new event that appears while an earlier claim is still active.
- A second run preserves `work_in_progress`; it cannot replace the active
  claim.
- A new Codex runtime ID may reclaim immediately after the app restarts.
- The janitor releases a stream-disconnected claim only when its related task
  becomes stale. `notLoaded` alone is not proof of death.
- The janitor terminates only a helper bundle exactly correlated with a
  completed X task. The current owner and ambiguous processes remain
  untouched.
- The resource guard defers Browser work under critical memory pressure without
  resolving an event or moving the queue.
- Failure before durable resolution releases the exact claim.
- `completed` is accepted only after the claimed IDs leave the wake queue.
- `reconcile-completed` additionally verifies every event in SQLite before it
  can correct a postflight health state.
- `blocked_pending_iab` is not success and must not resolve an event.
- The retired CLI launcher exits nonzero before claiming or spending a model
  run.

## Live canary

Use one or more real unresolved replies:

1. Pause the Desktop automation and unload poll/watchdog.
2. Save an SQLite backup and copies of runtime JSON.
3. Remove only the canary events and rewind `since_id` to the immediately
   preceding observed ID.
4. Confirm queue, dispatch state, history, and resolutions no longer know the
   events.
5. Load poll/watchdog and activate the Desktop automation without giving it
   event IDs.
6. Require natural API rediscovery, one atomic claim, one verified publication
   per event, exact history, and durable resolution.
7. Confirm queue and dispatch state are empty.

Never simulate success by directly inserting a resolution.
