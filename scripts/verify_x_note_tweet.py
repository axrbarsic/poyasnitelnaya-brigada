#!/usr/bin/env python3
"""Verify an exact published X reply through the official API."""

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
from scripts import json_contract  # noqa: E402


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
    expected_text: str,
) -> tuple[int, int, str]:
    if start < 0 or end < start:
        raise ValueError("URL entity has an invalid range")
    candidates: dict[tuple[int, int], str] = {}
    if end <= len(text) and text[start:end] == expected_text:
        candidates[(start, end)] = "codepoint"
    try:
        utf16_start = utf16_offset_to_python_index(text, start)
        utf16_end = utf16_offset_to_python_index(text, end)
    except ValueError:
        pass
    else:
        if text[utf16_start:utf16_end] == expected_text:
            candidates.setdefault((utf16_start, utf16_end), "utf16")
    if not candidates:
        raise ValueError(
            f"Entity range does not match its expected text at {start}:{end}"
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
            expected_text=short_url,
        )
        text = text[:python_start] + expanded_url + text[python_end:]
        modes.append(mode)
    return text, list(reversed(modes))


def reconstruct_published_text(data: dict[str, Any]) -> tuple[str, list[str], str]:
    note_tweet = data.get("note_tweet")
    if isinstance(note_tweet, dict):
        text, modes = reconstruct_note_tweet(note_tweet)
        return text, modes, "note_tweet"

    text = data.get("text")
    if not isinstance(text, str):
        raise ValueError("published status contains neither note_tweet nor text")
    text_payload = {
        "text": text,
        "entities": data.get("entities") or {},
    }
    reconstructed, modes = reconstruct_note_tweet(text_payload)
    return reconstructed, modes, "text"


def normalize_hidden_reply_mentions(
    text: str,
    *,
    entities: dict[str, Any],
    source_text: str,
    is_expected_reply: bool,
) -> tuple[str, list[str], str]:
    if text == source_text:
        return text, [], "none"
    if not is_expected_reply or not text.endswith(source_text):
        return text, [], "none"

    prefix_end = len(text) - len(source_text)
    prefix = text[:prefix_end]
    if not prefix or not prefix[-1].isspace():
        return text, [], "none"
    mentions = entities.get("mentions") or []
    if not isinstance(mentions, list):
        raise ValueError("entities.mentions must be an array")

    covered = [False] * prefix_end
    usernames: list[str] = []
    for item in mentions:
        if not isinstance(item, dict):
            raise ValueError("Mention entity must be an object")
        start = item.get("start")
        end = item.get("end")
        username = item.get("username")
        if not isinstance(start, int) or not isinstance(end, int):
            raise ValueError("Mention entity offsets must be integers")
        if not isinstance(username, str) or not username:
            raise ValueError("Mention entity username must be non-empty text")
        expected = "@" + username
        python_start, python_end, _mode = resolve_entity_range(
            text,
            start=start,
            end=end,
            expected_text=expected,
        )
        if python_start >= prefix_end:
            continue
        if python_end > prefix_end:
            return text, [], "none"
        for index in range(python_start, python_end):
            if covered[index]:
                raise ValueError("Mention entities overlap")
            covered[index] = True
        usernames.append(username)

    if not usernames:
        return text, [], "none"
    if any(not covered[index] and not character.isspace() for index, character in enumerate(prefix)):
        return text, [], "none"
    return source_text, usernames, "hidden_reply_mentions"


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
    reconstructed, entity_index_modes, text_source = reconstruct_published_text(data)
    api_code_points = len(reconstructed)
    entities = data.get("entities") or {}
    if not isinstance(entities, dict):
        raise ValueError("response.data.entities must be an object")
    reconstructed, reply_prefix_mentions, text_normalization = (
        normalize_hidden_reply_mentions(
            reconstructed,
            entities=entities,
            source_text=source_text,
            is_expected_reply=(
                expected_parent_status_id is not None and parent_matches
            ),
        )
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
        "text_source": text_source,
        "api_code_points": api_code_points,
        "reply_prefix_mentions": reply_prefix_mentions,
        "text_normalization": text_normalization,
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
        response = json_contract.read(args.response_json)
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
