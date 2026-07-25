#!/usr/bin/env python3
"""Build the durable Codex Desktop wake contract for queued X events."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts import autopilot_dispatch
except ModuleNotFoundError:
    import autopilot_dispatch  # type: ignore[no-redef]


WAKE_CONTRACT = """СТОЯЧЕЕ РАЗРЕШЕНИЕ X-АВТОПИЛОТА.
Ты являешься единственным Browser owner. Обработай перечисленные ответы как
Sol High. Используй skill x-twitter-operator и встроенный Browser.

Для каждого события открой точный URL, восстанови полную ветку и историю,
проверь media и позицию автора, выполни актуальный фактчек первичными
источниками, двойную проверку дубля и выбери short, Pro или skip. Short пиши и
финально проверяй только Sol High. Follow-up к Pro отправляй только
screenshot-only с 0 символов в точную историческую conversation.

Событие разрешено, если это прямой ответ @axrbarsic либо новый комментарий в
любом диалоге, где точная сохраненная история уже содержит ход @axrbarsic.
Никаких тематических исключений или списков особых публикаций нет. Для
вложенного комментария сначала проверь, действительно ли ответ автора полезен
и не вмешивается ли он в чужой разговор без нового тезиса. В durable handoff
укажи ровно один подтвержденный маршрут:
direct_reply_to_axrbarsic=true либо tracked_conversation_reply=true.

Публикуй без дополнительного одобрения только релевантные ответы в рамках
ранее разрешенного X workflow. Не ставь лайки, не делай репосты, подписки,
личные сообщения, новые исходные посты и удаления. После каждой публикации
проверь точный URL, сохрани полную историю в watcher и durable resolve. На
iMac 8 GB держи одну вкладку X, одну ChatGPT и максимум один активный Pro.
После обработки закончи. Следующий X API poll выполняет LaunchAgent. Не
создавай автоматики, задачи или Browser helpers. В финале укажи event ID,
disposition и verified reply URL.
"""


def resolve_path(config_path: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return (config_path.resolve().parent / candidate).resolve()


def load_workspace(config_path: Path) -> Path:
    config = autopilot_dispatch.read_json(config_path)
    browser_owner_cwd = resolve_path(
        config_path,
        str(config.get("browser_owner_cwd", ".")),
    )
    if not browser_owner_cwd.is_dir():
        raise ValueError(
            f"browser_owner_cwd is not a directory: {browser_owner_cwd}"
        )
    return browser_owner_cwd


def build_prompt(
    events: Sequence[dict[str, Any]],
    *,
    config_path: Path,
    browser_owner_cwd: Path,
) -> str:
    payload = json.dumps(
        {"events": list(events)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        f"{WAKE_CONTRACT}\n\n"
        f"WATCHER_CONFIG={config_path.resolve()}\n"
        f"BROWSER_OWNER_WORKSPACE={browser_owner_cwd.resolve()}\n"
        f"EVENTS_JSON:\n{payload}\n"
    )
