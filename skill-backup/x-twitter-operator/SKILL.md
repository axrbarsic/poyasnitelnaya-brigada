---
name: x-twitter-operator
description: Reliable operation of X/Twitter through Codex Browser, including search, thread inspection, replies, duplicate prevention, local «Пояснительная бригада» generation, external fact checking, follow-up continuity, monitoring, posting verification, and recovery after stale or unavailable browser sessions. Use whenever Alex asks Codex to read, search, scroll, monitor, reply, publish, delete, or otherwise act on X/Twitter.
---

# X / Twitter Operator

## Core contract

Treat browser state as a single-owner resource.

1. Assign exactly one execution session as Browser owner.
2. Keep every authenticated UI mutation in that session.
3. Use parallel sessions only for read-only research, source verification, candidate triage, or draft preparation.
4. Never expect a child session to inherit an existing Browser binding, claimed tab, login state, or persistent JavaScript runtime.
5. Never let two sessions operate the same X tab concurrently.

Read and follow the bundled `browser:control-in-app-browser` skill before Browser work. If Alex explicitly selects the built-in Browser, do not silently switch to Chrome, Computer Use, Playwright CLI, or another browser.

## Cost-aware model routing

Keep the Browser owner and final publication brain on `gpt-5.6-sol`.

- Every standalone reply written without «Пояснительная бригада» must be authored and final-checked by Sol High or stronger.
- Every turn that may invoke `poyasnitelnaya-brigada-v2` must run on Sol Max. Do not generate its output in High and do not delegate its writing to another model. Keep `poyasnitelnaya-brigada` v1 unchanged and use it only when Alex explicitly asks for v1.
- Only Sol may decide the live target, resolve contextual ambiguity, classify `short`/`local-max`/`satirical-media`/`already-answered`, operate authenticated tabs, validate the final composer, or publish.
- Use deterministic scripts before any model for ledger lookup, state counting, exact duplicate IDs, Unicode length, forbidden-character scans, and queue timestamps.
- Use Luna Low only for bounded read-only mechanical work on supplied artifacts. It must not browse, research, draft replies, interpret context, or mutate state.
- Use Terra Medium only for one bounded read-only research packet from current primary sources. It must not draft the final reply, personalize political messaging, operate authenticated tabs, or mutate state.
- Treat helper output as evidence. Sol must inspect the live thread, decide, write, validate, and publish.
- Do not spawn a helper merely to wait. Do not fan out overlapping work because every subagent consumes its own tokens.
- Give helpers only target-local context, exact inputs, required output, and a stop condition. Do not fork the full conversation for mechanical or research work.

When the working directory supports project-scoped agents, prefer narrow `x_mechanical` and `x_researcher` profiles over changing global model defaults.

## Authorization boundary

Distinguish analysis from publication.

- A request to inspect, diagnose, search, summarize, or draft does not authorize posting, deleting, following, liking, messaging, or changing account state.
- Publish only when Alex explicitly asks for the post, reply, or clearly bounded batch.
- If Alex explicitly grants standing autopilot authority, treat later queued
  eligible replies and conversation continuations as the same bounded reply
  workflow until Alex revokes it.
  This standing authority covers contextual inspection, fact checking, short
  or local Sol Max routing, publication, verification, and durable resolution. It does not
  cover likes, reposts, follows, direct messages, unrelated original posts, or
  deletion of existing posts.
- Delete or replace an existing post only when Alex explicitly authorizes that exact effect.
- Do not expose credentials, cookies, local storage, session files, or authentication material.
- Use the normal signed-in browser flow. If authentication is missing, ask Alex to sign in to the selected browser.

## Preflight

Complete this sequence before a batch:

1. Confirm the requested browser and load its official skill.
2. Query deferred tools for a purpose-built X connector. Use it only if it supports the required operation and Alex did not explicitly require Browser.
3. In the Browser owner session, reuse an existing browser binding. Do not initialize a second runtime when one exists.
4. Discover open tabs and claim them by origin and purpose, never by array position.
5. Build a role map such as `x-main` and `research-1`.
6. Verify the X account identity expected for the task. Alex's default posting account is `@axrbarsic`.
7. Run a read-only test: inspect the current X tab and confirm its URL and page state.
8. Create an in-run ledger containing target post ID, target URL, author, reply type, duplicate-check result, generation skill and model when applicable, publication state, and reply URL.

