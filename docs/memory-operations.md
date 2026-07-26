# Operating on an 8 GB iMac

[English](memory-operations.md) | [Русский](memory-operations.ru.md)

## Root cause

The standalone Codex scheduled task created a new Codex task every five
minutes. The old lease protected only event IDs that were already claimed. If
a new X reply arrived during an active publication, the next scheduled run
claimed the new ID and started a second Sol High Browser owner. Electron
renderers, app-server, `node_repl`, MCP helpers, and sidebar tasks accumulated.

Real run logs from July 26, 2026 proved the overlap:

- the 07:24 run was still active until 07:31;
- the 07:29 run claimed another event;
- the 07:34 run started before the second owner completed.

## New contract

1. The dispatcher holds one global owner lease.
2. A new event waits for the active owner even when its ID was never leased.
3. Idle runs own zero Browser tabs.
4. Short work uses one X tab.
5. ChatGPT opens only after a Pro route is proven.
6. Pro work uses at most one X tab, one ChatGPT tab, and one generation.
7. Every task-owned tab closes on a terminal outcome.
8. Luna Low runs only a read-only gate. Sol High does not start for an empty,
   busy, or deferred queue.
9. Only a ready queue wakes one pinned Sol High task, which claims atomically
   before loading skills.
10. The resource guard runs before Browser. `resource_deferred` preserves the
   queue unchanged.
11. An active realtime voice conversation always defers heavy Browser work.
   The last microphone observation holds the pause for another five minutes.
12. A separate model-free LaunchAgent archives completed service tasks through
    the local Codex app-server. It creates no task and consumes no tokens.
13. The same LaunchAgent releases an orphaned claim only when its owning task
    is inactive and has not updated for at least five minutes.
14. After a scheduled run completes, the janitor correlates the exact task
    creation time with its `node_repl` and MCP helper bundle. After 120 seconds
    it sends `SIGTERM` only to helpers owned by that inactive task. The current
    owner, protected tasks, and processes without an exact time match are never
    touched.

## Automatic resource modes

`resource_mode: auto` chooses a profile before each non-empty claim. It changes
only whether Browser work may start. Sol High, fact checking, and publication
quality remain unchanged.

| Mode | Condition | Codex RSS | Renderers | Free memory | Swap |
| --- | --- | ---: | ---: | ---: | ---: |
| Efficiency | voice or memory pressure | 2200 MiB | 5 | at least 20% | up to 896 MiB |
| Balanced | user is active | 2350 MiB | 6 | at least 14% | up to 1024 MiB |
| Performance | idle 15 minutes on AC power, at least 35% free | 2500 MiB | 7 | at least 12% | up to 1152 MiB |

Every mode also obeys hard caps: 2700 MiB RSS, 8 renderers, at least 10% free
memory, and no more than 1280 MiB swap. Count caps of 10 `node_repl` and 20 MCP
helpers stay strict when their aggregate RSS is high or current free memory is
low. A large count with a small measured footprint is diagnostic, not a reason
to starve a ready queue forever. Crossing a cap defers the next Browser owner
and leaves every event durable.

macOS may retain allocated swap long after the active pressure has recovered.
An old absolute swap value alone therefore cannot block the queue forever. If
free memory is at least `memory_guard_swap_recovery_free_percent` and Codex RSS
is no more than `memory_guard_swap_recovery_codex_rss_mb`, swap is treated as
historical while RSS, renderer, and helper limits remain active. Swap becomes a
blocking signal again under current memory pressure.

Voice priority does not stop the minute watcher, watchdog, SQLite, or durable
queue. It blocks only the heavy Browser owner. After the last detected Codex
microphone input, the guard holds the pause for
`voice_priority_hold_seconds`, which defaults to 300 seconds. A short silent
gap between spoken turns therefore cannot resume Browser work.

Helper counts are early signals, not standalone proof of a leak. When free
memory is at least `memory_guard_helper_recovery_free_percent`, Codex RSS is no
more than `memory_guard_helper_recovery_codex_rss_mb`, and measured aggregate
helper RSS is no more than `memory_guard_helper_recovery_total_rss_mb`, excess
`node_repl` or MCP counts do not block the queue by themselves. Their limits
become strict again outside that recovery envelope. Performance mode also
requires `resource_mode_performance_min_free_percent`, so idle time alone
cannot select the most aggressive profile. The renderer cap is never relaxed.
If an individual system measurement is unavailable, the guard records the
error in `var/resource-health.json` and never loses an event.

## Measured wake cost

Three real empty or deferred Luna Low runs on July 26, 2026 used an average of
49,064 processed tokens. At a five-minute cadence this is about 588,768
processed tokens per hour. Two previous guard-only Sol High runs averaged about
59,993 tokens, or about 719,916 per hour.

The context-volume reduction is about 18%. Monetary savings are larger because
Luna is cheaper than Sol, but these counts are not a provider invoice. The
minute watcher, watchdog, and janitor remain entirely model-free.

## Why completed runs are archived

The official Codex documentation states that every standalone scheduled run
starts a new chat. Self-archiving an active task can leave it in `active`. A
model-based housekeeping automation also failed because it created more tasks.
Instead, `scripts/session_janitor.py` runs as an ordinary LaunchAgent every
minute. It archives exact matching completed tasks older than one minute,
protects the active owner and pinned task, and retains restorable history. An
active owner no longer stops housekeeping: the janitor still archives other
completed tasks and reaps their helpers.

The latest result overwrites `var/session-janitor-health.json`, so diagnostics
do not grow without bound. The owning task is matched by creation time before
the claim and its last update. Later empty scheduled runs cannot hide an
orphaned owner.

Each scheduled run may leave a child bundle of `node_repl`, Node MCP, and
Python MCP processes after the task itself completes. The janitor reaps only a
bundle whose start time matches one exact completed X task within two seconds.
It uses graceful `SIGTERM` only. A survivor is reported in the health state and
is never force-killed. This preserves fail-closed ownership when evidence is
ambiguous while preventing helper accumulation every five minutes.

References:

- [OpenAI Scheduled tasks](https://learn.chatgpt.com/docs/automations.md)
- [OpenAI Built-in browser](https://learn.chatgpt.com/docs/browser.md)
- [Apple: View memory usage in Activity Monitor](https://support.apple.com/en-au/guide/activity-monitor/-actmntr1004/mac)
- [Chromium: Memory Saver](https://developer.chrome.com/blog/memory-and-energy-saver-mode)
- [Codex issue 12491: MCP child process cleanup](https://github.com/openai/codex/issues/12491)
- [Codex issue 11324: MCP server accumulation](https://github.com/openai/codex/issues/11324)

## Post-update canary

1. Run the full unit test suite.
2. Run an empty schedule and confirm no Browser tab opened and the task
   completed normally.
3. Confirm the model-free janitor archives it after the minimum age without
   creating another task.
4. Wait for a real X event and confirm one global claim.
5. Let a second event arrive while the first claim is active.
6. Confirm the second run returns `owner_busy`, opens no Browser, and preserves
   `work_in_progress`.
7. After the first owner completes, confirm the second event can be claimed.
8. Compare `var/resource-health.json`, RSS, and renderer count before and after.
9. Interrupt a test owner and confirm recovery only after its related task
   becomes stale, without releasing a live `notLoaded` run.
10. After the canary completes, confirm its helper count drops, no current
    owner PID appears among targets, and the result is recorded in
    `var/session-janitor-health.json`.
