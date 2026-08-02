# Единый каталог проекта

## Канонический путь

Весь исполняемый проект хранится только здесь:

`/Users/alexlane/Developer/x-mention-watcher`

Внутри находятся:

```text
x-mention-watcher/
  .codex/                  настройки проектной сессии Codex
  AGENTS.md                локальный рабочий контракт
  docs/                    русская и английская документация
  macos/                   исходные шаблоны LaunchAgent
  scripts/                 dispatcher, bridge и служебные утилиты
  skill-backup/            восстанавливаемые версии всех трёх skills
  tests/                   все тесты
  var/                     изменяемое локальное состояние, вне Git
  *.py                     watcher, importer и snapshot tools
  config.json              локальная конфигурация, вне Git
  config.example.json      безопасный шаблон конфигурации
```

`browser_owner_cwd` равен `.`. Поэтому Browser owner запускается в том же
каноническом каталоге и не требует отдельного проекта в `Documents/Codex`.
Новые screenshots, ledgers, payloads и другие evidence должны сохраняться под
`var/evidence/browser-owner/<session-id>/`.

После durable работы с отдельными событиями ручная агрегация не требуется.
`finalize_browser_owner_session.py` проверяет точное совпадение каталогов
событий с активным claim, атомарно собирает два общих JSONL, вызывает
идемпотентный `browser-handoff-sync` и требует проверенный `manifest.json`.
Неполный или несогласованный набор блокирует завершение claim.

Старые evidence переносятся без изменения источника:

```bash
python3 evidence_import.py \
  --source /absolute/path/to/legacy-work \
  --label legacy-browser-owner-YYYY-MM-DD
python3 evidence_import.py \
  --audit var/evidence/browser-owner/legacy-browser-owner-YYYY-MM-DD
```

Импортёр запрещает symlinks и вероятные credentials, проверяет SHA-256 каждого
файла и атомарно публикует manifest. CLI всегда пишет только в канонический
`var/evidence/browser-owner`.

## Что находится вне каталога

Снаружи остаются только обязательные deployment points:

- `~/.codex/skills/x-twitter-operator`, установленная копия X skill;
- `~/.codex/skills/poyasnitelnaya-brigada`, установленная копия локального
  генератора;
- `~/.codex/skills/poyasnitelnaya-brigada-v2`, установленная копия
  основного генератора ответов и продолжений;
- `~/.codex/skills/377`, установленная копия локального визуального skill;
- `~/Library/LaunchAgents/com.axrbarsic.xmention.*.plist`, загруженные службы
  macOS;
- macOS Data Protection Keychain, где хранятся секреты. Подписанный helper
  собирается из `macos/XMentionKeychainHelper*` и устанавливается под `var/`;
- отдельный archive vault для исходных ZIP-архивов X.

Это не отдельные исходники проекта. Все четыре skills восстанавливаются из
`skill-backup/`, LaunchAgent из `macos/`, а секреты никогда не копируются в
репозиторий.

## Что не попадает в Git

Git содержит только воспроизводимую реализацию. Следующие данные остаются под
`var/` и игнорируются:

- live SQLite, WAL и SHM;
- очередь, health и lock files;
- Browser evidence и временные payloads;
- согласованные database snapshots;
- exports истории.

Официальные ZIP-архивы X хранятся в отдельном archive vault. Они не являются
частью проекта и не копируются в Git.

## Правило разработки

Временный Git worktree допустим только на время одного checkpoint. После
проверки, commit, push и fast-forward канонического каталога временный worktree
удаляется. Поэтому на диске не остаётся вторая рабочая копия проекта.

Контракт проверяется машинно:

```bash
python3 project_layout_audit.py --config config.json \
  --require-installed-skill
```
