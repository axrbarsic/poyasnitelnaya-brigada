---
name: x-twitter-operator
description: Reliable operation of X/Twitter and related authenticated browser tabs through Codex Browser, including search, thread inspection, replies, duplicate prevention, multi-tab ownership, external fact checking, long ChatGPT Pro generation, follow-up continuity, monitoring, posting verification, and recovery after stale or unavailable browser sessions. Use whenever Alex asks Codex to read, search, scroll, monitor, reply, publish, delete, or otherwise act on X/Twitter, or to coordinate X with ChatGPT or another browser tab.
---

# X / Twitter Operator

## Core contract

Treat browser state as a single-owner resource.

1. Assign exactly one execution session as Browser owner.
2. Keep every authenticated UI mutation in that session.
3. Use parallel sessions only for read-only research, source verification, candidate triage, or draft preparation.
4. Never expect a child session to inherit an existing Browser binding, claimed tab, login state, or persistent JavaScript runtime.
5. Never let two sessions operate the same X or ChatGPT tab concurrently.

Read and follow the bundled `browser:control-in-app-browser` skill before Browser work. If Alex explicitly selects the built-in Browser, do not silently switch to Chrome, Computer Use, Playwright CLI, or another browser.

## Cost-aware model routing

Keep the Browser owner and final publication brain on `gpt-5.6-sol` with `high` reasoning.

- Every standalone reply written without «Пояснительная бригада» must be authored and final-checked by Sol High.
- Only Sol High may decide the live target, resolve contextual ambiguity, classify `short`/`pro`/`skip`, operate authenticated tabs, validate the final composer, or publish.
- Use deterministic scripts before any model for ledger lookup, state counting, exact duplicate IDs, Unicode length, forbidden-character scans, and queue timestamps.
- Use Luna Low only for bounded read-only mechanical work on supplied artifacts. It must not browse, research, draft replies, interpret context, or mutate state.
- Use Terra Medium only for one bounded read-only research packet from current primary sources. It must not draft the final reply, personalize political messaging, operate authenticated tabs, or mutate state.
- Treat helper output as evidence. Sol High must inspect the live thread, decide, write, validate, and publish.
- Do not spawn a helper merely to wait. Do not fan out overlapping work because every subagent consumes its own tokens.
- Give helpers only target-local context, exact inputs, required output, and a stop condition. Do not fork the full conversation for mechanical or research work.

When the working directory supports project-scoped agents, prefer narrow `x_mechanical` and `x_researcher` profiles over changing global model defaults.

## Authorization boundary

Distinguish analysis from publication.

- A request to inspect, diagnose, search, summarize, or draft does not authorize posting, deleting, following, liking, messaging, or changing account state.
- Publish only when Alex explicitly asks for the post, reply, or clearly bounded batch.
- If Alex explicitly grants standing autopilot authority, treat later queued
  direct replies as the same bounded reply workflow until Alex revokes it.
  This standing authority covers contextual inspection, fact checking, short or
  Pro routing, publication, verification, and durable resolution. It does not
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
5. Build a role map such as `x-main`, `chatgpt-pro-1`, and `research-1`.
6. Verify the X account identity expected for the task. Alex's default posting account is `@axrbarsic`.
7. Run a read-only test: inspect the current X tab and confirm its URL and page state.
8. Create an in-run ledger containing target post ID, target URL, author, reply type, duplicate-check result, bot session URL when applicable, publication state, and reply URL.

If the read-only test fails, do not begin research that assumes later publication will work.

## Tab ownership

Use the smallest stable tab set:

- One primary X tab.
- One ChatGPT tab by default.
- One additional ChatGPT tab for each concurrent Pro request only when parallel waiting materially helps.
- Never run more than three simultaneous «Пояснительная бригада»
  conversations. A fourth Pro target waits for a slot.
- Optional research tabs only when a connector or direct web lookup cannot cover the source.

Reuse tabs instead of opening duplicates. Do not close or navigate a tab owned by another session. When a tab binding becomes stale, discard only that binding and reacquire the tab from the existing browser. Do not reinitialize the browser for an ordinary stale-tab error.

Before switching a tab, record its role and current URL. After switching, verify origin and page identity before typing or clicking.

On Alex's 8 GB iMac, use the low-memory profile by default:

