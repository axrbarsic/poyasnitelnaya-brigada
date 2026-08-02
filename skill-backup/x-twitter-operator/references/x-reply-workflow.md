# X reply workflow

## Contents

1. Candidate discovery
2. Context inspection
3. Duplicate checks
4. Drafting
5. Composer validation
6. Publication verification
7. Batch accounting

## 1. Candidate discovery

Use live or recent search filters matching the requested time window. Every
eligible available event inside that window requires one reply unless an exact
direct Alex child reply already exists.

Record the canonical status ID and URL immediately. Deduplicate by status ID before opening threads.

Do not infer position from keywords alone. A search result can be a rebuttal, quotation, satire, or hostile reply to the propaganda being searched.

## 2. Context inspection

Open the canonical post and inspect:

- complete text behind any expansion control;
- author and account labels;
- whether it is a reply;
- quoted post;
- media caption and visible provenance;
- surrounding thread;
- time and date;
- engagement context;
- sarcasm or parody indicators.

If meaning remains ambiguous, ask a neutral contextual question instead of
dropping the event.

For a reply containing an image, video, GIF, or meme:

1. Verify the exact parent status and author.
2. Read visible text, alt text, OCR, and the visual action or reaction.
3. Compare that meaning with the exact parent claim.
4. Inspect the same author's nearby replies in the thread to determine which
   side the author supports.
5. Research an unfamiliar meme or reference on the internet when its normal
   meaning is material to classification.
6. Record `media_meaning`, `stance`, `confidence`, and `evidence`.

Use only `supportive`, `opposing`, `neutral`, or `ambiguous` for stance. A
media-only reply is not a skip merely because `tweetText` is empty. If stance
remains ambiguous after contextual and internet checks, publish a neutral
clarifying reply.

### Cross-thread author continuity

Every claimed event may include `commenter_memory`, a compact source-linked
history keyed by the author's stable X user ID. Before drafting:

1. Check exact prior public turns, dates, URLs, and exact Alex replies.
2. Look for a demonstrable contradiction, changed criterion, double standard,
   or repetition of a claim already answered.
3. If the compact sample is insufficient, run
   `commenter-history EVENT_ID --limit N`.
4. Use an old statement only when it materially improves the current answer.
   Quote or paraphrase it accurately and preserve its URL in evidence.
5. Never infer sensitive traits, hidden motives, private facts, or a personal
   profile. Never use history for stalking, dogpiling, or personalized political
   manipulation.

## 3. Duplicate checks

Perform all of these when available:

1. Search visible thread articles for `@axrbarsic`.
2. Inspect the complete reply subtree below the target.
3. Check the in-run ledger by status ID.
4. Search `from:axrbarsic to:<target_handle>` with a distinctive phrase when thread rendering is incomplete.

Repeat checks immediately before clicking the final reply button. Another task or prior bot output may have posted since candidate discovery.

## 4. Drafting

### Short reply

Write a compact response that:

- identifies the precise claim;
- gives one or two decisive verified facts;
- explains the contradiction;
- avoids claims about the author's intelligence, motives, ethnicity, or worth;
- uses a link only when it materially helps verification.

Supportive reactions, jokes, sarcasm, insults, and content-free replies still
receive one contextual short reply. Do not answer an insult with an insult.
Use calm, evidence-backed superiority and address the argument or absence of
one.

### Satirical media reply

Use this experimental route only for a pure insult when a visual response adds
value.

1. For an explicitly selected `377` route, load the local `377` skill and use
   the image generation tool without opening ChatGPT. For an explicitly
   selected `Ложкин` route, open exactly that one custom ChatGPT web bot.
2. Provide only the target and minimum thread context.
3. Generate satire about the rhetorical move or weak argument.
4. Reject output that degrades appearance, dignity, protected traits, private
   life, or invents misconduct.
5. Inspect the final image before attaching it.
6. If the target contains a factual claim, include a Sol High text rebuttal
   with primary-source support. The picture is not evidence.
7. If generation or review fails, publish a Sol High text reply instead.

### Local Sol Max reply

