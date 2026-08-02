#!/usr/bin/env python3
"""Adaptive continuation policy for manually published Alex parents."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

try:
    from scripts import json_contract
except ModuleNotFoundError:
    import json_contract  # type: ignore[no-redef]


LOCAL_MAX_TURN_PROVENANCE = {
    "pro",
    "local_sol_max_and_live_x_dom",
    "poyasnitelnaya_brigada_local_sol_max",
    "poyasnitelnaya_brigada_local_sol_max_and_live_x_dom",
}
SOURCE_URL_PATTERN = re.compile(r"https?://[^\s]+")


def replied_to_status_id(payload: dict[str, Any]) -> str | None:
    references = payload.get("referenced_tweets") or []
    if not isinstance(references, list):
        return None
    for reference in references:
        if not isinstance(reference, dict):
            continue
        if reference.get("type") != "replied_to":
            continue
        status_id = str(reference.get("id", "")).strip()
        if status_id.isdigit():
            return status_id
    return None


def _manual_parent_reference(
    connection: sqlite3.Connection,
    event_id: str,
    *,
    configured_user_id: str,
) -> tuple[str, bool] | None:
    event = connection.execute(
        """
        SELECT in_reply_to_user_id, payload_json
        FROM events
        WHERE event_id = ?
        """,
        (event_id,),
    ).fetchone()
    if event is None:
        return None
    try:
        payload = json_contract.loads(
            str(event["payload_json"]),
            source=f"event {event_id} payload_json",
        )
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    parent_status_id = replied_to_status_id(payload)
    if parent_status_id is None:
        return None
    is_direct_reply = (
        bool(configured_user_id)
        and str(event["in_reply_to_user_id"] or "") == configured_user_id
    )
    return parent_status_id, is_direct_reply


def _missing_parent_profile(parent_status_id: str) -> dict[str, Any]:
    return {
        "parent_status_id": parent_status_id,
        "parent_history_status": "missing_exact_alex_parent",
        "parent_origin_provenance": "unknown",
        "origin_proven": False,
        "recommended_route": "pending_exact_parent_restore",
        "required_action": "restore_exact_live_x_parent_then_classify_adaptively",
        "chatgpt_web_allowed": False,
    }


def _content_profile(exact_text: str) -> tuple[dict[str, Any], bool]:
    paragraphs = [
        value.strip()
        for value in re.split(r"\n\s*\n", exact_text)
        if value.strip()
    ]
    source_url_count = len(SOURCE_URL_PATTERN.findall(exact_text))
    substantive = (
        len(exact_text) >= 500
        or len(paragraphs) >= 3
        or source_url_count >= 1
    )
    return (
        {
            "code_points": len(exact_text),
            "paragraph_count": len(paragraphs),
            "source_url_count": source_url_count,
            "substantive": substantive,
        },
        substantive,
    )


def _continuation_basis(
    *,
    proven_local_max: bool,
    substantive: bool,
) -> str:
    if proven_local_max:
        return "proven_local_max_origin"
    if substantive:
        return "adaptive_manual_parent_content"
    return "concise_manual_parent_content"


def _exact_parent_profile(
    parent_status_id: str,
    parent: sqlite3.Row,
) -> dict[str, Any]:
    exact_text = str(parent["exact_text"] or "")
    provenance = str(parent["provenance"] or "").strip()
    content_profile, substantive = _content_profile(exact_text)
    proven_local_max = provenance in LOCAL_MAX_TURN_PROVENANCE
    adaptive_local_max = proven_local_max or substantive
    return {
        "parent_status_id": parent_status_id,
        "parent_history_status": "exact_alex_parent",
        "parent_origin_provenance": provenance or "manual_unknown",
        "origin_proven": bool(provenance),
        "content_profile": content_profile,
        "adaptive_local_max": adaptive_local_max,
        "recommended_route": "local-max" if adaptive_local_max else "short",
        "continuation_basis": _continuation_basis(
            proven_local_max=proven_local_max,
            substantive=substantive,
        ),
        "required_action": (
            "use_complete_local_history_and_poyasnitelnaya_brigada"
            if adaptive_local_max
            else "use_complete_local_history_and_sol_short"
        ),
        "chatgpt_web_allowed": False,
    }


def manual_parent_continuation_profile(
    connection: sqlite3.Connection,
    event_id: str,
    *,
    configured_user_id: str,
) -> dict[str, Any] | None:
    """Classify how to continue an exact Alex parent without guessing origin."""

    reference = _manual_parent_reference(
        connection,
        event_id,
        configured_user_id=configured_user_id,
    )
    if reference is None:
        return None
    parent_status_id, is_direct_reply = reference
    parent = connection.execute(
        """
        SELECT actor, exact_text, provenance
        FROM conversation_turns
        WHERE status_id = ?
        """,
        (parent_status_id,),
    ).fetchone()
    if parent is None:
        if not is_direct_reply:
            return None
        return _missing_parent_profile(parent_status_id)
    if str(parent["actor"]) != "alex":
        return None
    return _exact_parent_profile(parent_status_id, parent)