If the read-only test fails, do not begin research that assumes later publication will work.

## Tab ownership

Use the smallest stable tab set:

- The production owner claim holds one event and one task-owned X tab. A
  bounded multi-tab read-only experiment requires an explicit runtime override
  and never changes the single writer lane.
- Keep exactly one writer lane. Never fill composers in two tabs at once, and
  never overlap publication, verification, history import, or resolve.
- Optional research tabs only when a connector or direct web lookup cannot cover the source.

Reuse tabs instead of opening duplicates. Do not close or navigate a tab owned by another session. When a tab binding becomes stale, discard only that binding and reacquire the tab from the existing browser. Do not reinitialize the browser for an ordinary stale-tab error.

Before switching a tab, record its role and current URL. After switching, verify origin and page identity before typing or clicking.

For Browser 26.727 or newer:

- The address bar and Browser history may help reacquire a task-owned page, but
  they are navigation aids only. Never use browsing history as the durable X
  queue, conversation memory, duplicate ledger, or proof of publication.
- The built-in Browser has a profile separate from regular Chrome. Never assume
  that a Chrome tab, login, or extension state is available in the built-in
  Browser.
- Use Computer Use to inspect rendered state and verify the exact target before
  a mutation. Reusing a URL from history does not waive target revalidation.
- Full CDP Developer mode is an optional diagnostic path for console, network,
  DOM, style, or performance failures. It requires explicit approval and is not
  part of routine X reading, composing, or publication.
- Chrome extension context, page right-click actions, and YouTube helpers do not
  replace the authenticated built-in Browser owner for X.

On Alex's 8 GB iMac, let the resource guard choose efficiency, balanced, or
performance automatically. The profile controls whether Browser work may
start, never the required quality of Sol reasoning or fact checking:

- idle dispatcher checks own zero Browser tabs and use no model;
- a production claim contains exactly one oldest pending event;
- short and Local Sol Max work use one X tab and complete one durable
  transaction before the next claim;
- Local Sol Max work generates locally through `poyasnitelnaya-brigada-v2` in
  the Sol Max owner turn;
- `initial-audit-next --conversations 1`, never routine `status --full`;
- no helper session for waiting, polling, or mechanical age filtering;
- close old backlog with one
  `initial-audit-expire --hours H --as-of <UTC>` run, not Browser tabs.
- close every task-owned Browser tab before the owner turn exits;
- run `autopilot_bridge gate` in the model-free dispatcher before starting
  Desktop or any model;
- on `dispatch=true`, launch Codex Desktop only when absent. One existing
  in-app Luna Low heartbeat must call one `relay-reserve-handoff`. Python
  chooses repair or X and returns one unambiguous `dispatch` plus `route`. Its
  atomic reservation suppresses adjacent heartbeat ticks. The relay must invoke
  `send_message_to_thread` exactly once without reading owner status. Codex
  queues or steers a follow-up when a turn is active, while the
  global owner claim serializes Browser work;
- if delivery fails, release only that exact reservation with
  `release-handoff` and leave the queue pending;
- let the reservation suppress adjacent heartbeat ticks before the owner claim
  becomes visible;
- never use the external app-server relay in production. It has no Codex
  Desktop Browser session and remains only a disabled diagnostic fallback;
- after the queue empties, close only the exact Desktop PID started by the
  supervisor. Never close a Desktop instance opened by Alex;
- require the dispatcher to verify that every original event ID left the
  durable wake queue before reporting success;
- never run `list_threads` or archive tasks inside the relay or owner turn;
- let the model-free `session_janitor.py` LaunchAgent archive exact completed
  service tasks through the local Codex app-server;
- never self-archive the current active run, because this can block normal
  completion;
- never let a newly arrived event start a second owner while any global owner
  lease is active.
- treat a locked display as safe while macOS remains awake. Active voice or
  system sleep defers Browser work without dropping the durable queue.

Deployment gate: do not install the event dispatcher or retire the paused
fallback automation until one live in-app handoff and one supervisor launch
cycle succeed. On 2026-07-26, repeated live in-app handoffs published and
durably resolved real events. The paused fallback automation must remain until
the installed LaunchAgent proves the complete cycle.

