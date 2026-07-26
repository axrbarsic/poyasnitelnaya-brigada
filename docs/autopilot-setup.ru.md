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
Та же память может содержать до трех карантинных подсказок из внешнего корпуса.
Их запрещено использовать как доказательство до проверки точного публичного
поста в живом X или через официальный X API.

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
| Session janitor LaunchAgent | 5 минут | нет | Архив задач и recovery осиротевшего claim |
| Automation Codex Desktop | 5 минут | Sol High | Claim непустой очереди, Browser, решение, публикация, проверка |
| Кастомный GPT | только Pro | настроенный Pro | Длинный ответ в точной исторической conversation |

Минутный poll не использует токены. Пустой scheduled run расходует небольшой
контекст Sol, но не открывает Browser и не выполняет содержательную работу,
пока `dispatch` не равен `true`. Claim выполняется до чтения skills и references.
Архивация не входит в prompt automation и не расходует модель. Ее выполняет
локальный app-server janitor.

## Конфигурация

Добавьте локальные значения в игнорируемый `config.json`:

```json
{
  "autopilot_state_file": "var/autopilot-dispatch.json",
  "autopilot_health_file": "var/autopilot-health.json",
  "browser_owner_cwd": ".",
  "memory_guard_enabled": true,
  "memory_guard_state_file": "var/resource-health.json",
  "resource_mode": "auto",
  "resource_mode_idle_seconds": 900,
  "session_janitor_interval_seconds": 300,
  "session_janitor_orphan_owner_seconds": 300,
  "session_janitor_reap_helpers": true,
  "session_janitor_helper_grace_seconds": 120,
  "commenter_memory_limit": 12,
  "poll_interval_seconds": 60,
  "watchdog_interval_seconds": 60
}
```

Точка означает каталог рядом с `config.json`, то есть канонический корень
этого проекта. Не создавайте отдельный Browser owner workspace.

Вложенные ответы отслеживаются автоматически, как только в точной истории
диалога появляется ход `alex`. Список тематических ID корневых постов не нужен.
Вложенный комментарий всё равно требует живой классификации контекста перед
ответом автора.

Храните runtime state в игнорируемом `var/`. Не коммитьте `config.json`,
токены, cookies, SQLite, очередь или локальную историю.

## Установка сервисов без токенов

Сгенерируйте три LaunchAgent:

```bash
python3 scripts/render_launchd.py \
  --config /absolute/path/to/config.json \
  --output-dir /absolute/path/to/staging
```

Установите и загрузите:

- `com.axrbarsic.xmention.poll.plist`
- `com.axrbarsic.xmention.watchdog.plist`
- `com.axrbarsic.xmention.janitor.plist`

Не устанавливайте устаревший `com.axrbarsic.xmention.autopilot.plist`.

## Создание automation Codex Desktop

Создайте одну локальную запланированную задачу:

- модель: `gpt-5.6-sol`
- reasoning effort: `high`
- частота: каждые пять минут
- уведомления: только при неудачных запусках

Её постоянный prompt должен реализовывать эту машину состояний:

1. До чтения automation memory, skills и references выполнить
   `autopilot_bridge.py ... claim`.
2. Если статус равен `memory_deferred` либо `dispatch` равен `false`, оставить
   очередь без изменений и не открывать Browser.
3. Нормально завершить пустой run без `list_threads`, Browser и самоархивации.
4. Если `dispatch` равен `true`, выполнить `started --claim-token ...`.
5. Исполнить возвращенный prompt в текущем запуске как единственный Browser
   owner.
6. После exact history и durable resolution всех заявленных событий убедиться,
   что они исчезли из `wake-request.json`, затем выполнить
   `completed --claim-token ...`.
7. При ошибке до durable resolution выполнить
   `failed --claim-token ... --error ...`.
8. Закрыть все принадлежащие run Browser-вкладки и завершить задачу обычным
   финальным ответом. Не архивировать текущую задачу изнутри нее.
9. Не запускать X API postflight poll внутри automation. Polling и доступ к
   Keychain принадлежат LaunchAgent.

Сохраняйте следующие инварианты prompt:

- один claim token на один pending batch;
- idle: ноль Browser-вкладок;
- short: одна X-вкладка;
- Pro: одна X, одна ChatGPT и одна активная generation;
- все принадлежащие run вкладки закрываются перед завершением;
- каждый короткий ответ пишет и проверяет Sol High;
- никаких тематических исключений;
- postflight warning не откатывает durable resolution.

## Поведение при сбоях

- Ошибка API не двигает cursor X.
- Очередь и dispatcher state используют file locks и atomic writes.
- Глобальная аренда одного владельца подавляет overlapping scheduled runs,
  включая новый event, который появился во время уже активного claim.
- Повторный запуск сохраняет `work_in_progress` и не заменяет активный claim.
- Новый процесс Codex с другим runtime ID немедленно может reclaim очередь
  после перезапуска приложения.
- Janitor освобождает claim после обрыва stream только если связанная задача
  устарела. Статус `notLoaded` сам по себе не считается доказательством смерти.
- Janitor завершает только helper bundle, точно сопоставленный с уже
  завершенной X-задачей. Текущий владелец и неоднозначные процессы остаются
  нетронутыми.
- Resource guard до Browser откладывает работу при критической памяти, но не
  удаляет event и не двигает очередь.
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
