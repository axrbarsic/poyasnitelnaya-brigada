# Настройка автопилота Codex Desktop

[Русский](autopilot-setup.ru.md) | [English](autopilot-setup.md)

## Назначение

Система разделяет бесплатную механику и содержательную работу:

1. X watcher раз в минуту получает упоминания через официальный X API,
   дедуплицирует их и обновляет SQLite с очередью.
2. Watchdog контролирует свежесть poll и ошибки API.
3. Python dispatcher раз в минуту выполняет model-free `gate`.
4. Пустая, занятая или отложенная очередь завершается без модели, Browser и
   новой задачи Codex.
5. Готовая очередь запускает Codex Desktop в каноническом workspace, если
   приложение закрыто. При неудаче очередь сохраняется, запуск повторяется, а
   Alex получает локальное уведомление.
6. Одна существующая in-app heartbeat-сессия на Luna Low выполняет
   `reserve-handoff` и только затем один `send_message_to_thread`.
7. Закрепленная сессия Sol High атомарно делает `claim`, восстанавливает живую
   ветку, публикует и сохраняет точную историю.
8. После завершения supervisor может закрыть только тот Desktop, который
   запустил сам. Пользовательский Desktop он не закрывает.

Внешний app-server не выполняет relay или Browser-работу: его runtime не имеет
встроенной Browser-сессии Codex Desktop и desktop-only cross-thread tools.
Browser всегда принадлежит постоянной owner-сессии внутри приложения.

> Текущее состояние deployment на 2026-07-26: постоянный Sol High owner прошел
> read-only Browser preflight при заблокированном экране и обработал три
> события. Затем новый in-app relay самостоятельно передал следующее
> органическое событие, которое было опубликовано, импортировано и durably
> resolved. Практический тест обнаружил гонку двух соседних heartbeat; она
> закрыта атомарным `reserve-handoff` с TTL. Старая automation `x` остается
> paused до финальной проверки нового LaunchAgent.
>
> В том же live checkpoint изолированный пустой цикл прошёл через настоящий
> entry point dispatcher и вернул `idle`. Task IDs, relay и owner turns,
> количество app-server, node helper, renderer и dispatcher до и после не
> изменились.

Официальные основания архитектуры:

