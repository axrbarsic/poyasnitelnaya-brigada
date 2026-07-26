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
