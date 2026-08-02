#!/usr/bin/env python3
"""Commit one Browser-owner event outcome from canonical evidence.json."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import browser_owner_evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--claim-token", required=True)
    parser.add_argument("--event-dir", type=Path, required=True)
    parser.add_argument("--max", dest="maximum", type=int, default=4000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = browser_owner_evidence.commit_event(
            config_path=arguments.config,
            claim_token=arguments.claim_token,
            requested_event_dir=arguments.event_dir,
            maximum_length=arguments.maximum,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(
            json.dumps(
                {"status": "blocked", "message": str(error)},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
