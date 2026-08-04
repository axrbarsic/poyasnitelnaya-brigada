#!/usr/bin/env python3
"""Read-only health projection for Browser-owner rotation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


OPEN_PHASES = {"prepared", "thread_created", "committed"}


def check_rotation(
    root: Path,
    runtime: dict[str, Any],
    *,
    make_check: Callable[..., Any],
    read_json: Callable[[Path], Any],
    resolve_path: Callable[[Path, str], Path],
    parse_timestamp: Callable[[Any], datetime | None],
) -> list[Any]:
    configured = str(runtime.get("owner_rotation_state_file", "")).strip()
    if not configured:
        return []
    path = resolve_path(root, configured)
    if not path.exists():
        return [
            make_check(
                "runtime.owner_rotation",
                "pass",
                "Ротация Browser owner ещё не запускалась.",
                "Действие не требуется.",
            )
        ]
    try:
        state = read_json(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return [
            make_check(
                "runtime.owner_rotation",
                "fail",
                "Состояние ротации Browser owner повреждено.",
                "Восстанови транзакцию только из config, Codex state и x-relay.",
                {"error": type(error).__name__},
            )
        ]
    transaction = state.get("transaction")
    if transaction is None:
        return [
            make_check(
                "runtime.owner_rotation",
                "pass",
                "Незавершённой ротации Browser owner нет.",
                "Действие не требуется.",
                {"generation": int(state.get("generation", 0))},
            )
        ]
    if not isinstance(transaction, dict):
        phase = ""
        updated_at = None
    else:
        phase = str(transaction.get("phase", ""))
        updated_at = parse_timestamp(transaction.get("updated_at"))
    if phase not in OPEN_PHASES or updated_at is None:
        return [
            make_check(
                "runtime.owner_rotation",
                "fail",
                "Транзакция ротации Browser owner невалидна.",
                "Не создавай новую задачу. Восстанови текущую транзакцию по журналу.",
                {"phase": phase},
            )
        ]
    age_seconds = max(
        0,
        int((datetime.now(timezone.utc) - updated_at).total_seconds()),
    )
    maximum = int(runtime.get("max_owner_rotation_age_seconds", 900))
    stale = age_seconds > maximum
    return [
        make_check(
            "runtime.owner_rotation",
            "fail" if stale else "warn",
            (
                "Транзакция ротации Browser owner зависла."
                if stale
                else "Транзакция ротации Browser owner выполняется."
            ),
            (
                "Продолжи сохранённую фазу, не создавая новую задачу."
                if stale
                else "Дождись следующего heartbeat."
            ),
            {
                "phase": phase,
                "age_seconds": age_seconds,
                "maximum_seconds": maximum,
                "old_thread_id": transaction.get("old_thread_id"),
                "new_thread_id": transaction.get("new_thread_id"),
            },
        )
    ]
