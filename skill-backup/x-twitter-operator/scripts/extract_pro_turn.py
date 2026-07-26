#!/usr/bin/env python3
"""Extract and validate one assistant payload bound to an exact ChatGPT turn."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


FORBIDDEN = {
    "\u2014": "U+2014 EM DASH",
    "\u2013": "U+2013 EN DASH",
    "\u00a0": "U+00A0 NO-BREAK SPACE",
    "\u200b": "U+200B ZERO WIDTH SPACE",
    "\u200c": "U+200C ZERO WIDTH NON-JOINER",
    "\u200d": "U+200D ZERO WIDTH JOINER",
    "\ue200": "U+E200 CHATGPT INTERNAL CITATION MARKER",
    "\ue201": "U+E201 CHATGPT INTERNAL CITATION MARKER",
    "\ue202": "U+E202 CHATGPT INTERNAL CITATION MARKER",
    "\ufeff": "U+FEFF ZERO WIDTH NO-BREAK SPACE",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path)
    parser.add_argument("--expected-turn-id", required=True)
    parser.add_argument("--expected-assistant-message-id")
    parser.add_argument("--event-id")
    parser.add_argument("--payload-output", type=Path)
    return parser.parse_args()


def load_input(path: Path | None) -> dict[str, object]:
    raw = path.read_text(encoding="utf-8") if path else sys.stdin.read()
    value = json.loads(raw)
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def forbidden_hits(value: str) -> list[dict[str, object]]:
    hits: list[dict[str, object]] = []
    for index, char in enumerate(value):
        if char in FORBIDDEN:
            hits.append(
                {"index": index, "codepoint": f"U+{ord(char):04X}", "name": FORBIDDEN[char]}
            )
        elif ord(char) < 32 and char not in "\n\t":
            hits.append(
                {
                    "index": index,
                    "codepoint": f"U+{ord(char):04X}",
                    "name": "CONTROL CHARACTER",
                }
            )
    return hits


def main() -> int:
    args = parse_args()
    data = load_input(args.file)
    turns = data.get("turns")
    if not isinstance(turns, list):
        raise ValueError("missing turns array")

    matches = [
        turn
        for turn in turns
        if isinstance(turn, dict) and turn.get("id") == args.expected_turn_id
    ]
    if len(matches) != 1:
        result = {
            "valid": False,
            "state": "turn_binding_failed",
            "expected_turn_id": args.expected_turn_id,
            "matching_turns": len(matches),
            "errors": ["expected exactly one matching turn"],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    turn = matches[0]
    items = turn.get("items")
    if not isinstance(items, list):
        items = []
    messages = [
        item
        for item in items
        if isinstance(item, dict) and item.get("type") == "agentMessage"
    ]
    if not messages:
        result = {
            "valid": False,
            "state": "unverified_background_pending",
            "expected_turn_id": args.expected_turn_id,
            "turn_status": turn.get("status"),
            "assistant_message_count": 0,
            "errors": ["matching turn has no assistant message"],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    if len(messages) != 1:
        result = {
            "valid": False,
            "state": "turn_binding_failed",
            "expected_turn_id": args.expected_turn_id,
            "assistant_message_count": len(messages),
            "errors": ["expected exactly one assistant message in matching turn"],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    message = messages[0]
    message_id = message.get("id")
    payload = message.get("text")
    errors: list[str] = []
    if args.expected_assistant_message_id and message_id != args.expected_assistant_message_id:
        errors.append("assistant message ID does not match")
    if message.get("truncated") is True:
        errors.append("assistant message is truncated")
    if turn.get("status") != "completed":
        errors.append("matching turn is not completed")
    if not isinstance(payload, str) or payload == "":
        errors.append("assistant payload is empty")
        payload = "" if not isinstance(payload, str) else payload

    hits = forbidden_hits(payload)
    if hits:
        errors.append("forbidden or unsafe characters found")
    if len(payload) > 4000:
        errors.append(f"expected at most 4000 code points, got {len(payload)}")

    result = {
        "valid": not errors,
        "state": "ready" if not errors else "invalid",
        "event_id": args.event_id,
        "thread_id": (data.get("thread") or {}).get("id")
        if isinstance(data.get("thread"), dict)
        else None,
        "turn_id": turn.get("id"),
        "turn_status": turn.get("status"),
        "assistant_message_id": message_id,
        "code_points": len(payload),
        "utf8_bytes": len(payload.encode("utf-8")),
        "forbidden": hits,
        "errors": errors,
    }

    if args.payload_output is not None and not errors:
        artifact = {
            "schema_version": 1,
            "event_id": args.event_id,
            "conversation_id": result["thread_id"],
            "turn_id": result["turn_id"],
            "assistant_message_id": message_id,
            "code_points": len(payload),
            "payload": payload,
        }
        args.payload_output.parent.mkdir(parents=True, exist_ok=True)
        args.payload_output.write_text(
            json.dumps(artifact, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        result["payload_output"] = str(args.payload_output)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
