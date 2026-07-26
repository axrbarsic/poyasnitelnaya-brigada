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
6. The scheduled Sol automation claimed the event exactly once.
7. Browser preflight used `@axrbarsic` and opened the exact live thread.
8. The response route, duplicate checks and fact check completed.
9. X contains one verified direct Alex child.
10. Exact user and Alex turns exist in `conversation_turns`.
11. `event_resolutions` contains the verified reply URL.
12. The event is absent from the wake queue and no lease remains active.

Classify the first failing layer as the weak link. Do not infer that the API
missed an event merely because the final queue is empty.

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
7. Let the ordinary one-minute watcher and five-minute Sol automation discover,
   claim and process the event.
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
