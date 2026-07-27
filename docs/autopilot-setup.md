# Codex Desktop autopilot setup

[English](autopilot-setup.md) | [Русский](autopilot-setup.ru.md)

## Purpose

The system separates token-free mechanics from content decisions:

1. The X watcher polls the official mentions endpoint every minute, deduplicates
   events, and updates SQLite plus the durable queue.
2. The watchdog checks polling freshness and API failures.
3. A Python dispatcher runs the model-free `gate` every minute.
4. Empty, busy, or resource-deferred queues exit without a model, Browser, or
   a new Codex task.
5. A ready queue starts Codex Desktop in the canonical workspace when the app
   is closed. Failure keeps the queue intact, retries later, and alerts Alex.
6. One existing in-app Luna Low heartbeat runs `reserve-handoff`, reads the
   canonical owner thread, then makes exactly one `send_message_to_thread` call
   only when that thread is inactive.
7. The pinned Sol High task claims atomically, restores the live thread,
   publishes, and stores exact history.
8. The supervisor may later stop only a Desktop process it launched itself.

The external app-server performs neither relay nor Browser work. It has no
Codex Desktop built-in Browser session or desktop-only cross-thread tools. The
authenticated Browser always belongs to the persistent in-app owner.

> Current deployment status, 2026-07-26: the persistent Sol High owner passed
> a read-only Browser preflight with the display locked and processed three
> events. The new in-app relay then handed off the next organic event without
> manual prompting; it was published, imported, and durably resolved. The
> live test exposed a race between adjacent heartbeat runs. Atomic
> `reserve-handoff` with a TTL now closes that race. The old `x` automation
> remains paused until the new LaunchAgent completes final verification.
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
| X watcher LaunchAgent | 1 minute | none | API, dedupe, SQLite, queue |
| Watchdog LaunchAgent | 1 minute | none | Poll and API health |
| Session janitor LaunchAgent | 1 minute | none | Archive service tasks, recover claims |
| Event dispatcher LaunchAgent | 1 minute | none while idle | Gate, Desktop launch, managed shutdown |
| In-app relay heartbeat | while Desktop is open | Luna Low | Reservation and one owner message |
| Pinned Browser owner | per event | Sol High | Claim, Browser, sources, publication |
| Custom GPT | Pro only | configured Pro | Long answer in historical conversation |
| Codex CLI updater | 6 hours | none without update | Version, SHA-256, doctor, history |

Idle terminal monitoring spends zero model tokens and creates no task. The
normal unattended state keeps Desktop closed. A real event starts it and uses
one short Luna relay turn. A supervisor-managed Desktop closes after the queue
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
  "command_center_idle_grace_seconds": 60,
  "desktop_relay_mode": "in_app_heartbeat",
  "desktop_auto_quit_after_work": true,
  "desktop_auto_quit_grace_seconds": 180,
  "relay_handoff_reservation_seconds": 180,
  "app_server_dispatch_interval_seconds": 60,
  "app_server_dispatch_state_file": "var/app-server-dispatch.json",
  "app_server_dispatch_lock_file": "var/app-server-dispatch.lock",
  "codex_cli_update_channel": "preview",
  "codex_cli_update_interval_seconds": 21600,
  "resource_mode": "auto",
  "memory_guard_enabled": true,
  "voice_priority_enabled": true,
  "voice_priority_hold_seconds": 300,
  "session_janitor_interval_seconds": 60,
  "session_janitor_minimum_age_seconds": 60,
  "commenter_memory_limit": 12,
  "poll_interval_seconds": 60,
  "watchdog_interval_seconds": 60
}
```

`browser_owner_thread_id` identifies one pinned Sol High owner. `x-relay` is a
heartbeat attached to one existing Luna Low thread, not a standalone
automation. `reserve-handoff` does not claim X events, but prevents duplicate
wake delivery for 180 seconds. Before creating a reservation, it reads the
owner's durable rollout, requires the latest task to be terminal, and enforces
`command_center_idle_grace_seconds`. The relay then reads the live owner thread
and sends only when `status.type=idle` or `status.type=notLoaded`. On a live
read failure or delivery failure, it runs `release-handoff` with the exact
reservation token. A successful owner claim changes the reservation to
`claimed`. The restorable heartbeat prompt is tracked in
[`macos/x-relay.prompt.txt`](../macos/x-relay.prompt.txt).

A reply posted manually by Alex is an `alex` turn. If somebody answers it, the
Browser owner restores the exact live branch, stores any previously unseen
manual reply, and uses the complete history before drafting the continuation.

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
5. The in-app heartbeat runs `reserve-handoff`. The command returns
   `owner_thread_active` or `owner_thread_cooldown` without a reservation while
   the canonical command center is active or inside its quiet period.
6. The winning relay reads the exact live Browser owner thread. It sends one message
   with a Sol High override only when `status.type=idle` or
   `status.type=notLoaded`.
7. If the owner thread is active, cannot be read, or delivery fails, the relay
   runs `release-handoff` with its exact reservation token and exits. The queue
   remains pending. An adjacent heartbeat receives `handoff_reserved`.
8. The Browser owner runs `claim`, then `started`.
9. The owner loads `x-twitter-operator`, opens the minimum tabs, performs double
   dedupe, and executes the publication transaction.
10. After exact history and durable resolution, the owner runs `completed`.
11. If Desktop was supervisor-launched, an empty queue and cleared owner lease
    start a grace timer before that exact managed PID is stopped.

## Memory and quality contracts

- Idle: zero model tokens, zero Browser tabs, zero new tasks.
- Short: one X tab.
- Pro: one X tab, one ChatGPT tab, one active generation.
- Only Sol High makes publication decisions and writes short replies.
- Luna never analyzes X content or drafts responses.
- The resource guard pauses Browser work without deleting queued events.
- An active voice conversation selects the efficiency profile and holds a
  post-voice pause.
- Display lock is not an error. On an awake Mac, the owner performs one
  read-only Browser preflight and continues when `iab` is available.
- The handoff reservation and global owner lease prevent overlapping relay and
  Browser-owner runs.
- The relay never wakes an active canonical owner thread. Mobile Remote, local
  Desktop, and automated Browser work use that durable thread serially. Because
  the thread status read and message delivery are separate Codex operations,
  interactive sends from two clients must also be serialized.
- The janitor never terminates the current Browser owner or an ambiguous
  process.

## Live canary

1. Pause the old X automation.
2. Use one real unresolved event without giving its ID to the model.
3. Require natural watcher discovery.
4. Confirm one relay turn and one Sol High owner turn.
5. Verify the X URL, exact history, and durable resolution.
6. Confirm the event leaves the queue.
7. Run one idle dispatcher cycle and verify no task or helper count increase.
8. Only then delete the old automation and keep the LaunchAgent.

Never simulate success by inserting a resolution directly. A post-restart
check must begin from the live durable queue and exact X thread.
