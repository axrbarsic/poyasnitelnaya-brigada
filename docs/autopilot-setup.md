# Codex Desktop autopilot setup

[English](autopilot-setup.md) | [Русский](autopilot-setup.ru.md)

## Purpose

The watcher detects X replies without model tokens. A local Codex Desktop
automation checks the durable queue every five minutes. An empty queue exits
before opening Browser. A non-empty queue is leased atomically and processed
inside that same scheduled run on Sol High.

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
| Session janitor LaunchAgent | 5 minutes | none | Archive tasks and recover an orphaned claim |
| Codex Desktop automation | 5 minutes | Sol High | Claim a non-empty queue, inspect Browser, decide, publish, verify |
| Custom GPT | Pro only | configured Pro | Long answer in the exact historical conversation |

The minute poll is token-free. The scheduled run still spends a small amount of
Sol context on an empty queue, but it never opens Browser or performs content
work unless `dispatch` is true. Claim runs before skills and references are
loaded. Archiving is not part of the automation prompt and consumes no model.
The local app-server janitor performs it independently.

## Configure

Add local values to ignored `config.json`:

```json
{
  "autopilot_state_file": "var/autopilot-dispatch.json",
  "autopilot_health_file": "var/autopilot-health.json",
  "browser_owner_cwd": ".",
  "memory_guard_enabled": true,
  "memory_guard_state_file": "var/resource-health.json",
  "resource_mode": "auto",
  "resource_mode_idle_seconds": 900,
  "session_janitor_interval_seconds": 300,
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

## Create the Codex Desktop automation

Create one local scheduled task with:

- model: `gpt-5.6-sol`
- reasoning effort: `high`
- cadence: every five minutes
- failed-run notifications only

Its durable prompt must implement this state machine:

1. Before reading automation memory, skills, or references, run
   `autopilot_bridge.py ... claim`.
2. If status is `memory_deferred` or `dispatch` is false, leave the queue
   unchanged and do not open Browser.
3. Finish the empty run normally without `list_threads`, Browser, or
   self-archiving.
4. If `dispatch` is true, run `started --claim-token ...`.
5. Execute the returned prompt inside the current run as the only Browser
   owner.
6. After exact history and durable resolution remove every claimed event from
   `wake-request.json`, run `completed --claim-token ...`.
7. On an error before durable resolution, run
   `failed --claim-token ... --error ...`.
8. Close every run-owned Browser tab and finish with a normal final response.
   Never archive the current task from inside itself.
9. Do not run the X API postflight poll inside the automation. The LaunchAgent
   owns polling and Keychain access.

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
