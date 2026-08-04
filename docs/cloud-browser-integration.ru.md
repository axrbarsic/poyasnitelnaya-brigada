# Прямой облачный Browser для X

## Что подключено

Codex получает облачный Chromium напрямую, без Hermes:

1. Проектный MCP `cloudBrowser` работает локально и в простое не создаёт
   браузерную сессию.
2. `cloud_browser_start` создаёт ровно одну сессию Browserbase с постоянным
   Context профиля X.
3. Внутри сессии запускается закреплённый официальный Playwright MCP. Наружу
   выходит только ограниченный набор инструментов. Опасный
   `browser_run_code_unsafe` исключён.
4. `cloud_browser_end` останавливает Playwright и немедленно просит Browserbase
   завершить сессию, чтобы не расходовать оплачиваемое время.

Hermes в этой цепочке отсутствует. Очередь, SQLite, дедупликация, evidence,
проверка точного опубликованного текста и media через X API остаются локальными.

## Почему не hosted Browserbase MCP

Hosted MCP хорош для простого чтения и проверки концепции, но его высокоуровневые
команды не дают достаточного контроля над точным DOM composer и загрузкой
локального файла. Для публикации в X проект использует Browserbase как удалённый
Chromium, а официальный Playwright MCP как точный интерфейс управления.

## Секреты и профиль

В `config.json` добавляется блок `cloud_browser`. В Git попадает только пример.

- `project_id`: Browserbase Project ID.
- `context_id`: один постоянный Context для профиля `@axrbarsic`.
- API key: только запись macOS Keychain с service
  `axrbarsic-x-mention-watcher-browserbase` и account `api-key`.

Записать API key после установки подписанного helper:

```bash
printf '%s' "$BROWSERBASE_API_KEY" | \
  var/keychain-helper set \
  axrbarsic-x-mention-watcher-browserbase api-key
```

Не вставляйте ключ в командную строку, конфигурацию Codex, URL MCP или чат.

## Порядок включения

1. Создать Browserbase project и API key.
2. Записать key в Keychain, заполнить `project_id` в `config.json`.
3. Создать один Browserbase Context и заполнить `context_id`.
4. Перезапустить Codex, вызвать `cloud_browser_preflight`.
5. Провести read-only canary на публичной странице вне X.
6. Через Live View один раз войти в X и закрыть сессию с сохранением Context.
7. Проверить восстановление авторизации в новой сессии.
8. Открыть точный X status, проверить author, text, parent и conversation.
9. Выполнить composer dry-run без нажатия Reply.
10. Выполнить upload dry-run из task-owned evidence.
11. Опубликовать один контролируемый ответ и доказать exact text, parent и media
    через официальный X API.
12. Только после зелёного canary переключить production backend. Встроенный
    Browser сохраняется как fallback.

## Ограничения

- Один Context нельзя использовать в двух одновременных Browserbase sessions.
- Блокировка экрана iMac не мешает облачному Chromium, но локальные Codex,
  watcher и dispatcher остановятся, если сам Mac уснёт или выключится.
- Browserbase тарифицирует активные сессии. Поэтому `start` и `end` являются
  обязательными границами одного claim, а не постоянным фоновым процессом.
- Первая загрузка закреплённого npm-пакета Playwright MCP потребует сеть и место
  в npm cache. Она выполняется только перед canary, а не в простое.

## Источники

- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)
- [Browserbase MCP overview](https://docs.browserbase.com/integrations/mcp/introduction)
- [Browserbase Contexts](https://docs.browserbase.com/platform/browser/core-features/contexts)
- [Browserbase Playwright quickstart](https://docs.browserbase.com/welcome/quickstarts/playwright)
- [Browserbase uploads](https://docs.browserbase.com/platform/browser/files/uploads)
- [Playwright MCP](https://github.com/microsoft/playwright-mcp)