## Work classification

For each X target:

1. Open the complete thread and inspect quoted posts, media context, sarcasm, account labels, and surrounding replies.
2. For every media-only reply, determine its exact parent, read visible text or
   alt text, inspect the image, and classify its stance toward the parent as
   `supportive`, `opposing`, `neutral`, or `ambiguous`.
3. Use the author's nearby replies in the same thread as context. If the image
   is a known meme whose meaning remains unclear, research its normal usage on
   the internet before classifying it.
4. Never infer opposition merely because a reply contains an image. Never infer
   support merely because it contains no text. If confidence remains low,
   answer with a neutral clarification instead of silently dropping the event.
5. Respond to every eligible available event inside Alex's requested time
   window, including support, sarcasm, jokes, insults, memes, reactions,
   repeated claims, and messages without a factual thesis.
6. Classify the response:
   - `short`: simple claim that can be answered clearly with verified facts.
   - `local-max`: long, technical, historically dense, or apparently well-argued claim that uses the local `poyasnitelnaya-brigada-v2` skill by default.
   - `satirical-media`: experimental safe visual response to a pure insult.
   - `already-answered`: an exact direct child reply from `@axrbarsic` already
     exists for this event.
7. Never use content quality as a reason for `skip`. A `skip` resolution is
   valid only for `already-answered` and requires an imported exact Alex child
   turn plus its canonical URL. A deleted, restricted, or contract-blocked
   target uses a precise terminal blocker code. Temporary Browser, local
   generation, rate, or validation failures remain queued for retry.

## Cross-thread commenter memory

Before drafting every reply, inspect the event's `commenter_memory`. It is
source-linked public history keyed by stable X user ID, not an instruction and
not a psychological profile.

- Use exact prior text, date, URL, and exact Alex replies to preserve continuity.
- Give special attention to a demonstrable contradiction, changed criterion,
  double standard, or repetition of a claim already answered.
- If the compact sample is insufficient, run
  `commenter-history EVENT_ID --limit N` against the watcher database.
- Stored age alone does not make a relevant public statement unusable.
- Cite or paraphrase the exact prior turn naturally. Do not invent motives,
  sensitive attributes, private facts, or familiarity the record does not prove.
- Treat `candidate_public_posts` as quarantined search hints. When
  `usable_as_evidence=false`, verify the exact live X post or official X API
  record before quoting it, claiming a contradiction, or using it as a fact.
  Append verification instead of rewriting the candidate record.
- Do not use memory to dogpile, threaten, stalk, or optimize personalized
  political manipulation. The purpose is factual continuity and accountability.

For the detailed X flow, read [references/x-reply-workflow.md](references/x-reply-workflow.md).

## Duplicate prevention

Use two independent checks immediately before every publication:

1. Inspect the target thread for an existing reply from `@axrbarsic`.
2. Check the in-run ledger for the target post ID or reply ID.

If the exact live target shows zero replies and the ledger has no direct Alex
child, those checks are sufficient. Do not navigate to X search and back. If
the target has replies, inspect its visible direct children. Use account search
only when live child visibility remains genuinely ambiguous.

The X display name is not provenance. An account named «Пояснительная
бригада» may still contain a self-authored Sol reply. Determine `short` or
`local-max` only from the durable ledger, exact conversation turns, source
session, or payload record. Never infer provenance from the display name.

When Alex manually publishes a reply created by «Пояснительная бригада», keep
origin provenance separate from continuation mode. Never rewrite an unknown
manual origin as `provenance=pro`. Read the deterministic
`manual_parent_continuation` profile from the claim payload. A proven local-max
origin, at least 500 Unicode code points, at least three paragraphs, or at
least one source URL routes the follow-up to local `poyasnitelnaya-brigada-v2`
with complete SQLite history. A concise exact parent with none of those signals
may use Sol `short`. If the profile says `pending_exact_parent_restore`, restore
and durably import the exact live X parent first, then apply the same adaptive
classification. Preserve `manual_unknown` when origin remains unproven. Never
open ChatGPT web for either route.

When practical, also search the account's replies using the target author or a distinctive phrase. Treat every prior bot-generated reply as an `@axrbarsic` reply.

