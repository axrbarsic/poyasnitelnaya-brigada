#!/usr/bin/env python3
"""Split one validated X payload into an ordered reply thread."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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

SENTENCE_BOUNDARY = re.compile(r'[.!?…][»”"\')\]]*\s+')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path)
    source.add_argument("--text")
    parser.add_argument("--max", dest="maximum", type=int, default=4000)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--strip-one-final-newline", action="store_true")
    return parser.parse_args()


def read_text(args: argparse.Namespace) -> str:
    if args.file is not None:
        value = args.file.read_text(encoding="utf-8")
    else:
        value = str(args.text)
    if args.strip_one_final_newline:
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
    return value


def validate_source(value: str, maximum: int) -> None:
    if maximum < 1:
        raise ValueError("maximum must be at least 1")
    if not value:
        raise ValueError("payload must not be empty")
    for index, char in enumerate(value):
        if char in FORBIDDEN:
            raise ValueError(
                f"forbidden character {FORBIDDEN[char]} at code point {index}"
            )
        if ord(char) < 32 and char not in "\n\t":
            raise ValueError(f"unsafe control character at code point {index}")


def preferred_cut(window: str) -> int:
    paragraph = window.rfind("\n\n")
    if paragraph > 0:
        return paragraph

    line = window.rfind("\n")
    if line > 0:
        return line

    sentence = 0
    for match in SENTENCE_BOUNDARY.finditer(window):
        if match.start() > 0:
            sentence = match.end() - len(match.group(0)) + len(
                match.group(0).rstrip()
            )
    if sentence > 0:
        return sentence

    for index in range(len(window) - 1, 0, -1):
        if window[index].isspace():
            return index
    return len(window)


def split_payload(value: str, maximum: int = 4000) -> tuple[list[str], int]:
    validate_source(value, maximum)
    if len(value) <= maximum:
        return [value], 0

    parts: list[str] = []
    boundary_whitespace_removed = 0
    remaining = value
    while len(remaining) > maximum:
        cut = preferred_cut(remaining[:maximum])
        part = remaining[:cut].rstrip()
        if not part:
            cut = maximum
            part = remaining[:cut]
        cursor = cut
        while cursor < len(remaining) and remaining[cursor].isspace():
            cursor += 1
        boundary_whitespace_removed += cursor - cut
        parts.append(part)
        remaining = remaining[cursor:]

    if remaining:
        parts.append(remaining)

    if any(not part or len(part) > maximum for part in parts):
        raise AssertionError("splitter produced an invalid part")
    source_non_whitespace = "".join(char for char in value if not char.isspace())
    parts_non_whitespace = "".join(
        char for part in parts for char in part if not char.isspace()
    )
    if source_non_whitespace != parts_non_whitespace:
        raise AssertionError("splitter changed non-whitespace content")
    return parts, boundary_whitespace_removed


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_manifest(
    value: str,
    parts: list[str],
    *,
    maximum: int,
    boundary_whitespace_removed: int,
    output_dir: Path | None,
) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for index, part in enumerate(parts, start=1):
        path = None
        if output_dir is not None:
            path = output_dir / f"part-{index:02d}.txt"
            path.write_text(part, encoding="utf-8")
        records.append(
            {
                "index": index,
                "code_points": len(part),
                "sha256": sha256(part),
                "path": str(path) if path is not None else None,
            }
        )
    return {
        "valid": True,
        "source_code_points": len(value),
        "source_sha256": sha256(value),
        "maximum": maximum,
        "part_count": len(parts),
        "boundary_whitespace_removed": boundary_whitespace_removed,
        "parts": records,
    }


def main() -> int:
    args = parse_args()
    try:
        value = read_text(args)
        parts, removed = split_payload(value, args.maximum)
        if args.output_dir is not None:
            args.output_dir.mkdir(parents=True, exist_ok=True)
        result = build_manifest(
            value,
            parts,
            maximum=args.maximum,
            boundary_whitespace_removed=removed,
            output_dir=args.output_dir,
        )
    except (OSError, ValueError, AssertionError) as error:
        print(
            json.dumps(
                {"valid": False, "error": str(error)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
