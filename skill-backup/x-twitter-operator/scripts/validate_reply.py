#!/usr/bin/env python3
"""Validate exact text contracts for X/Twitter replies."""

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
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--file", type=Path)
    source.add_argument("--text")
    parser.add_argument("--exact", type=int)
    parser.add_argument("--min", dest="minimum", type=int)
    parser.add_argument("--max", dest="maximum", type=int)
    parser.add_argument("--non-empty", action="store_true")
    parser.add_argument("--single-paragraph", action="store_true")
    parser.add_argument("--strip-one-final-newline", action="store_true")
    return parser.parse_args()


def read_text(args: argparse.Namespace) -> str:
    if args.file is not None:
        value = args.file.read_text(encoding="utf-8")
    elif args.text is not None:
        value = args.text
    else:
        value = sys.stdin.read()
    if args.strip_one_final_newline:
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
    return value


def utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def main() -> int:
    args = parse_args()
    value = read_text(args)
    errors: list[str] = []
    forbidden_hits: list[dict[str, object]] = []

    for index, char in enumerate(value):
        if char in FORBIDDEN:
            forbidden_hits.append(
                {"index": index, "codepoint": f"U+{ord(char):04X}", "name": FORBIDDEN[char]}
            )
        elif ord(char) < 32 and char not in "\n\t":
            forbidden_hits.append(
                {
                    "index": index,
                    "codepoint": f"U+{ord(char):04X}",
                    "name": "CONTROL CHARACTER",
                }
            )

    length = len(value)
    if args.non_empty and length == 0:
        errors.append("expected a non-empty reply")
    if args.exact is not None and length != args.exact:
        errors.append(f"expected exactly {args.exact} code points, got {length}")
    if args.minimum is not None and length < args.minimum:
        errors.append(f"expected at least {args.minimum} code points, got {length}")
    if args.maximum is not None and length > args.maximum:
        errors.append(f"expected at most {args.maximum} code points, got {length}")
    if args.single_paragraph and ("\n" in value or "\r" in value):
        errors.append("expected a single paragraph without line breaks")
    if forbidden_hits:
        errors.append("forbidden or unsafe characters found")

    result = {
        "valid": not errors,
        "code_points": length,
        "utf16_units": utf16_units(value),
        "utf8_bytes": len(value.encode("utf-8")),
        "line_count": 0 if value == "" else value.count("\n") + 1,
        "forbidden": forbidden_hits,
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
