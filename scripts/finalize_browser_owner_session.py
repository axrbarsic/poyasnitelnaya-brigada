#!/usr/bin/env python3
"""Finalize one Browser-owner evidence session with one deterministic command."""

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


HISTORY_NAME = browser_owner_evidence.HISTORY_NAME
LEDGER_NAME = browser_owner_evidence.LEDGER_NAME
AGGREGATE_NAMES = browser_owner_evidence.AGGREGATE_NAMES
finalize_session = browser_owner_evidence.finalize_session


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--session-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = finalize_session(
            config_path=arguments.config,
            requested_session_dir=arguments.session_dir,
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