If any check is uncertain, do not publish until resolved. Never count a skipped duplicate as a completed reply.

## Fact checking and response quality

Perform a live internet check before drafting every factual X reply, even when the claim seems familiar. Verify unstable or contested claims against current evidence. Prefer primary and authoritative sources, including official documents, courts, international organizations, election monitors, and original statistics.

Group the material factual propositions into one search call with no more than
four target-local queries. Make another search call only for a named unresolved
gap. A primary source already stored in the exact durable chain may be reused
after confirming that it is still available, current where relevant, and
supports the present sentence.

For a `local-max` reply, use the verified research directly while applying
`poyasnitelnaya-brigada-v2`. The skill and final publication brain are the same
Sol Max turn, so no external bot handoff exists.

Keep replies focused on claims, evidence, logic, and contradictions. Do not:

- insult or degrade the author;
- target protected traits;
- threaten, dogpile, or repeatedly pursue one person;
- invent facts, sources, quotations, or current events;
- copy graphic or hateful material unless the minimum context is necessary to rebut it;
- automate engagement spam or personalized political manipulation.

Publish exactly one reply per new eligible target. For a supportive reaction,
acknowledge it briefly. For a joke or sarcasm, answer in context. For an insult,
respond with calm intellectual superiority, evidence when a factual claim
exists, and no reciprocal abuse.

For an experimental satirical visual reply to a pure insult:

- for an explicitly selected `377` route, load the local `377` skill and use
  the image generation tool without opening ChatGPT;
- for an explicitly selected `Ложкин` route, use exactly one custom ChatGPT
  web bot;
- provide only the target post and the minimum thread context needed to
  understand the exchange;
- satirize the rhetorical move or argument, not the author's body, dignity,
  protected traits, private life, or invented conduct;
- inspect the generated image before attachment;
- keep factual rebuttal and primary-source support in text when the target
  contains a factual claim;
- fall back to a Sol text reply if the bot, image, or context check fails.

## Text validation

Before filling the composer, validate the source text:

```bash
python3 <skill-dir>/scripts/validate_reply.py --file /path/to/reply.txt --max 4000
```

For the local «Пояснительная бригада» contract:

```bash
python3 <skill-dir>/scripts/validate_reply.py \
  --file /path/to/reply.txt \
  --strip-one-final-newline \
  --non-empty \
  --max 4000
```

Resolve `<skill-dir>` as the directory containing this `SKILL.md`.

The validator rejects literal U+2014, U+2013, non-breaking spaces, zero-width characters, and unsafe control characters.

After filling the actual X composer, read its DOM value and repeat the length and forbidden-character checks on that value. Source validation alone is not sufficient because paste and rich text handling can change content.

For the X DraftJS contenteditable composer, the canonical DOM value is the
ordered sequence of elements with `data-block="true"`: take each block's
`textContent` and join the blocks with one literal `\n`. Do not validate raw
`innerText`, because it can insert a presentation-only extra newline between
DraftJS blocks and falsely report a source over the publication limit.
Record the block count, reconstructed code-point count, exact source match,
and forbidden-character scan in evidence.

## Пояснительная бригада

Use the local `poyasnitelnaya-brigada-v2` skill by default for `local-max`
targets. Use `poyasnitelnaya-brigada` v1 only when Alex explicitly requests the
old version.

- Require `gpt-5.6-sol` with reasoning effort `max` before invoking the skill.
- Never open ChatGPT, the custom GPT, or a ChatGPT conversation for generation.
- For a new target, give the skill the exact live author, complete target text,
  quoted material that belongs to the target, relevant media meaning, verified
  primary-source research, and the durable X chain.
- For a follow-up whose parent has legacy `provenance=pro`, load the complete
  exact local chain with `history-show` and apply the same skill to the new turn. An old
  `chatgpt_conversation_url` is archival metadata only and must not be opened.
- Generate one non-empty publication-ready Russian monologue of at most 4000
  Unicode code points. Do not target the maximum and do not pad with filler.
- Validate with:

  ```bash
  python3 <skill-dir>/scripts/validate_reply.py \
    --file /path/to/reply.txt \
    --strip-one-final-newline \
    --non-empty \
    --max 4000
  ```

  Then validate the actual X composer value again.
