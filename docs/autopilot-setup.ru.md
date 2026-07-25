# Настройка автопилота Codex Desktop

[Русский](autopilot-setup.ru.md) | [English](autopilot-setup.md)

## Назначение

Watcher обнаруживает ответы X без токенов модели. Локальная automation Codex
Desktop каждые пять минут проверяет durable очередь. При пустой очереди запуск
завершается до открытия Browser. Непустая очередь атомарно арендуется и
обрабатывается в этом же scheduled run на Sol High.

Каждый непустой claim получает до `commenter_memory_limit` точных публичных
взаимодействий с тем же стабильным X user ID. Более глубокая сохраненная история
доступна Sol через `commenter-history`, только когда она полезна текущему ответу.

Codex CLI намеренно исключен из Browser-работы. Официальное руководство Codex
указывает, что встроенный Browser недоступен в Codex CLI и IDE extension.
`scripts/autopilot_resume.py` является fail-closed защитой старой установки.
Смотрите официальную документацию
[Built-in browser](https://learn.chatgpt.com/docs/browser?surface=app) и
[Scheduled tasks](https://learn.chatgpt.com/docs/automations.md).

## Компоненты

| Компонент | Частота | Модель | Ответственность |
| --- | --- | --- | --- |
| X watcher LaunchAgent | 1 минута | нет | Получение, дедупликация, SQLite, очередь |
| Watchdog LaunchAgent | 1 минута | нет | Контроль polling и ошибок API |
| Automation Codex Desktop | 5 минут | Sol High | Claim непустой очереди, Browser, решение, публикация, проверка |
| Кастомный GPT | только Pro | настроенный Pro | Длинный ответ в точной исторической conversation |

Минутный poll не использует токены. Пустой scheduled run расходует небольшой
контекст Sol, но не открывает Browser и не выполняет содержательную работу,
пока `dispatch` не равен `true`.

## Конфигурация

Добавьте локальные значения в игнорируемый `config.json`:

```json
{
  "autopilot_state_file": "var/autopilot-dispatch.json",
  "autopilot_health_file": "var/autopilot-health.json",
  "browser_owner_cwd": "/absolute/path/to/browser-owner-workspace",
  "commenter_memory_limit": 12,
  "poll_interval_seconds": 60,
  "watchdog_interval_seconds": 60
}
```

Вложенные ответы отслеживаются автоматически, как только в точной истории
диалога появляется ход `alex`. Список тематических ID корневых постов не нужен.
Вложенный комментарий всё равно требует живой классификации контекста перед
ответом автора.

Храните runtime state в игнорируемом `var/`. Не коммитьте `config.json`,
токены, cookies, SQLite, очередь или локальную историю.

## Установка сервисов без токенов

Сгенерируйте только poll и watchdog LaunchAgents:

```bash
python3 scripts/render_launchd.py \
  --config /absolute/path/to/config.json \
  --output-dir /absolute/path/to/staging
```

Установите и загрузите:

- `com.axrbarsic.xmention.poll.plist`
- `com.axrbarsic.xmention.watchdog.plist`

Не устанавливайте устаревший `com.axrbarsic.xmention.autopilot.plist`.

## Создание automation Codex Desktop

Создайте одну локальную запланированную задачу:

- модель: `gpt-5.6-sol`
- reasoning effort: `high`
- частота: каждые пять минут
- уведомления: только при неудачных запусках

Её постоянный prompt должен реализовывать эту машину состояний:

1. Выполнить `autopilot_bridge.py ... claim`.
2. Если `dispatch` равен `false`, завершиться без Browser, изменений файлов,
   сообщений и inbox item.
3. Если `dispatch` равен `true`, выполнить `started --claim-token ...`.
4. Исполнить возвращенный prompt в текущем запуске как единственный Browser
   owner.
5. После exact history и durable resolution всех заявленных событий убедиться,
   что они исчезли из `wake-request.json`, затем выполнить
   `completed --claim-token ...`.
6. При ошибке до durable resolution выполнить
   `failed --claim-token ... --error ...`.
7. Не запускать X API postflight poll внутри automation. Polling и доступ к
   Keychain принадлежат LaunchAgent.

Сохраняйте следующие инварианты prompt:

- один claim token на один pending batch;
- максимум одна вкладка X, одна ChatGPT и одна активная Pro generation;
- каждый короткий ответ пишет и проверяет Sol High;
- никаких тематических исключений;
- postflight warning не откатывает durable resolution.

## Поведение при сбоях

- Ошибка API не двигает cursor X.
- Очередь и dispatcher state используют file locks и atomic writes.
- Аренда 30 минут подавляет overlapping scheduled runs.
- Повторный запуск сохраняет `work_in_progress` и не заменяет активный claim.
- Ошибка до durable resolution освобождает точный claim.
- `completed` принимается только после исчезновения заявленных ID из wake
  queue.
- `reconcile-completed` дополнительно проверяет каждый event в SQLite перед
  исправлением postflight health state.
- `blocked_pending_iab` не является успехом и не resolve событие.
- Старый CLI launcher завершается с ошибкой до claim и расхода модели.

## Живой canary

Используйте один или несколько реальных unresolved replies:

1. Поставьте Desktop automation на паузу и выгрузите poll/watchdog.
2. Сохраните backup SQLite и runtime JSON.
3. Удалите только canary events и открутите `since_id` до непосредственно
   предыдущего наблюдаемого ID.
4. Подтвердите, что очередь, dispatch state, история и resolutions больше не
   знают эти события.
5. Загрузите poll/watchdog и активируйте Desktop automation, не передавая ей
   event ID.
6. Требуйте естественное повторное обнаружение через API, один атомарный claim,
   одну verified publication на событие, exact history и durable resolution.
7. Подтвердите пустую очередь и пустой dispatch state.

Нельзя имитировать успех прямой вставкой resolution.