- [Codex app-server](https://learn.chatgpt.com/docs/app-server)
- [Scheduled tasks](https://learn.chatgpt.com/docs/automations)
- [Project config files](https://learn.chatgpt.com/docs/config-file/config-advanced#project-config-files-codexconfigtoml)
- [Built-in Browser](https://learn.chatgpt.com/docs/browser?surface=app)

## Компоненты

| Компонент | Частота | Модель | Ответственность |
| --- | --- | --- | --- |
| X watcher LaunchAgent | 1 минута | нет | API, дедупликация, SQLite, очередь |
| Watchdog LaunchAgent | 1 минута | нет | Контроль poll и API |
| Session janitor LaunchAgent | 1 минута | нет | Архив служебных задач, recovery claim |
| Event dispatcher LaunchAgent | 1 минута | нет при idle | Gate, запуск и managed shutdown Desktop |
| In-app relay heartbeat | пока Desktop открыт | Luna Low | Reservation и одно сообщение owner |
| Закрепленный Browser owner | по событию | Sol High | Claim, Browser, фактчек, публикация |
| Кастомный GPT | только Pro | настроенный Pro | Длинный ответ в исторической conversation |
| Codex CLI updater | 6 часов | нет без update | Version check, SHA-256, doctor, журнал |

Пустой terminal monitoring не расходует model tokens и не создает задач.
Штатный unattended режим держит Desktop закрытым. При событии supervisor
поднимает его, Luna получает короткий relay-turn, а после завершения managed
Desktop закрывается. Если Desktop открыт самим Alex, supervisor его не закрывает
и минутный heartbeat остается минимальной ценой in-app транспорта.

## Конфигурация

Проект должен быть trusted, иначе локальный `.codex/config.toml` игнорируется.
Добавьте в `~/.codex/config.toml`:

```toml
[projects."/absolute/path/to/x-mention-watcher"]
trust_level = "trusted"
```

Создайте `config.json` из примера и задайте:

```json
{
  "browser_owner_cwd": ".",
  "browser_owner_thread_id": "PINNED_SOL_OWNER_THREAD_ID",
  "desktop_relay_mode": "in_app_heartbeat",
  "desktop_auto_quit_after_work": true,
  "desktop_auto_quit_grace_seconds": 180,
  "relay_handoff_reservation_seconds": 180,
  "app_server_dispatch_interval_seconds": 60,
  "app_server_dispatch_state_file": "var/app-server-dispatch.json",
  "app_server_dispatch_lock_file": "var/app-server-dispatch.lock",
  "codex_cli_update_channel": "preview",
  "codex_cli_update_interval_seconds": 21600,
  "resource_mode": "auto",
  "memory_guard_enabled": true,
  "voice_priority_enabled": true,
  "voice_priority_hold_seconds": 300,
  "session_janitor_interval_seconds": 60,
  "session_janitor_minimum_age_seconds": 60,
  "commenter_memory_limit": 12,
  "poll_interval_seconds": 60,
  "watchdog_interval_seconds": 60
}
```

`browser_owner_thread_id` принадлежит одной закрепленной owner-сессии на Sol
High. `x-relay` является heartbeat одной существующей Luna Low сессии, а не
standalone automation. `reserve-handoff` не claim события, но атомарно блокирует
повторную отправку wake на 180 секунд. Успешный owner claim очищает reservation.

Ручной ответ Alex считается ходом `alex`. Если на него отвечают, Browser owner
поднимает точную живую ветку, сохраняет ранее не импортированный ручной ответ и
использует его вместе со всей историей до подготовки продолжения.

Событие от настроенного `user_id` является собственным ходом Alex, а не
входящей работой. Watcher импортирует точный текст и метаданные цепочки как ход
`alex`, устанавливает `delivery_state=self_authored` и не ставит такое событие
в очередь и не выполняет для него resolve. Старые unresolved собственные строки
идемпотентно исправляются командой
`python3 xmention_watcher.py --config config.json self-authored-reconcile`.
Команда не создаёт строку `event_resolutions` и ничего не меняет в X.

## Установка LaunchAgent

Сгенерируйте пять plist:

```bash
python3 scripts/render_launchd.py \
  --config /absolute/path/to/config.json \
  --output-dir /absolute/path/to/staging
```

Установите:

- `com.axrbarsic.xmention.poll.plist`
- `com.axrbarsic.xmention.watchdog.plist`
- `com.axrbarsic.xmention.janitor.plist`
- `com.axrbarsic.xmention.dispatch.plist`
- `com.axrbarsic.xmention.codex-update.plist`

Не создавайте пятиминутную Codex automation для X. Каждый standalone
scheduled run создает отдельную задачу и может оставлять helper-процессы в
долгоживущем Codex Desktop runtime. Старую automation нужно сначала поставить
на паузу, проверить новый dispatcher, затем удалить через официальный
`automation_update`.

## Машина состояний

1. LaunchAgent вызывает `scripts/app_server_dispatch.py`.
2. Python выполняет model-free `gate`.
3. `idle`, `work_in_progress` и `deferred_resources` записываются в state и
   завершаются без app-server.
4. При `ready` supervisor проверяет main process Codex Desktop и запускает
   `codex app CANONICAL_ROOT`, если процесс отсутствует.
5. In-app heartbeat выполняет атомарный `reserve-handoff`.
6. Победивший relay делает один `send_message_to_thread` в точный Browser owner
   с override Sol High. Соседний heartbeat получает `handoff_reserved`.
7. Browser owner выполняет `claim`, затем `started`.
8. Owner загружает `x-twitter-operator`, открывает минимум вкладок, выполняет
   double dedupe и publication transaction.
9. После exact history и durable resolution owner выполняет `completed`.
10. Если Desktop был запущен supervisor, пустая очередь и owner=null запускают
    grace timer, после которого завершается только сохраненный managed PID.

## Контракты памяти и качества

- Idle: ноль model tokens, ноль Browser-вкладок, ноль новых задач.
- Short: одна X-вкладка.
- Pro: одна X-вкладка, одна ChatGPT-вкладка, одна активная generation.
- Только Sol High принимает публикационные решения и пишет short.
- Luna не анализирует X и не формулирует ответы.
- Ресурсный guard ставит Browser на паузу, но не удаляет очередь.
- Активный голосовой разговор включает экономный режим и удерживает паузу.
- Блокировка дисплея не является ошибкой. При awake Mac owner выполняет один
  read-only Browser preflight и продолжает, если `iab` доступен.
- `reserve-handoff` и глобальный owner lease предотвращают перекрывающиеся
  relay и Browser-owner запуски.
- Janitor не должен убивать текущий Browser owner или неоднозначный процесс.

## Живой canary

1. Поставьте старую X automation на паузу.
2. Запустите один реальный unresolved event без передачи его ID модели.
3. Подтвердите естественное обнаружение watcher.
4. Подтвердите один relay-turn и один Sol High owner-turn.
5. Проверьте verified X URL, exact history и durable resolution.
6. Проверьте, что event исчез из очереди.
7. Выполните пустой цикл dispatcher и убедитесь, что число задач и helper
   процессов не выросло.
8. Только после этого удалите старую automation и оставьте LaunchAgent.

Успех нельзя имитировать прямой вставкой resolution. Любая проверка после
перезапуска должна начинаться с живого состояния очереди и точной ветки X.
