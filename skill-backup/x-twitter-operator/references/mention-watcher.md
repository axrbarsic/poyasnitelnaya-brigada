# X mention watcher

## Purpose

Use `/Users/alexlane/Developer/x-mention-watcher` for deterministic polling of
new direct replies to `@axrbarsic`. Polling and watchdog execution consume no
model tokens and create no Codex tasks. Official X API resource charges still
apply.

The watcher is read-only. It detects events and maintains state. It never
drafts, posts, deletes, likes, follows, or changes X account state.

## Security contract

- Keep the Bearer Token only in macOS Keychain under service
  `axrbarsic-x-mention-watcher` and account `axrbarsic`.
- Prefer `var/keychain-helper`, compiled from
  `scripts/keychain_helper.swift`, over the `security` CLI.
- Never print, log, screenshot, hash, measure, or place the token in argv.
- `config.json`, `var/`, databases, queues, health files, and secrets are
  ignored by Git.

## Queue contract

- The first successful live poll is a baseline and establishes `since_id`.
- Historical results are stored with state `baseline`, not queued.
- After baseline, only direct replies whose `in_reply_to_user_id` equals
  `16337609` enter the queue.
- Other mentions and nested replies are stored as `ignored`.
- Deduplicate by immutable X event ID.
- A successful poll alone may advance `since_id`. Failed polls never do.
- Use `baseline` only to recover an accidentally queued initial backlog. It
  preserves the cursor and event history.

## Operational commands

```bash
cd /Users/alexlane/Developer/x-mention-watcher
python3 xmention_watcher.py --config config.json preflight
python3 xmention_watcher.py --config config.json poll
python3 xmention_watcher.py --config config.json status
python3 xmention_watcher.py --config config.json watchdog
```

Use `ack EVENT_ID...` only after the Browser owner has classified or processed
those exact queued events.

## Installation gate

Before loading LaunchAgents, require all of the following:

1. Unit tests and plist validation pass.
2. A live poll succeeds with the Keychain token.
3. Repeated live polls return no duplicate IDs.
4. A temporary invalid environment token causes the configured failure state.
5. A following Keychain poll restores healthy state.
6. Manual Browser comparison finds no missed direct replies.
7. X API credits are positive and the spending cap is understood.

The installed poll interval is 300 seconds. The independent watchdog interval
is 60 seconds. Both write durable state and send local notifications only on
meaningful changes. Background stdout and stderr go to `/dev/null`.

If health is `billing_blocked`, do not keep polling. Restore X API credits
before loading or restarting the LaunchAgents.

## Event handoff

When the queue becomes non-empty:

1. The Browser owner opens each exact X event URL.
2. It performs the full thread, context, duplicate, and safety checks from this
   skill.
3. It checks the ledger for the parent publication type and historical
   ChatGPT conversation URL.
4. A follow-up to a prior Pro reply continues in that exact conversation with
   one screenshot and zero text.
5. A follow-up to a short reply is classified by Sol High.
6. Acknowledge the event only after skip or verified publication is durably
   recorded.

The watcher must not autonomously wake a model merely because polling occurred.
Use the durable queue and local notification as the boundary between free
mechanical detection and model work.
