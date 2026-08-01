# Настройка автопилота Codex Desktop

[Русский](autopilot-setup.ru.md) | [English](autopilot-setup.md)

## Назначение

Система разделяет бесплатную механику и содержательную работу:

1. X watcher раз в минуту получает упоминания через официальный X API и
   проверяет хвост недавних conversation ID с точным ходом Alex. Затем он
   дедуплицирует события и обновляет SQLite с очередью.
2. Token-free supervisor контролирует свежесть poll, системный контракт и
   ошибки API. Однозначную stale-poll поломку он один раз ремонтирует сам.
3. Python dispatcher раз в минуту сначала выполняет repair gate, затем X gate.
4. Пустая, занятая или отложенная очередь завершается без модели, Browser и
   новой задачи Codex.
5. Готовая очередь запускает Codex Desktop в каноническом workspace, если
   приложение закрыто. При неудаче очередь сохраняется, запуск повторяется, а
   Alex получает локальное уведомление.
6. Одна существующая in-app heartbeat-сессия на Luna Low выполняет один
   `relay-reserve-handoff`. Python сам проверяет durable repair incident, затем
   X queue и атомарно резервирует единственный маршрут. Luna сразу делает один
   direct `send_message_to_thread`. Codex ставит follow-up в очередь или
   направляет его в активный turn.
7. Закрепленная сессия Sol Max атомарно делает `claim`, восстанавливает живую
   ветку, публикует и сохраняет точную историю.
8. После завершения supervisor может закрыть только тот Desktop, который
   запустил сам. Пользовательский Desktop он не закрывает.

Внешний app-server не выполняет relay или Browser-работу: его runtime не имеет
встроенной Browser-сессии Codex Desktop и desktop-only cross-thread tools.
Browser всегда принадлежит постоянной owner-сессии внутри приложения.

