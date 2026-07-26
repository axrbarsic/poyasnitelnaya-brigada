#!/usr/bin/env python3
"""Render portable LaunchAgent templates with absolute local paths."""

from __future__ import annotations

import argparse
import json
import plistlib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = PROJECT_ROOT / "macos"
TEMPLATES = (
    "com.axrbarsic.xmention.poll.plist",
    "com.axrbarsic.xmention.watchdog.plist",
    "com.axrbarsic.xmention.janitor.plist",
    "com.axrbarsic.xmention.dispatch.plist",
    "com.axrbarsic.xmention.codex-update.plist",
)


def render(config_path: Path, output_dir: Path) -> list[Path]:
    config = config_path.expanduser().resolve()
    config_payload = json.loads(config.read_text(encoding="utf-8"))
    poll_interval = int(config_payload.get("poll_interval_seconds", 300))
    watchdog_interval = int(
        config_payload.get("watchdog_interval_seconds", 60)
    )
    janitor_interval = int(
        config_payload.get("session_janitor_interval_seconds", 60)
    )
    janitor_minimum_age = int(
        config_payload.get("session_janitor_minimum_age_seconds", 60)
    )
    dispatch_interval = int(
        config_payload.get("app_server_dispatch_interval_seconds", 60)
    )
    codex_update_interval = int(
        config_payload.get("codex_cli_update_interval_seconds", 21600)
    )
    if (
        poll_interval <= 0
        or watchdog_interval <= 0
        or janitor_interval <= 0
        or dispatch_interval <= 0
        or codex_update_interval <= 0
        or janitor_minimum_age < 60
    ):
        raise ValueError(
            "LaunchAgent intervals must be positive and janitor age "
            "must be at least 60 seconds"
        )
    wake_value = str(config_payload.get("wake_file", "var/wake-request.json"))
    wake_candidate = Path(wake_value).expanduser()
    wake_path = (
        wake_candidate
        if wake_candidate.is_absolute()
        else (config.parent / wake_candidate).resolve()
    )
    output = output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    rendered: list[Path] = []

    for name in TEMPLATES:
        template = TEMPLATE_DIR / f"{name}.example"
        text = template.read_text(encoding="utf-8")
        text = text.replace("REPLACE_PROJECT_DIR", str(PROJECT_ROOT))
        text = text.replace("REPLACE_CONFIG_PATH", str(config))
        text = text.replace("REPLACE_WAKE_PATH", str(wake_path))
        text = text.replace("REPLACE_POLL_INTERVAL", str(poll_interval))
        text = text.replace(
            "REPLACE_WATCHDOG_INTERVAL",
            str(watchdog_interval),
        )
        text = text.replace(
            "REPLACE_JANITOR_INTERVAL",
            str(janitor_interval),
        )
        text = text.replace(
            "REPLACE_JANITOR_MINIMUM_AGE",
            str(janitor_minimum_age),
        )
        text = text.replace(
            "REPLACE_DISPATCH_INTERVAL",
            str(dispatch_interval),
        )
        text = text.replace(
            "REPLACE_CODEX_UPDATE_INTERVAL",
            str(codex_update_interval),
        )
        if "REPLACE_" in text:
            raise RuntimeError(f"Unresolved placeholder in {template.name}")
        payload = text.encode("utf-8")
        plistlib.loads(payload)
        destination = output / name
        destination.write_bytes(payload)
        rendered.append(destination)

    return rendered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    for path in render(args.config, args.output_dir):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