- one Browser owner, one X tab, and one ChatGPT tab;
- at most one active Pro conversation at a time;
- `initial-audit-next --conversations 1`, never routine `status --full`;
- no helper session for waiting, polling, or mechanical age filtering;
- close old backlog with one
  `initial-audit-expire --hours H --as-of <UTC>` run, not Browser tabs.

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
   support merely because it contains no text. If confidence remains low, mark
   it `ambiguous` and do not publish automatically.
5. Skip satire, misclassified posts, context that reverses the apparent meaning, and targets already handled.
6. Classify the response:
   - `short`: simple claim that can be answered clearly with verified facts.
   - `pro`: long, technical, historically dense, or apparently well-argued claim that benefits from the custom GPT.
   - `skip`: duplicate, bait without substance, unsafe target, unverifiable claim, or low-value repetition.
7. Prefer one useful response over engagement for its own sake.

For the detailed X flow, read [references/x-reply-workflow.md](references/x-reply-workflow.md).

## Duplicate prevention

Use two independent checks immediately before every publication:

1. Inspect the target thread for an existing reply from `@axrbarsic`.
2. Check the in-run ledger for the target post ID or reply ID.

The X display name is not provenance. An account named «Пояснительная
бригада» may still contain a self-authored Sol reply. Determine `short` or
`pro` only from the durable ledger, source session, payload record, or exact
ChatGPT conversation URL. Never invent a historical Pro conversation from the
display name.

When practical, also search the account's replies using the target author or a distinctive phrase. Treat every prior bot-generated reply as an `@axrbarsic` reply.

If any check is uncertain, do not publish until resolved. Never count a skipped duplicate as a completed reply.

## Fact checking and response quality

Perform a live internet check before drafting every factual X reply, even when the claim seems familiar. Verify unstable or contested claims against current evidence. Prefer primary and authoritative sources, including official documents, courts, international organizations, election monitors, and original statistics.

Keep this research local. Never forward sources, fact checks, summaries, or conclusions to «Пояснительная бригада». Its input is governed only by the screenshot-only contract below.

Keep replies focused on claims, evidence, logic, and contradictions. Do not:

- insult or degrade the author;
- target protected traits;
- threaten, dogpile, or repeatedly pursue one person;
- invent facts, sources, quotations, or current events;
- copy graphic or hateful material unless the minimum context is necessary to rebut it;
- automate engagement spam or personalized political manipulation.

One factual reply per new target is the default.

## Text validation

Before filling the composer, validate the source text:

```bash
python3 <skill-dir>/scripts/validate_reply.py --file /path/to/reply.txt --max 4000
```

For the Pro contract:

```bash
python3 <skill-dir>/scripts/validate_reply.py --file /path/to/reply.txt --max 4000
```

Resolve `<skill-dir>` as the directory containing this `SKILL.md`.

The validator rejects literal U+2014, U+2013, non-breaking spaces, zero-width characters, and unsafe control characters.

After filling the actual X composer, read its DOM value and repeat the length and forbidden-character checks on that value. Source validation alone is not sufficient because paste and rich text handling can change content.

## Пояснительная бригада

Use the custom GPT only for `pro` targets.