- Record `generation_profile=local_sol_max`,
  `generation_skill=poyasnitelnaya-brigada-v2`,
  `generation_model=gpt-5.6-sol`, `reasoning_effort=max`, source URLs, target
  URL, exact text hash, and verified reply URL in durable evidence. Keep
  `provenance=pro` only as the legacy database compatibility marker.
- Store every exact X turn in the existing SQLite and JSONL history. Local
  conversation history, not a browser chat, is the canonical continuation
  memory.
- For an autopilot claim, write one terminal `evidence.json` per event and run
  `scripts/commit_browser_owner_event.py` immediately. That committer derives
  the route, event history, ledger and durable resolution. Never construct the
  generated JSONL files or call history import and resolve separately. After
  every event reports `status=committed`, run
  `scripts/finalize_browser_owner_session.py` once for the claim and require a
  complete manifest before `completed`.
- The terminal target must exactly match the stored API text, author ID,
  parent status ID and conversation ID. A `local_sol_max` outcome names
  `poyasnitelnaya-brigada-v2`; a `sol_short` outcome names no skill. ChatGPT
  web is forbidden except for an explicitly authorized `lozhkin_web` visual
  route.

Read [references/local-sol-max-explainer-contract.md](references/local-sol-max-explainer-contract.md)
before using the local skill.

## Token-free mention monitoring

For recurring checks of new replies, use the deterministic local watcher and a
model-free LaunchAgent dispatcher. The
watcher polls the official X API without invoking a model, stores a durable
cursor, and queues:

- every direct reply to `@axrbarsic`;
- every new reply in a conversation whose exact stored history contains an
  `alex` turn.

This rule is universal. Never add topic names, post IDs, authors, or special
article lists to make detection work.

When `mandatory_response_mode=true`, queue every reply returned by the
authenticated mentions endpoint, including a reply whose immediate parent is
another participant. X may carry `@axrbarsic` through the participant list
while an older local chain lacks the Alex root turn. Live Browser inspection,
not incomplete historical backfill, decides whether and how to answer.

When `mandatory_response_mode=true`, the deterministic resolver rejects every
content-based `skip`. It accepts `skip` only after exact history contains a
direct Alex child reply to the event. It accepts `blocked` only for an allowed
terminal blocker code. Use `mandatory-response-requeue --hours H --as-of <UTC>`
to auditably requeue recent historical skips and ignored mention replies
without supplying target IDs.

- At cycle start, use the lookback Alex explicitly requests, for example
  run `initial-audit-start`, then
  `initial-audit-expire --hours 3 --as-of <UTC> --dry-run`, followed by the
  apply command with the same hours and identical `--as-of`. Older unresolved
  eligible replies receive exact stored history plus an age-policy skip without
  Browser or model use.
- Fix the cutoff once. Do not rerun expiry during the active cycle. Continue
  every newly arriving eligible reply until resolved, even after its original
  timestamp passes the initial cutoff.
- Never put X credentials in config, logs, Git, task prompts, or process
  arguments. Use the native Keychain helper.
- Do not install or load LaunchAgents until replay tests, live deduplication,
  controlled failure, recovery, and Browser comparison are all green.
- The watcher may detect and queue work, but it must never draft or publish.
  Sol and the Browser owner retain classification and publication.
- A queued local Sol Max follow-up must load the exact recorded local X history and use
  `poyasnitelnaya-brigada-v2` in Sol Max by default.
- Run poll, watchdog, janitor, and the Python event dispatcher every minute on
  the 8 GB iMac. Empty, leased, voice-paused, and resource-deferred checks stop
  without a model, Browser, or new Codex task.
- When `dispatch=true`, the supervisor launches Desktop only if needed. The
  existing in-app Luna heartbeat calls one `relay-reserve-handoff`. Python
  chooses repair or X and returns one unambiguous `dispatch` plus `route`.
  The command atomically reserves one handoff without a model. After a
  reservation, Luna calls the direct Codex app tool exactly once with
  `gpt-5.6-sol` and `max`, without reading owner status. A delivery failure
  releases the exact reservation without touching the queue. The
  pinned Sol owner atomically claims the batch and executes the queued or
  steered wake prompt.
