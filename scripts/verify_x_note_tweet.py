#!/usr/bin/env python3
"""Verify an exact published X long reply through the official note_tweet API."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import xmention_watcher  # noqa: E402


FORBIDDEN = ("\u2013", "\u2014", "\u00a0", "\u200b", "\u200c", "\u200d", "\ufeff")


def utf16_offset_to_python_index(text: str, offset: int) -> int:
    if offset < 0:
        raise ValueError("UTF-16 offset must be non-negative")
    units = 0
    for index, character in enumerate(text):
        if units == offset:
            return index
        units += 2 if ord(character) > 0xFFFF else 1
        if units > offset:
            raise ValueError("UTF-16 offset splits a surrogate pair")
    if units == offset:
        return len(text)
    raise ValueError("UTF-16 offset exceeds text length")


def resolve_entity_range(
    text: str,
    *,
    start: int,
    end: int,
    expected_url: str,
) -> tuple[int, int, str]:
    if start < 0 or end < start:
        raise ValueError("URL entity has an invalid range")
    candidates: dict[tuple[int, int], str] = {}
    if end <= len(text) and text[start:end] == expected_url:
        candidates[(start, end)] = "codepoint"
    try:
        utf16_start = utf16_offset_to_python_index(text, start)
        utf16_end = utf16_offset_to_python_index(text, end)
    except ValueError:
        pass
    else:
        if text[utf16_start:utf16_end] == expected_url:
            candidates.setdefault((utf16_start, utf16_end), "utf16")
    if not candidates:
        raise ValueError(
            f"URL entity range does not match its t.co value at {start}:{end}"
        )
    if len(candidates) > 1:
        raise ValueError("URL entity range is ambiguous")
    (python_start, python_end), mode = next(iter(candidates.items()))
    return python_start, python_end, mode


def reconstruct_note_tweet(note_tweet: dict[str, Any]) -> tuple[str, list[str]]:
    text = note_tweet.get("text")
    if not isinstance(text, str):
        raise ValueError("note_tweet.text must be a string")
    entities = note_tweet.get("entities") or {}
    if not isinstance(entities, dict):
        raise ValueError("note_tweet.entities must be an object")
    urls = entities.get("urls") or []
    if not isinstance(urls, list):
        raise ValueError("note_tweet.entities.urls must be an array")

    modes: list[str] = []
    seen_ranges: set[tuple[int, int]] = set()
    sorted_urls = sorted(
        urls,
        key=lambda item: int(item.get("start", -1))
        if isinstance(item, dict)
        else -1,
        reverse=True,
    )
    for item in sorted_urls:
        if not isinstance(item, dict):
            raise ValueError("URL entity must be an object")
        start = item.get("start")
        end = item.get("end")
        short_url = item.get("url")
        expanded_url = item.get("expanded_url")
        if not isinstance(start, int) or not isinstance(end, int):
            raise ValueError("URL entity offsets must be integers")
        if not isinstance(short_url, str) or not short_url:
            raise ValueError("URL entity url must be a non-empty string")
        if not isinstance(expanded_url, str) or not expanded_url:
            raise ValueError("URL entity expanded_url must be a non-empty string")
        original_range = (start, end)
        if original_range in seen_ranges:
            raise ValueError("URL entities contain a duplicate range")
        seen_ranges.add(original_range)
        python_start, python_end, mode = resolve_entity_range(
            text,
            start=start,
            end=end,
            expected_url=short_url,
        )
        text = text[:python_start] + expanded_url + text[python_end:]
        modes.append(mode)
    return text, list(reversed(modes))


def load_live_tweet(
    *,
    config_path: Path,
    status_id: str,
) -> dict[str, Any]:
    if not status_id.isdigit():
        raise ValueError("status ID must be numeric")
    config = xmention_watcher.load_config(config_path)
    params = {
        "tweet.fields": (
            "author_id,created_at,conversation_id,in_reply_to_user_id,"
            "referenced_tweets,entities,note_tweet"
        )
    }
    url = (
        f"{config.api_base}/tweets/{urllib.parse.quote(status_id)}?"
        f"{urllib.parse.urlencode(params)}"
    )
    response = xmention_watcher.request_json(
        url,
        xmention_watcher.bearer_token(config),
        config.request_timeout_seconds,
    )
    if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
        raise ValueError("X API response does not contain a tweet data object")
    return response


def build_report(
    response: dict[str, Any],
    *,
    source_text: str,
    maximum_length: int,
    expected_status_id: str | None,
    expected_parent_status_id: str | None,
) -> dict[str, Any]:
    data = response.get("data")
    if not isinstance(data, dict):
        raise ValueError("response.data must be an object")
    status_id = data.get("id")
    if not isinstance(status_id, str) or not status_id.isdigit():
        raise ValueError("response.data.id must be numeric text")
    if expected_status_id is not None and status_id != expected_status_id:
        raise ValueError("X API returned a different status ID")
    note_tweet = data.get("note_tweet")
    if not isinstance(note_tweet, dict):
        raise ValueError("published status does not contain note_tweet")
    reconstructed, entity_index_modes = reconstruct_note_tweet(note_tweet)
    referenced = data.get("referenced_tweets") or []
    if not isinstance(referenced, list):
        raise ValueError("referenced_tweets must be an array")
    parent_status_ids = [
        str(item.get("id"))
        for item in referenced
        if isinstance(item, dict) and item.get("type") == "replied_to"
    ]
    parent_matches = (
        expected_parent_status_id is None
        or parent_status_ids == [expected_parent_status_id]
    )
    forbidden = [character for character in FORBIDDEN if character in reconstructed]
    report = {
        "status_id": status_id,
        "created_at": data.get("created_at"),
        "conversation_id": data.get("conversation_id"),
        "parent_status_ids": parent_status_ids,
        "parent_matches": parent_matches,
        "code_points": len(reconstructed),
        "utf8_bytes": len(reconstructed.encode("utf-8")),
        "maximum_length": maximum_length,
        "non_empty": bool(reconstructed),
        "length_within_limit": len(reconstructed) <= maximum_length,
        "exact_file_match": reconstructed == source_text,
        "sha256": hashlib.sha256(reconstructed.encode("utf-8")).hexdigest(),
        "forbidden": forbidden,
        "entity_index_modes": entity_index_modes,
    }
    report["valid"] = (
        report["parent_matches"]
        and report["non_empty"]
        and report["length_within_limit"]
        and report["exact_file_match"]
        and not forbidden
    )
    return report


def read_source(path: Path, *, strip_one_final_newline: bool) -> str:
    text = path.read_text(encoding="utf-8")
    if strip_one_final_newline and text.endswith("\n"):
        return text[:-1]
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--response-json", type=Path)
    source.add_argument("--config", type=Path)
    parser.add_argument("--status-id")
    parser.add_argument("--parent-status-id")
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--max", dest="maximum", type=int, default=4000)
    parser.add_argument("--strip-one-final-newline", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.maximum <= 0:
        raise ValueError("--max must be positive")
    if args.config is not None:
        if not args.status_id:
            raise ValueError("--status-id is required with --config")
        response = load_live_tweet(
            config_path=args.config,
            status_id=args.status_id,
        )
    else:
        if args.status_id:
            raise ValueError("--status-id is only valid with --config")
        response = json.loads(args.response_json.read_text(encoding="utf-8"))
    source_text = read_source(
        args.file,
        strip_one_final_newline=args.strip_one_final_newline,
    )
    report = build_report(
        response,
        source_text=source_text,
        maximum_length=args.maximum,
        expected_status_id=args.status_id,
        expected_parent_status_id=args.parent_status_id,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(
            json.dumps(
                {"valid": False, "error": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from error
