# Operating on an 8 GB iMac

[English](memory-operations.md) | [Русский](memory-operations.ru.md)

## Proven root cause

The standalone Codex scheduled task created a new task every five minutes. Even
after completion, child `node_repl` and MCP helpers remained attached to the
long-lived Codex Desktop runtime. Process ages formed an exact five-minute
staircase. The sidebar filled with duplicate tasks while Electron redraw cost,
RSS, and swap grew.

Archiving reduced visual noise but did not remove the source. The five-minute
Codex automation is therefore retired.

## New low-memory contract

1. Polling, watchdog, janitor, and the Desktop supervisor are plain Python.
2. An idle cycle spends zero model tokens, creates no task, and starts no
   Desktop or Browser.
3. A ready queue launches Codex Desktop only when it is absent.
4. One existing Sol Max owner heartbeat atomically reserves the handoff and
   executes the matching claim in the same task.
5. An adjacent heartbeat cannot hand off the same queue again.
6. The Browser owner claims atomically and remains the sole owner.
7. A bounded claim uses at most three task-owned X tabs for independent
   read-only inspection.
8. Short and local-max share one ordered composer and publication lane. The
   local explainer opens no ChatGPT tab.
9. Every task-owned tab closes on a terminal outcome.
10. The dispatcher verifies that every original event ID left the queue.
11. A released unresolved claim is failure, not success.
12. Active voice pauses only Browser, never polling or the durable queue.
13. The janitor archives historical service tasks, while the new design no
    longer creates one task per minute.
14. After the queue empties, the supervisor closes only the exact Desktop PID
    it launched. It never closes a Desktop instance opened by Alex.

## Automatic profiles

`resource_mode: auto` changes only Browser admission. Sol Max, source checking,
and publication rules remain unchanged.

| Mode | Condition | Codex RSS | Renderers | Free memory | Swap |
| --- | --- | ---: | ---: | ---: | --- |
| Efficiency | voice or pressure | 2200 MiB | 5 | at least 20% | telemetry |
| Balanced | user active | 2350 MiB | 6 | at least 14% | telemetry |
| Performance | idle 15 minutes, AC, at least 35% free | 2500 MiB | 7 | at least 12% | telemetry |

Hard caps always apply: 3200 MiB RSS, 8 renderers, at least 10% free memory,
and strict helper-process limits.

Swap remains measured and stored for diagnosis, but by default it neither
selects a mode nor blocks Browser by itself. Its size includes historical
memory pressure and can remain high after RAM recovers. Only current low free
memory, excessive RSS, excessive renderers, or heavy helpers defer work.
The system never attempts a forced swap purge.

A high count of old lightweight helpers does not block forever when free
memory, Codex RSS, aggregate helper RSS, and renderer count are all inside the
recovery envelope. The default recovery ceilings are 3200 MiB Codex RSS and 8
renderer equivalents.
This exception exists only for already accumulated legacy processes. After
stale tasks are archived and Codex Desktop restarts once, legacy helpers should
disappear and stop accumulating.

## Voice priority

The minute watcher, SQLite, and queue continue during voice. The resource guard
blocks only the heavy Browser owner. The last microphone observation holds the
pause for `voice_priority_hold_seconds`, which defaults to 300 seconds.

Replies remain durable while realtime voice receives memory and CPU first.
After the hold expires, the ordinary Sol Max owner processes the queue.

## Measured cost

The historical Luna scheduled task averaged 49,064 processed tokens per empty
or deferred run. At five-minute cadence this was about 588,768 processed tokens
per hour.

The new idle path:

| Stage | Model tokens | New task | Browser |
| --- | ---: | ---: | ---: |
| X poll | 0 | 0 | no |
| Watchdog | 0 | 0 | no |
| Resource gate | 0 | 0 | no |
| Session janitor | 0 | 0 | no |
| Idle dispatcher | 0 | 0 | no |

In normal unattended idle, supervisor-owned Desktop is closed and the heartbeat
does not run. A ready queue starts the existing Sol Max owner directly, without
a second model turn for transport. If Alex intentionally keeps Desktop open,
the self-owned heartbeat continues its small scheduled gate. Substantive Sol
Max cost follows the number and complexity of real replies.

## Archiving

The official Codex documentation states that every standalone scheduled run
starts a new chat. A model-based housekeeping automation is unsuitable because
it creates more tasks.

`scripts/session_janitor.py` runs every minute as a LaunchAgent. It:

- archives only exact matching completed service tasks;
- protects the active and pinned Browser owner;
- retains restorable history;
- releases an orphaned claim only from strict state and timing evidence;
- terminates only helpers proven to belong to a completed service task;
- writes one replacing health state instead of an unbounded log.

The safest final cleanup for helpers left by historical scheduled runs is one
Codex Desktop restart at a safe boundary. The production dispatcher starts no
app-server and never depends on heuristically killing unrelated processes.

## Post-update proof

1. Run the unit test suite.
2. Run an idle dispatcher cycle and confirm zero new tasks.
3. Compare helper PID and RSS before and after idle, with no growth.
4. Wait for one real X event.
5. Confirm one self-owned Sol Max heartbeat and owner turn.
6. Verify the X URL, exact history, and durable resolution.
7. Confirm the original event leaves the queue.
8. Confirm an adjacent heartbeat is blocked by the reservation.
9. Delete the old paused automation through the official API only after the
   full new cycle is accepted.
10. Restart Codex Desktop once and record baseline RSS without legacy helpers.

References:

- [OpenAI Codex app-server](https://learn.chatgpt.com/docs/app-server)
- [OpenAI Scheduled tasks](https://learn.chatgpt.com/docs/automations)
- [OpenAI project config](https://learn.chatgpt.com/docs/config-file/config-advanced#project-config-files-codexconfigtoml)
- [Apple Activity Monitor](https://support.apple.com/en-au/guide/activity-monitor/-actmntr1004/mac)
- [Codex issue 12491](https://github.com/openai/codex/issues/12491)
- [Codex issue 11324](https://github.com/openai/codex/issues/11324)