> Текущее состояние deployment на 2026-07-30: постоянный Sol Max owner и Luna
> Low relay проходят doctor без FAIL. Маршрут «Пояснительной бригады»
> выполняется локальным skill без ChatGPT, использует exact durable history и
> проверяет непустой ответ не длиннее 4000 Unicode code points без искусственного
> увеличения текста.
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
| X watcher LaunchAgent | 1 минута | нет | Упоминания, хвост разговоров, дедупликация, SQLite, очередь |
| Supervisor LaunchAgent | 1 минута | нет | Doctor, allowlist ремонта, durable incident |
| Session janitor LaunchAgent | 1 минута | нет | Архив служебных задач, recovery claim |
| Event dispatcher LaunchAgent | 1 минута | нет при idle | Gate, запуск и managed shutdown Desktop |
| In-app relay heartbeat | пока Desktop открыт | Luna Low | Reservation и одно сообщение owner |
| Закрепленный Browser owner | по событию | Sol Max | Claim, Browser, фактчек, публикация |
| Локальный skill «Пояснительная бригада» | только local-max | Sol Max | Не более 4000 code points, история, источники |
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
  "autopilot_supervisor_state_file": "var/autopilot-supervisor.json",
  "autopilot_supervisor_repair_cooldown_seconds": 90,
  "autopilot_supervisor_escalation_retry_seconds": 1800,
  "codex_cli_update_channel": "preview",
  "codex_cli_update_interval_seconds": 21600,
  "resource_mode": "auto",
  "memory_guard_enabled": true,
  "memory_guard_swap_blocks_dispatch": false,
  "voice_priority_enabled": true,
  "voice_priority_hold_seconds": 300,
  "session_janitor_interval_seconds": 60,
  "session_janitor_minimum_age_seconds": 60,
  "commenter_memory_limit": 12,
  "conversation_tail_enabled": true,
  "conversation_tail_poll_interval_seconds": 300,
  "conversation_tail_watch_hours": 24,
  "conversation_tail_initial_lookback_hours": 2,
  "conversation_tail_overlap_seconds": 120,
  "conversation_tail_max_conversations": 80,
  "conversation_tail_daily_post_read_limit": 200,
  "conversation_tail_max_post_reads_per_poll": 50,
  "poll_interval_seconds": 60,
  "watchdog_interval_seconds": 60
}
```

Лента собственных упоминаний продолжает проверяться раз в минуту. Более
дорогой Recent Search по хвостам цепочек запускается раз в пять минут, не
запрашивает расширенные ресурсы User и Media и имеет отдельный лимит чтения.
Исчерпание лимита хвостов не останавливает собственные упоминания. Текущие
счетчики доступны в `python3 xmention_watcher.py --config config.json status`.

`browser_owner_thread_id` принадлежит одному выделенному service worker на Sol
Max. `x-relay` является heartbeat одной существующей Luna Low сессии, а не
standalone automation. `reserve-handoff` не claim события, но атомарно блокирует
повторную отправку wake на 180 секунд. Relay не читает owner thread как
дополнительный gate. Точный thread, model и thinking возвращает versioned
contract, а process-local `hostId` не передаётся. Relay сразу отправляет ровно
один direct follow-up в service worker, а Codex ставит его в очередь или
направляет в активный turn. При ошибке доставки relay выполняет
`release-handoff` с точным reservation token. Успешный owner claim переводит
reservation в состояние `claimed`. Atomic reservation и глобальный owner claim
не допускают двух Browser-owner. Восстановимый prompt heartbeat хранится в
[`macos/x-relay.prompt.txt`](../macos/x-relay.prompt.txt).

Repair handoff не прерывает активный X claim. Relay проверяет глобальный owner
до repair reservation и сразу после неё. Если X owner успел стать активным,
relay освобождает только repair reservation и ждёт durable завершения X.

Ручной ответ Alex считается ходом `alex`. Если на него отвечают, Browser owner
поднимает точную живую ветку, сохраняет ранее не импортированный ручной ответ и
использует его вместе со всей историей до подготовки продолжения.
Происхождение хода и способ продолжения разделены. Claim payload содержит
профиль `manual_parent_continuation`, построенный по точному сохраненному
parent. Доказанный local-max origin, 500 или больше Unicode code points, три
или больше абзацев либо хотя бы одна source URL выбирают локальный skill.
Краткий parent без этих признаков может остаться Sol short. Неизвестное ручное
происхождение остается неизвестным, и ни один маршрут не открывает ChatGPT web.

История local-max ветки теперь восстанавливается из SQLite и append-only JSONL.
Старые custom-GPT URL и migration records сохраняются только как audit trail.
Команда совместимости `pro-model-recovery-requeue` без event ID возвращает в
durable очередь старые model, conversation и screenshot blockers, устраненные
локальным skill.

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

1. Supervisor LaunchAgent выполняет doctor и при необходимости один
   allowlisted ремонт без модели.
2. LaunchAgent вызывает `scripts/app_server_dispatch.py`.
3. Python сначала выполняет model-free repair gate, затем X gate.
4. `idle`, `work_in_progress` и `deferred_resources` записываются в state и
   завершаются без app-server.
5. При `ready` supervisor проверяет main process Codex Desktop и запускает
   `codex app CANONICAL_ROOT`, если процесс отсутствует.
6. In-app heartbeat выполняет один `relay-reserve-handoff`. Python сам
   резервирует repair incident либо, при его отсутствии, X queue и возвращает
   единственный `dispatch` вместе с точным `route`.
7. Победивший relay делает один direct `send_message_to_thread` с override Sol
   Max без отдельного live-чтения owner thread. Codex ставит follow-up в
   очередь или направляет его в активный turn.
8. При ошибке доставки relay выполняет `release-handoff` со своим точным
   reservation token и завершается. Очередь остается pending. Соседний
   heartbeat получает `handoff_reserved`, а глобальный owner claim не допускает
   двух Browser-owner.
9. Repair owner выполняет doctor claim и started, чинит минимально и закрывает
   incident только после нулевого FAIL и отчета. X owner выполняет X claim и
   started.
10. X owner загружает `x-twitter-operator`, открывает минимум вкладок, выполняет
   double dedupe и publication transaction.
11. После exact history и durable resolution owner выполняет `completed`.
    Если dispatcher уже снял lease, `completed` автоматически завершает
    reconciliation только после проверки нулевой pending queue и durable
    SQLite resolution для каждого claimed event.
12. Если Desktop был запущен supervisor, пустая очередь и owner=null запускают
    grace timer, после которого завершается только сохраненный managed PID.

Пока Desktop готов, а owner отсутствует, dispatcher сохраняет один
`waiting_since` между минутными тиками. `system_doctor` поднимает
`runtime.relay_progress` только когда ожидание без owner превышает версионный
контракт `max_relay_wait_seconds`. Resource deferral и активный owner не
считаются остановкой relay.

Отдельная проверка `runtime.queue_latency` измеряет `first_seen_at` самого
старого события независимо от свежести poll и dispatcher. Превышение
`max_queue_age_seconds` без owner является FAIL. Если именно старейшее событие
уже находится в активном claim, doctor возвращает WARN и требует проверить
renew и durable завершение.

## Контракты памяти и качества

- Idle: ноль model tokens, ноль Browser-вкладок, ноль новых задач.
- Short: одна X-вкладка.
- Local-max: одна X-вкладка, локальный skill и ноль ChatGPT-вкладок.
- Только Sol принимает публикационные решения. Локальный local-max route всегда
  требует effort `max`.
- Luna не анализирует X и не формулирует ответы.
- Ресурсный guard ставит Browser на паузу, но не удаляет очередь.
- Лимиты renderer считаются эквивалентами по 256 МиБ RSS, при этом сырое
  количество процессов и суммарный renderer RSS остаются видимыми. Легкие
  кэшированные renderer не блокируют Browser только из-за изменений
  внутренней архитектуры Codex.
- `node_repl_count` и `mcp_process_count` считаются эквивалентами по 64 МиБ
  RSS. Сырые количества остаются видны как `node_repl_process_count` и
  `mcp_raw_process_count`, поэтому пустые helper-процессы не исчерпывают лимиты.
- Активный голосовой разговор включает экономный режим и удерживает паузу.
- Блокировка дисплея не является ошибкой. При awake Mac owner выполняет один
  read-only Browser preflight и продолжает, если `iab` доступен.
- `reserve-handoff` и глобальный owner lease предотвращают перекрывающиеся
  relay и Browser-owner запуски.
- Python не создает reservation, пока durable канонический owner активен. Если
  другой turn начался во время доставки, Codex ставит direct follow-up в
  очередь или направляет его в активный turn. Mobile Remote, локальный Desktop
  и автоматический Browser owner используют один durable thread, а глобальный
  owner claim сериализует публикацию.
- Janitor не должен убивать текущий Browser owner или неоднозначный процесс.

## Живой canary

1. Поставьте старую X automation на паузу.
2. Запустите один реальный unresolved event без передачи его ID модели.
3. Подтвердите естественное обнаружение watcher.
4. Подтвердите один relay-turn и один Sol Max owner-turn.
5. Проверьте verified X URL, exact history и durable resolution.
6. Проверьте, что event исчез из очереди.
7. Выполните пустой цикл dispatcher и убедитесь, что число задач и helper
   процессов не выросло.
8. Только после этого удалите старую automation и оставьте LaunchAgent.

Успех нельзя имитировать прямой вставкой resolution. Любая проверка после
перезапуска должна начинаться с живого состояния очереди и точной ветки X.
Event ID, ссылку и готовый ответ модели заранее не передавать. Watcher обязан
сам обнаружить органическое неотвеченное событие.
