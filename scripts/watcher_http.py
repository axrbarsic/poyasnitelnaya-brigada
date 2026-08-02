#!/usr/bin/env python3
"""Small authenticated JSON client for the X API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


def request_json(
    url: str,
    token: str,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "axrbarsic-x-mention-watcher/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read(1000).decode("utf-8", errors="replace")
        detail = ""
        try:
            payload = json.loads(body)
            detail = str(payload.get("title") or payload.get("detail") or "")
        except (TypeError, ValueError):
            detail = ""
        suffix = f" ({detail[:200]})" if detail else ""
        raise RuntimeError(f"X API HTTP {error.code}{suffix}") from error
