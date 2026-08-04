#!/usr/bin/env python3
"""Credential lookup, preflight checks, and local notifications."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from scripts import watcher_constants


@dataclass(frozen=True)
class Dependencies:
    run: Callable[..., Any]
    verify_keychain_bundle: Callable[[Path], Any]
    platform: str


def bearer_token_with_source(
    config: Any,
    *,
    dependencies: Dependencies,
) -> tuple[str, str]:
    for name in watcher_constants.TOKEN_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            return value, f"environment:{name}"
    if (
        dependencies.platform == "darwin"
        and config.keychain_service
        and config.keychain_account
    ):
        try:
            if config.keychain_helper.is_file() and os.access(
                config.keychain_helper,
                os.X_OK,
            ):
                try:
                    verification = dependencies.verify_keychain_bundle(
                        config.keychain_helper
                    )
                except Exception as error:
                    raise RuntimeError(
                        "Unable to verify the configured Keychain helper"
                    ) from error
                if not verification.ok:
                    raise RuntimeError("Refusing an unverified Keychain helper")
                verified_helper = Path(verification.executable_path)
                command = [
                    str(verified_helper),
                    "get",
                    config.keychain_service,
                    config.keychain_account,
                ]
                source = "keychain_helper"
            else:
                command = [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    config.keychain_service,
                    "-a",
                    config.keychain_account,
                    "-w",
                ]
                source = "keychain"
            result = dependencies.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except RuntimeError:
            raise
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(
                "Unable to read X API bearer token from Keychain"
            ) from error
        value = result.stdout.strip()
        if value:
            return value, source
    raise RuntimeError(
        "Missing X API bearer token. Set an environment variable or configured Keychain item."
    )


def bearer_token(config: Any, *, dependencies: Dependencies) -> str:
    return bearer_token_with_source(config, dependencies=dependencies)[0]


def preflight(config: Any, *, dependencies: Dependencies) -> dict[str, Any]:
    user_id_valid = bool(config.user_id and config.user_id.isdigit())
    try:
        _, source = bearer_token_with_source(config, dependencies=dependencies)
        token_available = True
    except RuntimeError:
        source = None
        token_available = False
    return {
        "config": str(config.source_path),
        "user_id_configured": user_id_valid,
        "token_available": token_available,
        "token_source": source,
        "database_parent_writable": os.access(config.database.parent, os.W_OK)
        if config.database.parent.exists()
        else os.access(config.database.parent.parent, os.W_OK),
        "ready_for_live_poll": user_id_valid and token_available,
    }


def notify_macos(
    config: Any,
    title: str,
    body: str,
    *,
    dependencies: Dependencies,
) -> None:
    if not config.notifications_enabled or dependencies.platform != "darwin":
        return
    try:
        dependencies.run(
            [
                "/usr/bin/osascript",
                "-e",
                "on run argv",
                "-e",
                "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e",
                "end run",
                title,
                body,
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass
