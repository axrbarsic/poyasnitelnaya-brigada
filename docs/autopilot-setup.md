# Codex autopilot setup

[English](autopilot-setup.md) | [Русский](autopilot-setup.ru.md)

## Purpose

The autopilot wakes an existing Codex Browser-owner task only when the durable
X queue contains an event. The watcher, dispatcher, and Luna automation never
draft or post. Sol High keeps all authenticated browsing and publication
decisions.

## Components

| Component | Frequency | Model | Responsibility |
| --- | --- | --- | --- |
| X watcher LaunchAgent | 5 minutes | none | Fetch, deduplicate, persist, queue |
| Watchdog LaunchAgent | 1 minute | none | Detect stale polling or repeated errors |
| Codex dispatcher automation | 5 minutes | Luna Low | Snapshot, persistent lease, one task wake |
| Existing Browser-owner task | event driven | Sol High | Context, facts, draft, post, verify |
| Custom GPT | only for Pro targets | configured Pro | Long reply in exact conversation |

## Configure the local dispatcher

Add this optional path to `config.json`:

```json
{
  "autopilot_state_file": "var/autopilot-dispatch.json"
}
```

If the scheduled task is projectless and its sandbox cannot write into the
repository, do not redirect state to `/tmp`. Use the read-only `snapshot`
command and keep the lease in the automation's persistent `memory.md`, which
Codex exposes as a supported writable location.

Verify an empty or pending claim:

```bash
python3 scripts/autopilot_dispatch.py \
  --config config.json \
  --lease-seconds 1800 \
  status
```

Do not commit `config.json`, `var/`, credentials, cookies, or runtime state.

## Create the Codex automation

Create one active five-minute Codex automation:

- model: `gpt-5.6-luna`
- reasoning: Low
- environment: local
- notifications: failed runs only
- target: the existing pinned Browser-owner task

The projectless automation prompt must:

1. Run one read-only `snapshot` command.
2. Compare pending IDs with `leased_event_ids` in persistent memory.
3. Remove resolved IDs and expire a lease after 30 minutes.
4. Exit silently when no ID is eligible.
5. Record eligible IDs before delivery.
6. Send one message to the exact existing task.
7. Override the destination turn to `gpt-5.6-sol`, High.
8. Never open Browser, X, or ChatGPT itself.
9. Remove newly leased IDs if task delivery fails.
10. Never create a new task per event.

Replace local paths and the Codex task ID with values from your machine. Keep
those machine-specific values outside the public repository.

## Destination wake contract

The Sol High wake message should require:

- exact event URLs from the claim;
- full live thread and media inspection;
- primary-source fact checking;
- thread plus ledger duplicate checks;
- short, Pro, or skip classification;
- exact historical custom GPT conversation for every Pro follow-up;
- composer and Unicode validation before one publication click;
- live reply URL verification;
- append-only conversation history and durable resolution;
- one final poll before the turn ends;
- one X tab, one ChatGPT tab, and one active Pro generation on an 8 GB Mac.

Standing autopilot authority should be explicitly granted by the account owner.
It should remain bounded to direct replies. Likes, reposts, follows, direct
messages, unrelated original posts, and deletions require separate authority.

## Failure behavior

- API failure never advances the X cursor.
- Malformed wake JSON fails before dispatcher state changes.
- A file lock serializes direct script claims.
- Persistent automation memory serializes the projectless scheduled lease.
- A 30-minute lease suppresses duplicate wakes.
- Failed task delivery removes newly leased IDs immediately.
- A crashed destination turn leaves the event unresolved, so it becomes
  eligible after lease expiry.
- Sol still checks live X and the ledger before every composer fill.

## Verification

Run:

```bash
python3 -m unittest discover -s tests -v
python3 scripts/autopilot_dispatch.py \
  --config config.json \
  --lease-seconds 1800 \
  status
```

Then use one real direct reply as a canary and verify:

1. Watcher detection creates one queue event.
2. The scheduled dispatcher creates one claim.
3. A second run inside the lease sends no duplicate wake.
4. Sol processes or durably skips the event.
5. The queue reaches zero.
6. Dispatcher state prunes the resolved ID.
