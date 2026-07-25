# Codex autopilot setup

[English](autopilot-setup.md) | [Русский](autopilot-setup.ru.md)

## Purpose

The autopilot starts Codex only when the durable X queue contains an eligible
event. Watcher and dispatcher checks use no model. Sol High keeps every
authenticated browsing and publication decision.

## Components

| Component | Frequency | Model | Responsibility |
| --- | --- | --- | --- |
| X watcher LaunchAgent | 5 minutes | none | Fetch, deduplicate, persist, queue |
| Watchdog LaunchAgent | 1 minute | none | Detect stale polling or repeated errors |
| Autopilot LaunchAgent | wake-file change, 1 minute fallback | none when empty | Lease and conditional Codex start |
| Lightweight Browser-owner task | event driven | Sol High | Context, facts, draft, post, verify |
| Custom GPT | only for Pro targets | configured Pro | Long reply in exact conversation |

## Configure the local dispatcher

Add these local values to `config.json`:

```json
{
  "autopilot_state_file": "var/autopilot-dispatch.json",
  "autopilot_health_file": "var/autopilot-health.json",
  "autopilot_last_message_file": "var/autopilot-last-message.txt",
  "autopilot_process_lock": "var/autopilot-resume.lock",
  "autopilot_owner_rotation_after_runs": 20,
  "browser_owner_thread_id": "REPLACE_WITH_CODEX_TASK_UUID",
  "browser_owner_cwd": "/absolute/path/to/browser-owner-workspace",
  "codex_cli_path": "~/.local/bin/codex"
}
```

Keep runtime state under ignored `var/`, not `/tmp`, so leases survive ordinary
restarts.

Verify an empty or pending claim:

```bash
python3 scripts/autopilot_dispatch.py \
  --config config.json \
  --lease-seconds 1800 \
  status
```

Do not commit `config.json`, `var/`, credentials, cookies, or runtime state.

## Initialize the Browser owner

Reuse or create one lightweight Codex task and keep it unarchived. In Codex
Desktop, run one read-only IAB preflight in that task and verify the expected X
account. This one app turn establishes Browser eligibility.

The local launcher later uses the official CLI:

```bash
codex exec resume --ephemeral TASK_UUID \
  -m gpt-5.6-sol \
  -c 'model_reasoning_effort="high"' \
  --skip-git-repo-check -
```

`--ephemeral` resumes the IAB-eligible task without creating another sidebar
task. Exact X conversation history comes from SQLite, the append-only ledger,
and recorded ChatGPT conversation URLs.

Codex CLI 0.146 still shows resumed ephemeral turns inside the owner task.
Keep this task dedicated, watch its cumulative history, and rotate to another
small preflight-verified owner before the context becomes large. Never point
the launcher at a long general-purpose X conversation.

`autopilot-health.json` keeps `completed_runs` and raises
`rotation_recommended` at the configured threshold. Rotation remains a
deliberate owner replacement because the new task must first pass an app IAB
preflight.

Do not use a bare `codex exec --ephemeral`: a fresh isolated session does not
inherit IAB eligibility. Do not resume a huge historical task either, because
its accumulated context defeats the token-saving design.

## Install the launcher

Render the three LaunchAgent files:

```bash
python3 scripts/render_launchd.py \
  --config /absolute/path/to/config.json \
  --output-dir /absolute/path/to/staging
```

Install and load `com.axrbarsic.xmention.autopilot.plist` together with poll and
watchdog. Its `WatchPaths` trigger reacts to queue-file changes, while the
one-minute interval is a recovery fallback. Empty runs exit before Codex starts.

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
- A process lock serializes launcher processes.
- Atomic JSON state serializes claims.
- A shared wake-file lock prevents a claim from reading through an atomic queue
  replacement.
- A 30-minute lease suppresses duplicate wakes.
- Failed Codex CLI start removes newly leased IDs immediately.
- A crashed Browser-owner run leaves the event unresolved, so it becomes
  eligible after lease expiry.
- Sol still checks live X and the ledger before every composer fill.
- A session-local IAB timeout does not prove that the shared backend is down.
  Start one fresh turn in the same Browser-owner task, run the official Browser
  bootstrap once, and resume only after the authenticated read-only preflight
  succeeds.
- Browser-owner runs reuse one dedicated task and do not create task-per-event
  clutter. Monitor and rotate that task before its context becomes large.
- A zero exit that leaves a leased event queued is recorded as
  `completed_unresolved`. Failures and unresolved completions produce one local
  macOS notification per state transition.

## Verification

Run:

```bash
python3 -m unittest discover -s tests -v
python3 scripts/autopilot_dispatch.py \
  --config config.json \
  --lease-seconds 1800 \
  status
python3 scripts/autopilot_resume.py \
  --config config.json \
  --lease-seconds 1800
```

Then use one real direct reply as a canary and verify:

1. Watcher detection creates one queue event.
2. The local launcher creates one claim and one ephemeral Sol run.
3. A second run inside the lease starts no duplicate Sol run.
4. Sol processes or durably skips the event.
5. The queue reaches zero.
6. Dispatcher state prunes the resolved ID.
7. No new Codex task appears and the lightweight owner remains within its
   chosen context budget.
