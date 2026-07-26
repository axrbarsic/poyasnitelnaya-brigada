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
8. Every run claims before loading skills.
9. The resource guard runs before Browser. `memory_deferred` preserves the
   queue unchanged.
10. A separate model-free LaunchAgent archives completed service tasks through
    the local Codex app-server. It creates no task and consumes no tokens.
11. The same LaunchAgent releases an orphaned claim only when its owning task
    is inactive and has not updated for at least five minutes.
12. After a scheduled run completes, the janitor correlates the exact task
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
| Efficiency | memory pressure | 2200 MiB | 5 | at least 20% | up to 896 MiB |
| Balanced | user is active | 2350 MiB | 6 | at least 14% | up to 1024 MiB |
| Performance | idle 15 minutes on AC power | 2500 MiB | 7 | at least 12% | up to 1152 MiB |

Every mode also obeys hard caps: 2700 MiB RSS, 8 renderers, 10 `node_repl`,
20 MCP helpers, at least 10% free memory, and no more than 1280 MiB swap.
Crossing a cap defers the next Browser owner and leaves every event durable.

Helper counts are early signals, not standalone proof of a leak. Their limits
stay above measured normal active state. If an individual system measurement
is unavailable, the guard records the error in `var/resource-health.json` and
never loses an event.

## Why completed runs are archived

The official Codex documentation states that every standalone scheduled run
starts a new chat. Self-archiving an active task can leave it in `active`. A
model-based housekeeping automation also failed because it created more tasks.
Instead, `scripts/session_janitor.py` runs as an ordinary LaunchAgent every
five minutes. It archives exact matching tasks older than 15 minutes, protects
the pinned task, and retains restorable history.

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