- For every new X target, start a new ChatGPT conversation.
- Before sending content, verify the visible model label is exactly `ChatGPT 5.6 Pro`.
- Send exactly one tightly cropped screenshot containing only the target X post. Include the author, post text, attached media, and quoted post only when they belong to the target post.
- Keep the ChatGPT composer completely empty. The submitted turn must contain zero text code points.
- Never add instructions, captions, greetings, source links, fact checks, length requirements, punctuation, or any other text. The custom GPT already contains its own prompt.
- Let Pro think as long as needed, including more than ten minutes.
- Never click `Ответить сейчас` and never interrupt reasoning.
- Bind the output to the exact submitted screenshot turn. Never reuse a global last assistant answer or `last .markdown.prose`; an older completed answer does not satisfy a newer pending screenshot.
- Treat the bot output as immutable. Do not edit, shorten, expand, reorder, correct, or append anything.
- Accept any non-empty output up to and including 4000 Unicode code points. Reject 4001 or more code points, U+2014, and U+2013.
- Never reject, regenerate, pad, or alter an otherwise valid output merely because it is shorter than 4000 code points.
- If a new-target output violates the contract, discard it and start a fresh conversation with the same screenshot-only submission. Do not send a correction request or explain the validation failure.
- If a user replies to an existing bot-generated X answer, return to the exact ChatGPT conversation that produced that answer. Continue there so the bot retains the discussion history.
- For a follow-up, submit exactly one tightly cropped screenshot of the new reply in that historical conversation, with zero text code points.
- If a follow-up output is invalid, keep the same historical conversation and resubmit only the same screenshot. Do not add correction instructions.
- After a validated Pro reply is published and its X reply URL plus ChatGPT conversation URL are durably recorded, archive that ChatGPT conversation to remove sidebar clutter.
- Keep conversations in `thinking` or `ready` state unarchived. Never archive while generation, validation, or publication verification is incomplete.
- When a follow-up arrives, open the recorded historical conversation URL. If ChatGPT requires it, unarchive that exact conversation, verify that the prior exchange is present, and continue there. Never start a new conversation for a follow-up.
- After the follow-up is validated and its X publication is verified, archive the same conversation again and update the ledger.
- Never delete a «Пояснительная бригада» conversation. Archived history is required for later follow-ups.
- If a clean target-only screenshot cannot be attached with an empty composer, mark the Pro target `blocked`. Never fall back to pasted text.

Read [references/pro-explainer-contract.md](references/pro-explainer-contract.md) before using the custom GPT.

## Waiting and monitoring

Maintain an explicit Pro queue:

- target X URL;
- ChatGPT conversation URL;
- submission time;
- current state: `thinking`, `ready`, `invalid`, `published`, or `blocked`;
- measured length, maximum-length result, and forbidden-character result.

While Pro is thinking, the Browser owner may work on short X targets in the primary X tab. Parallel research sessions may gather sources, but must not operate authenticated tabs.

Poll the Pro queue from the Browser owner session while it continues independent short work. Do not create standalone scheduled tasks for Pro polling because each standalone run creates another Codex task. If an in-chat schedule is explicitly required, attach it to the existing Browser owner task, stop it when the queue is empty, and archive completed technical runs.

## Token-free mention monitoring

For recurring checks of new replies, prefer the deterministic local watcher over
scheduled Codex tasks. The watcher polls the official X API without invoking a
model, stores a durable cursor, queues only direct replies to `@axrbarsic`, and
uses an independent watchdog.

- At cycle start, use the lookback Alex explicitly requests, for example
  run `initial-audit-start`, then
  `initial-audit-expire --hours 3 --as-of <UTC> --dry-run`, followed by the
  apply command with the same hours and identical `--as-of`. Older unresolved
  direct replies receive exact stored history plus an age-policy skip without
  Browser or model use.
- Fix the cutoff once. Do not rerun expiry during the active cycle. Continue
  every newly arriving direct reply and keep an active Pro target alive until
  resolved, even after its original timestamp passes the initial cutoff.
- Never put X credentials in config, logs, Git, task prompts, or process
  arguments. Use the native Keychain helper.
- Do not install or load LaunchAgents until replay tests, live deduplication,
  controlled failure, recovery, and Browser comparison are all green.
- The watcher may detect and queue work, but it must never draft or publish.
  Sol High and the Browser owner retain classification and publication.
- A queued Pro follow-up must return to the exact recorded historical
  «Пояснительная бригада» conversation.

Read [references/mention-watcher.md](references/mention-watcher.md) before
installing, diagnosing, or operating the local watcher.

## Publication transaction

Treat each reply as a transaction:

1. Reopen or verify the exact target.
2. Repeat duplicate checks.
3. Validate the source text.
4. Fill the composer.
5. Read and validate the actual composer value.
6. Confirm the target and account once more.
7. Click the exact reply control once.
8. Verify that the composer cleared and the new reply appears in the thread or account search.
9. Record the reply URL and final state in the ledger.

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

## Reporting

During long work, report verified progress after meaningful batches and at least once per minute while actively working. Include:

- candidates checked;
- duplicates skipped;
- short replies published;
- Pro replies published;
- Pro replies still thinking;
- blockers.

At completion, distinguish `published`, `skipped`, `unverified`, and `blocked`. Never inflate the completed count.
