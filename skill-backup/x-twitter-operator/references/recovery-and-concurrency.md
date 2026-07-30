# Browser recovery and concurrency

## Contents

1. Execution ownership
2. Failure classification
3. Screen lock and sleep
4. Safe parallelism
5. Recovery decision table

## 1. Execution ownership

Browser bindings and claimed tabs are execution-session state. Shared filesystem access does not imply shared Browser access.

A child session can see the same files but still receive an empty browser list. Do not infer that the Browser plugin is globally broken from a child-session failure.

Keep the original Browser owner alive for the whole authenticated workflow.

## 2. Failure classification

### Stale tab

Symptoms:

- tab missing;
- tab closed;
- tab is no longer part of the browser session.

Action:

- discard the tab binding;
- enumerate open tabs from the existing browser;
- reclaim the correct tab by URL and role.

Do not recreate the browser runtime.

### Browser disconnected

Symptoms:

- explicit browser-disconnected error;
- existing browser binding cannot communicate.

Action:

- read official bootstrap troubleshooting;
- retry the supported connection path once;
- preserve known URLs and the publication ledger.

### Browser unavailable in a fresh session

Symptoms:

- `Browser is not available: iab`;
- available browser list is empty in a child or newly created session.

Action:

- stop that session's UI work;
- do not switch browser when Alex explicitly selected the built-in Browser;
- keep the session on read-only research if useful;
- resume mutations only in one session that passes Browser preflight.

If this follows a local control-kernel timeout inside the established
Browser-owner task, start one fresh turn in that same task and run the official
bundled Browser bootstrap once. A successful authenticated read-only preflight
proves that the failure was turn-local. Do not create another Browser-owner
task and do not resume mutations before that preflight.

### Unverified submission

Symptoms:

- click timeout;
- composer state unknown;
- reply not immediately visible.

Action:

- inspect the thread and account search before retrying;
- never click twice without establishing that the first submission failed.

## 3. Screen lock and sleep

Treat these as different states:

- Screen locked: the Mac user session is protected.
- Display off: the display is dark, but the system may remain active.
- System asleep: processes and browser work may pause.

An already connected built-in Browser can sometimes continue while the screen is locked if the Mac remains awake. Verify with a read-only Browser operation instead of promising or refusing from the lock state alone.

Computer Use is a different mechanism. Operating visible desktop windows after lock requires its official Locked use setup. A Computer Use window error does not prove that the built-in Browser backend is unavailable.

## 4. Safe parallelism

Allowed parallel work:

- authoritative web research;
- source extraction;
- candidate classification from supplied public text;
- draft alternatives;
- factual review;
- duplicate-risk review from the ledger.

Single-owner work:

- authenticated tab discovery and claiming;
- navigation of X and any separately authorized visual-bot tab;
- typing into composers;
- clicking reply, delete, follow, like, or message controls;
- publication verification.

The local `poyasnitelnaya-brigada` route never opens ChatGPT or a custom GPT.
Its exact 4000-code-point output is generated inside the Sol Max owner turn.
Additional research sessions must return data to the owner instead of operating
authenticated tabs.

## 5. Recovery decision table

| Evidence | Meaning | Next action |
|---|---|---|
| Existing browser works, tab stale | Local tab binding failure | Reclaim tab |
| Original owner works, child has empty browser list | Session isolation | Keep Browser in original owner |
| Every eligible session lacks Browser | Backend unavailable | Stop UI work and report |
| Mac locked, original Browser read succeeds | Lock is not blocking Browser | Continue cautiously |
| Mac locked, Computer Use fails | Desktop UI access blocked | Configure Locked use later |
| Click timed out, reply visible | Submission succeeded | Record, do not retry |
| Click timed out, composer still filled, no reply found | Submission likely failed | Recheck once, then retry carefully |
