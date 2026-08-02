#!/usr/bin/env python3
"""Versioned constants shared by X watcher domains."""

SCHEMA_VERSION = 11
TOKEN_ENV_NAMES = (
    "X_BEARER_TOKEN",
    "X_API_BEARER_TOKEN",
    "TWITTER_BEARER_TOKEN",
)
CONVERSATION_TAIL_SOURCE = "x_api_conversation_tail"
CONVERSATION_TAIL_SCAN_STATE_KEY = "conversation_tail_scan_state"
CONVERSATION_TAIL_LAST_ATTEMPT_KEY = "conversation_tail_last_attempt_at"
INITIAL_AUDIT_EXPIRY_PROVENANCE = "stored_api_auto_expiry_v1"
CHAIN_PROVENANCE_VALUES = frozenset({"short", "pro", "mixed"})
TERMINAL_BLOCKER_CODES = frozenset(
    {
        "account_unavailable",
        "missing_historical_pro_conversation",
        "reply_restricted",
        "required_pro_model_unavailable",
        "safety_restriction",
        "target_screenshot_unavailable",
        "target_unavailable",
    }
)