Inspect the complete context locally, load `poyasnitelnaya-brigada-v2` by
default, and generate inside the Sol Max Browser-owner turn. Keep
`poyasnitelnaya-brigada` v1 unchanged and use it only when Alex explicitly asks
for v1. Include exact durable X history and
current primary-source research. Never open ChatGPT or the custom GPT. Produce
one non-empty direct monologue of at most 4000 Unicode code points. Do not target
the maximum or pad the answer. Validate it deterministically before filling the
X composer.

## 5. Composer validation

Before filling a short reply:

- validate and split the source text with `scripts/split_reply_thread.py`;
- confirm no literal U+2014 or U+2013;
- confirm every generated part is from 1 through 4000 Unicode code points;
- confirm the target post is still open.

Before filling a local «Пояснительная бригада» reply:

- validate with:

  ```bash
  python3 <skill-dir>/scripts/validate_reply.py \
    --file /path/to/reply.txt \
    --strip-one-final-newline \
    --non-empty \
    --max 4000
  ```

- confirm `generation_model=gpt-5.6-sol`;
- confirm `reasoning_effort=max`;
- confirm `generation_skill=poyasnitelnaya-brigada-v2` unless Alex explicitly
  requested v1;
- confirm the target post is still open.

After filling:

- read the actual textbox value from the DOM;
- for X DraftJS, reconstruct that value from ordered
  `[data-block="true"]` elements by joining each block's `textContent` with
  one literal `\n`; never use raw `innerText`, which adds presentation-only
  newlines between blocks;
- count its Unicode code points;
- scan again for forbidden characters;
- confirm line breaks are preserved when relevant;
- confirm no automatic prefix or quote was inserted.

If the actual value differs from the validated source, clear the composer and investigate. Do not publish a best-effort approximation.

## 6. Publication verification

After clicking each part:

1. Wait for the page state to settle.
2. Confirm the composer cleared.
3. Locate a new thread article from `@axrbarsic`.
4. Capture the reply URL and verify the exact parent status ID.
5. Fetch the official API representation of every published reply. Prefer
   `note_tweet` when present. For a regular short post without `note_tweet`,
   use `text` and `entities`. Replace each t.co URL span, in reverse offset
   order, with its `expanded_url`. The reconstructed text must be non-empty,
   contain at most 4000 Unicode code points, and match the validated source
   byte-for-byte. In this project, run:

   ```bash
   python3 scripts/verify_x_note_tweet.py \
     --config config.json \
     --status-id REPLY_STATUS_ID \
     --parent-status-id TARGET_STATUS_ID \
     --file /path/to/reply.txt \
     --strip-one-final-newline \
     --max 4000
   ```

   Require `valid=true` and preserve the JSON report in task evidence.
6. Treat rendered `innerText` as visual evidence only. X may add wrapping
   newlines and ellipses to displayed URLs, so it is not an exact-text oracle.
7. Verify a distinctive prefix and suffix in the live article.
8. Build and import the exact two-turn local history from the completed
   evidence object:

   ```bash
   python3 scripts/build_outbound_history.py \
     --evidence /path/to/evidence.json \
     --output /path/to/conversation-history.jsonl \
     --max 4000
   python3 xmention_watcher.py --config config.json history-import \
     --file /path/to/conversation-history.jsonl
   python3 xmention_watcher.py --config config.json history-show TARGET_STATUS_ID
   ```

   Require the exact target turn, exact Alex turn, correct parent, source URLs,
   and legacy `provenance=pro` marker before durable completion.

For a multi-part payload, the first part replies to the exact target. Open the
verified URL of each published part and make the next part its direct child.
Do not add numbering or connective text. The ledger must prove the complete
ordered status-ID chain and exact hash of every part.

If the click times out, inspect state before clicking again. A timeout can occur after a successful submission. Blindly retrying risks duplicates.

## 7. Batch accounting

Maintain separate counts:

- candidates inspected;
- already-answered events with exact Alex child URLs;
- terminal blockers;
- short replies published;
- local Sol Max replies published;
- unverified submissions;
- blocked targets.

Only verified reply URLs count as published.
