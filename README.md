# X mention watcher

Read-only polling for new X mentions and replies with zero model-token use.

The watcher uses the official X API user mentions endpoint with `since_id`. It
stores immutable event IDs in SQLite, writes a durable pending queue, and
maintains a health file. A separate watchdog detects stale polling and repeated
failures. The official X API still requires an X API Bearer Token.

It does not:

- draft or publish replies;
- access Browser cookies, storage, or credentials;
- wake Codex autonomously;
- store API tokens in files;
- decide whether an event deserves a response.

## Current checkpoint

The live gate passed on 2026-07-24. The poll and watchdog LaunchAgents are
installed for `@axrbarsic`, the watcher is healthy, and the queue is empty.
Polling runs every five minutes and the watchdog runs every minute.

Live polling also requires a positive prepaid X API credit balance. With a
recognized token and no credits, X returns HTTP 402 with the
`credits-depleted` problem type. The watcher reports this as
`billing_blocked` and must remain unloaded until credits are available.

## Configure

Copy `config.example.json` to `config.json`, then set the numeric X user ID.
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

Compile the helper locally before live use:

```bash
mkdir -p var
xcrun swiftc -framework Security \
  scripts/keychain_helper.swift \
  -o var/keychain-helper
```

Store a token without putting it in process arguments:

```bash
printf '%s' "$X_BEARER_TOKEN" | \
  var/keychain-helper set axrbarsic-x-mention-watcher axrbarsic
```

The helper also supports metadata-only verification:

```bash
var/keychain-helper exists axrbarsic-x-mention-watcher axrbarsic
```

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

## Live one-shot poll

```bash
python3 xmention_watcher.py --config config.json poll
```

On a new empty live database, the first successful poll is an automatic
baseline. It stores historical events and establishes `since_id` without
queueing the archive as new work. Later polls queue only direct replies whose
`in_reply_to_user_id` matches the configured user.

If an older build accidentally queued the initial archive, preserve the event
history and cursor while clearing only that queue:

```bash
python3 xmention_watcher.py --config config.json baseline
```

## Watchdog

One-shot health check:

```bash
python3 xmention_watcher.py --config config.json watchdog
```

Continuous shadow watchdog:

```bash
python3 xmention_watcher.py --config config.json watchdog --loop
```

The watchdog is intentionally separate from the poller. If the poller dies, it
cannot report its own death. The watchdog detects an old `last_success_at`,
repeated failures, and a long period with no new events that deserves a manual
cross-check. On macOS it sends a local notification only when health status
changes. New queued X events also produce one local notification per successful
poll that finds new IDs.

The poller uses a process lock, rejects repeated or excessive pagination, and
never advances `since_id` when a request fails. Background stdout and stderr go
to `/dev/null`; durable diagnostics remain in SQLite and the health JSON so log
files cannot grow without bound.

## Background service

The `macos/` directory contains LaunchAgent templates for a five-minute poll
and a one-minute independent watchdog. They run only Python and macOS system
tools, so idle checks consume no model tokens and create no Codex tasks.

Do not install the LaunchAgents on another machine until a live shadow run with
the official X API has matched a manual Browser scan.

After that gate, render machine-specific plists into a staging directory:

```bash
python3 scripts/render_launchd.py \
  --config /absolute/path/to/config.json \
  --output-dir /absolute/path/to/staging
```

Rendering validates both plists. Loading them with `launchctl` is a separate,
explicit production step.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Skill backup

`skill-backup/x-twitter-operator` is a restorable snapshot of the installed
Codex skill contract that governs detection, Browser ownership, duplicate
checks, Pro continuity, and publication. Runtime state and credentials remain
outside this repository.

Official references:

- https://docs.x.com/x-api/users/get-mentions
- https://docs.x.com/x-api/posts/timelines/quickstart/user-mention-quickstart
- https://docs.x.com/x-api/fundamentals/rate-limits
- https://docs.x.com/x-api/getting-started/pricing
