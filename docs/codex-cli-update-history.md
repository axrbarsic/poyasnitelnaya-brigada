# Хронология обновлений Codex CLI

Этот журнал получает запись только после успешной установки новой версии,
проверки `codex --version` и прохождения `codex doctor --summary`.

Автоматический updater сравнивает semantic versions и никогда не понижает
версию. Поэтому встроенный в Codex Desktop preview может временно быть новее
последнего публичного релиза GitHub.

## 2026-07-26T17:14:48.511136Z - 0.146.0-alpha.3.1 -> 0.146.0-alpha.10.1

- Релиз: https://github.com/openai/codex/releases/tag/rust-v0.146.0-alpha.10.1
- Проверка: `codex --version` и `codex doctor --summary` успешно.
- Кратко: добавлены постоянные pin-состояния потоков и single-writer защита
  paginated threads; улучшены wake для sleeping threads и fork paginated
  history; MCP runtime теперь стабильнее переиспользует, обновляет и
  восстанавливает соединения; усилены сетевые approvals, исправлено сохранение
  пользовательского ввода при прерывании MCP startup и подписаны bundled
  macOS helper binaries.

## 2026-07-27T12:47:27.059597Z - 0.146.0-alpha.10.1 -> 0.146.0-alpha.12

- Релиз: https://github.com/openai/codex/releases/tag/rust-v0.146.0-alpha.12
- Проверка: `codex --version` и `codex doctor --summary` успешно.
- Кратко: - Текст release notes для `0.146.0-alpha.12` не предоставлен.
- Поэтому достоверно перечислить новые возможности и исправления невозможно.

## 2026-07-28T06:48:30.896835Z - 0.146.0-alpha.12 -> 0.146.0-alpha.14

- Релиз: https://github.com/openai/codex/releases/tag/rust-v0.146.0-alpha.14
- Проверка: `codex --version` и `codex doctor --summary` успешно.
- Кратко: - Текст release notes для версии 0.146.0-alpha.14 не предоставлен.
- Поэтому достоверно перечислить новые возможности и исправления невозможно.

## 2026-07-29T06:51:58.819867Z - 0.146.0-alpha.14 -> 0.146.0

- Релиз: https://github.com/openai/codex/releases/tag/rust-v0.146.0
- Проверка: `codex --version` и `codex doctor --summary` успешно.
- Кратко: - Новые возможности управления сессиями: именование, закрепление, боковые беседы и форки, включая временные.
- Добавлена поддержка Agent Plugins, публикации workspace-плагинов и новых marketplace для Bedrock и Claude Code.
- Расширена интеграция: удалённые Code Mode hosts по WebSocket и web search для совместимых провайдеров.
- Исправлены прокси, переподключение MCP и Apps, сохранение истории после сбоев, а также отзывчивость терминала и работа Windows-навигации.

## 2026-07-29T12:57:04.002917Z - 0.146.0 -> 0.147.0-alpha.1

- Релиз: https://github.com/openai/codex/releases/tag/rust-v0.147.0-alpha.1
- Проверка: `codex --version` и `codex doctor --summary` успешно.
- Кратко: - Указана версия Codex CLI: `0.147.0-alpha.1`.
- Содержимое release notes не предоставлено, поэтому новые возможности и исправления определить нельзя.
