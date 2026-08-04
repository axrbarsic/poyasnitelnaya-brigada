#!/usr/bin/env python3
"""Strict ChatGPT conversation URL parsing for historical provenance."""

from __future__ import annotations

import re
import urllib.parse


def is_exact_conversation_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "chatgpt.com":
        return False
    return re.search(r"/c/[A-Za-z0-9-]+/?$", parsed.path) is not None


def custom_gpt_scope(value: str | None) -> str | None:
    if not is_exact_conversation_url(value):
        return None
    parsed = urllib.parse.urlparse(str(value))
    match = re.fullmatch(
        r"(/g/[A-Za-z0-9_-]+)/c/[A-Za-z0-9-]+/?",
        parsed.path,
    )
    return match.group(1) if match else None