- In normal unattended idle, supervisor-owned Desktop is closed and Luna does
  not run. If Alex intentionally keeps Desktop open, the heartbeat still
  performs its small scheduled gate, but it never wakes Sol for an empty queue.
- Keep zero Browser tabs while idle and one task-owned X tab during a
  production claim. Finish the event end-to-end before the next claim. A
  larger read-only tab set is a temporary diagnostic override, not the normal
  backlog strategy.
- Close all task-owned tabs and finish normally. The model-free session
  janitor archives the task after its minimum age. `notLoaded` alone is not
  proof that a live owner died.
- A durable resolution remains successful even if a final API poll cannot read
  Keychain. Record `completed_with_warning`; do not release or republish the
  resolved event. The LaunchAgent owns the next token-free poll.

Read [references/mention-watcher.md](references/mention-watcher.md) before
installing, diagnosing, or operating the local watcher.

## Publication transaction

Treat each reply or multi-part reply thread as one transaction:

1. Reopen or verify the exact target.
2. Repeat duplicate checks.
3. Validate the source text.
4. Fill the composer.
5. Read and validate the actual composer value.
6. Confirm the target and account once more.
7. Bind the publish control to the filled visible composer through their
   nearest shared DOM container. Never use page-global
   `tweetButtonInline.first()` or `.last()` without evidence that it belongs to
   that composer. Click the paired control once.
8. Wait at least three seconds, then verify that the composer cleared and the
   exact new reply appears in the thread or account search. If publication is
   proven absent, no new Alex child exists, and the composer still exactly
   matches the source, allow one Enter activation on the same paired button.
   Never use a keyboard submit shortcut while focus is in the composer. If the
   composer changed or duplicated, replace only that task-owned tab, refill
   from the exact file, and repeat the full checks. After one unverified fresh
   tab attempt, leave the event unresolved with durable evidence.
9. Obtain the canonical URL from the exact new Alex article. If X visually
   truncates a long Note Tweet, do not click `Show more` only to reconstruct
   the full body. Record UI author, parent, URL, prefix and suffix, then require
   the official exact-text API verifier before durable commit.
10. For a multi-part payload, open the verified reply URL, publish the next
   exact part as its child, and repeat until the complete chain is verified.
11. Record every part index, exact text hash, status ID, parent status ID, and
    reply URL in the ledger.

Run the queue gate and duplicate check immediately before the first part.
After the first part is published, finish the already-started thread transaction
without interleaving unrelated work. If a later part fails, record
`partial_thread_unverified`, preserve every verified part URL, and resume only
from the first missing part after proving the existing prefix.

Do not report publication success after a click alone. A timeout or unchanged composer means `unverified`, not `published`.

## Recovery

Separate these failure classes:

- `stale tab`: reacquire the tab from the existing browser.
- `browser disconnected`: follow official bootstrap troubleshooting once.
- `Browser is not available`: stop UI work and report that the current execution session lacks the selected backend.
- `screen locked`: do not assume failure. A persistent built-in Browser may continue while the Mac remains awake.
- `Computer Use window unavailable`: treat as a separate UI-access issue, not proof that built-in Browser failed.

If the Browser owner loses access, do not solve it by launching multiple child Browser operators. Keep research and drafts, pause mutations, and resume only in one Browser-capable session that passes preflight.

A local IAB timeout or reset inside one turn does not by itself prove that the
shared Browser backend is unavailable. Keep the same Browser-owner task, start
one fresh turn, run the official bundled Browser bootstrap once, and require a
successful authenticated read-only preflight before resuming mutations.

Read [references/recovery-and-concurrency.md](references/recovery-and-concurrency.md) when Browser availability, screen locking, session interruption, or delegation is involved.

When Alex reports a missed reply or asks for a clean experiment, read
[references/reliability-debugging.md](references/reliability-debugging.md).
Diagnose the complete pipeline, fix only a universal rule, then use a
target-agnostic replay so the ordinary automation discovers the event without
receiving its ID.

## Reporting

During long work, report verified progress after meaningful batches and at least once per minute while actively working. Include:

- candidates checked;
- duplicates skipped;
- short replies published;
- local Sol Max replies published;
- local Sol Max drafts rejected by exact validation;
- blockers.

At completion, distinguish `published`, `skipped`, `unverified`, and `blocked`. Never inflate the completed count.
