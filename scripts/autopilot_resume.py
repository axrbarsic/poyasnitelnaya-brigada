#!/usr/bin/env python3
"""Retired CLI launcher kept as a fail-closed upgrade guard."""

from __future__ import annotations

import json
import sys


def main() -> int:
    json.dump(
        {
            "status": "retired",
            "error": (
                "Codex CLI cannot access the built-in Browser. "
                "Use scripts/autopilot_bridge.py from a Codex Desktop "
                "scheduled automation."
            ),
        },
        sys.stderr,
        sort_keys=True,
    )
    sys.stderr.write("\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
