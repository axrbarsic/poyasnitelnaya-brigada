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

Use live or recent search filters matching the requested time window. Gather more candidates than the requested reply count because context inspection will eliminate satire, duplicates, weak claims, and unverifiable material.

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

Skip a target if its meaning remains ambiguous.

## 3. Duplicate checks

Perform all of these when available:

1. Search visible thread articles for `@axrbarsic`.
2. Inspect direct replies below the target.
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

### Pro reply

Inspect the complete context locally, then follow the custom GPT screenshot-only contract. Send exactly one target-only screenshot and zero text code points. Never forward local research, sources, instructions, length requirements, or corrections. Do not manually merge bot text with a local introduction or conclusion.

## 5. Composer validation

Before filling:

- validate the source text with `scripts/validate_reply.py`;
- confirm no literal U+2014 or U+2013;
- confirm the text is no longer than 4000 Unicode code points;
- confirm the target post is still open.

After filling:

- read the actual textbox value from the DOM;
- count its Unicode code points;
- scan again for forbidden characters;
- confirm line breaks are preserved when relevant;
- confirm no automatic prefix or quote was inserted.

If the actual value differs from the validated source, clear the composer and investigate. Do not publish a best-effort approximation.

## 6. Publication verification

After clicking:

1. Wait for the page state to settle.
2. Confirm the composer cleared.
3. Locate a new thread article from `@axrbarsic`.
4. Verify a distinctive prefix or the full text when practical.
5. Capture the reply URL.

If the click times out, inspect state before clicking again. A timeout can occur after a successful submission. Blindly retrying risks duplicates.

## 7. Batch accounting

Maintain separate counts:

- candidates inspected;
- context skips;
- duplicates;
- short replies published;
- Pro replies published;
- unverified submissions;
- blocked targets.

Only verified reply URLs count as published.
