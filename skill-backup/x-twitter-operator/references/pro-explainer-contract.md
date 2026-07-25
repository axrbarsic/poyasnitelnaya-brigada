# Пояснительная бригада contract

## Conversation routing

Use one dedicated ChatGPT conversation per new X target.

For a new target:

1. Start a new conversation.
2. Verify the visible model label is exactly `ChatGPT 5.6 Pro`.
3. Capture one tightly cropped screenshot containing only the target X post.
4. Attach that screenshot with an empty ChatGPT composer.
5. Verify the composer contains exactly zero text code points.
6. Submit the attachment without any accompanying text.
7. Record the ChatGPT conversation URL beside the X target URL.

For a follow-up to a bot-generated X reply:

1. Look up the recorded ChatGPT conversation URL.
2. Open that exact conversation, including when it is archived.
3. Unarchive it only when ChatGPT requires that step before continuation.
4. Verify the prior target screenshot and bot answer are present.
5. Capture one tightly cropped screenshot containing only the new X reply.
6. Attach the screenshot with an empty ChatGPT composer.
7. Verify the composer contains exactly zero text code points.
8. Submit the attachment without any accompanying text.
9. Keep all later replies in this discussion in the same conversation.

Never choose a conversation by title alone when URLs or exact history can disambiguate it.

## Conversation lifecycle

Use these states:

`thinking -> ready -> published -> archived`

Archive a conversation only after:

- the complete payload passes validation;
- the X reply is visibly verified as published;
- the X target URL, published reply URL, and exact ChatGPT conversation URL are stored in the ledger;
- no generation, retry, or publication verification remains active.

Do not archive a conversation in `thinking`, `ready`, `invalid`, `unverified`, or `blocked` state.

When a follow-up arrives:

1. Resolve the published X reply to its recorded ChatGPT conversation URL.
2. Open that exact archived conversation or unarchive it when required.
3. Verify the visible history matches the original target and published payload.
4. Submit only the new follow-up screenshot under the zero-text contract.
5. Validate and publish the immutable result.
6. Record the new X reply ID and publication URL.
7. Archive the same conversation again.

Never create a new ChatGPT conversation for a follow-up merely because the historical conversation is archived or inconvenient to reach. If the exact conversation cannot be recovered, mark the follow-up `blocked`.

Never delete these conversations. Archiving is reversible and preserves the history required for follow-up routing; deletion is permanent.

## Screenshot-only input

The input contract is strict and has no exceptions.

- Submit exactly one screenshot of the target post or follow-up.
- Crop out unrelated replies, recommendations, navigation, notifications, and other page content.
- Preserve target media and a quoted post only when they are part of the target post.
- Send zero text code points in the ChatGPT composer.
- Do not add a caption, prompt, instruction, greeting, punctuation mark, source link, verified fact, length requirement, filename explanation, or correction note.
- Do not paste OCR text alongside the screenshot.
- Do not send a second message that explains the screenshot.

Before clicking send, record:

- attachment count: `1`;
- composer text code points: `0`;
- target X URL;
- ChatGPT conversation URL;
- screenshot scope verified: `target-only`.

If any value differs, do not send.

## Waiting

Allow Pro to think for more than ten minutes when needed. Never:

- click `Ответить сейчас`;
- stop generation to save time;
- send a second prompt while the current answer is still running;
- treat partial streaming text as complete.

Track every active conversation in the Pro queue. Poll without disturbing the page.

## Immutable output

The returned answer is an immutable payload.

Do not:

- alter spelling or punctuation;
- replace dash characters manually;
- add a greeting, source note, or conclusion;
- remove citations;
- join or split paragraphs;
- shorten the answer;
- copy only the visible portion when text is collapsed.

Validate the complete copied payload with:

```bash
python3 <skill-dir>/scripts/validate_reply.py --file /path/to/bot-output.txt --max 4000
```

Also validate the actual X composer value after filling.

Any non-empty payload from 1 through 4000 Unicode code points is valid when all other checks pass. Never reject, regenerate, pad, shorten, or edit a payload solely because it is below 4000 code points. A payload becomes invalid for length only at 4001 or more code points.

## Invalid output

For a new X target, discard an invalid output and start a fresh ChatGPT conversation. Submit only the same target screenshot again with an empty composer.

For a follow-up discussion, stay in the same historical conversation and resubmit only the same follow-up screenshot with an empty composer.

Never send:

- the invalid output;
- its measured length;
- the maximum allowed length;
- detected forbidden characters;
- a correction instruction;
- a requested deletion or replacement;
- any other explanatory text.

Do not publish until the output passes every check.

## Publication identity

Once published, the bot output counts as an `@axrbarsic` reply for duplicate prevention and follow-up routing. Record:

- target X URL;
- published reply URL;
- ChatGPT conversation URL;
- validation result;
- publication time.
