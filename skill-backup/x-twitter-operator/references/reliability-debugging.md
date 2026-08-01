# Reliability debugging and clean experiments

Use this workflow when Alex reports a missed X reply, an empty queue that
conflicts with live X, an apparent duplicate, or an automation that claims
success without the expected publication.

## Evidence boundary

- Treat the screenshot as a lead, not proof of failure.
- Inspect every visible candidate in the screenshot. A notifications screen
  can show more than one event.
- Determine the exact status ID, canonical URL, parent, conversation ID, author
  and live direct Alex children.
- Do not publish while the diagnosis is unresolved.
- Preserve append-only database and ledger evidence. Never rewrite history to
  make the result look clean.

## Trace the complete pipeline

Check each layer in order:

1. Live X contains the event.
2. The official mentions poll observed the event and advanced its cursor.
3. `events` contains the exact payload and correct `delivery_state`.
4. Eligibility classified it as queued or ignored for an explicit reason.
5. `wake-request.json` exposed every queued event.
6. The Luna gate returned `dispatch=true`.
7. Luna called `send_message_to_thread` directly and the pinned Sol owner
   received the follow-up.
8. The pinned owner claimed the event exactly once.
9. The claim contains at most three oldest pending events and does not hide a
   short reply behind preparation of the whole batch.
10. Browser preflight used `@axrbarsic` and opened the exact live thread.
11. The response route, duplicate checks and fact check completed.
12. X contains one verified direct Alex child.
13. Exact user and Alex turns exist in `conversation_turns`.
14. `event_resolutions` contains the verified reply URL.
15. The event is absent from the wake queue and no lease remains active.

Classify the first failing layer as the weak link. Do not infer that the API
missed an event merely because the final queue is empty.

An event is operationally missed when it is detected but exceeds the expected
response time without a verified direct reply. Measure both
`first_seen_at -> claimed_at` and `claimed_at -> verified publication`. A live
lease proves ownership, not completion. For a convoy failure, keep one writer,
bound a claim to three oldest events, allow up to three independent read-only X
tabs, and commit each event end-to-end before preparing the next publication.

If the gate is ready but the owner never wakes, inspect the Luna trace. A
`send_message_to_thread` call made inside `functions.exec`, JavaScript,
`tools.*`, or another nested wrapper can remain pending. Fix the universal
dispatcher prompt: expose the tool through `tool_search`, call the direct Codex
app tool, keep the queue unclaimed on failure, and let the next cycle retry.

## Proven failure signature

One verified incident had this exact shape:

- the authenticated mentions endpoint returned the reply;
- `since_id` advanced past it;
- SQLite stored it as `ignored`;
- the immediate parent belonged to another participant;
- live X still showed an older Alex turn in the conversation;
- that Alex turn was absent from local `conversation_turns`.

The weak link was eligibility, not API polling. The universal repair queues
every reply returned by the authenticated mentions endpoint in mandatory mode.
The Browser owner then restores the live chain and proves exactly one durable
route before synchronization.

## Mandatory mention rule

With `mandatory_response_mode=true`, every reply returned by the authenticated
mentions endpoint is eligible for live inspection. This includes a reply to
another participant when X also mentions `@axrbarsic`.

Do not require an older local conversation to contain an Alex turn before
queueing that event. Historical backfill can be incomplete even when live X
shows that Alex participated in the thread.

## Clean experiment

After fixing a weak link:

1. Add a regression test using synthetic IDs, authors and conversation data.
   Never encode the reported status ID, author or topic in production logic.
2. Run targeted tests, then the normal project gate.
3. Deploy the universal change before touching live queue state.
4. Choose one timezone-aware `as-of` value and a bounded lookback.
5. Run `mandatory-response-requeue` first with `--dry-run`, then apply with
   the same hours and exact `as-of`.
6. Do not pass the reported event ID to the requeue command, automation prompt
   or Browser owner.
7. Let the ordinary one-minute watcher and in-app heartbeat relay discover the
   event and wake the pinned Sol owner through the direct Codex app tool.
8. Observe without manually claiming, drafting or publishing.
9. Verify the live direct reply, exact history, durable resolution, empty queue
   and released lease.
10. If another layer fails, append the evidence, fix that universal layer and
    repeat the same experiment.

The experiment is invalid if the operator manually inserts the known event ID,
publishes the answer directly, changes a topic allowlist, or marks the event
resolved without a verified live reply.

## Reporting

Report:

- exact event and which visible screenshot candidate it represented;
- first failing layer;
- universal fix and regression coverage;
- target-agnostic replay window and fixed `as-of`;
- automation claim and verified reply URL;
- final queue, lease and database state;
- any remaining blocker.
