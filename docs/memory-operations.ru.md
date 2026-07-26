# Эксплуатация на iMac с 8 ГБ памяти

[Русский](memory-operations.ru.md) | [English](memory-operations.md)

## Найденная первопричина

Standalone scheduled task Codex создавал новую задачу каждые пять минут. Даже
после завершения в долгоживущем runtime Codex Desktop оставались дочерние
`node_repl` и MCP helper. Возраст процессов образовал точную пятиминутную
лестницу. Одновременно sidebar заполнялся одинаковыми задачами, Electron
перерисовывал длинный список, росли RSS и swap.

Архивирование уменьшало визуальный шум, но не устраняло источник. Поэтому
пятиминутная Codex automation выведена из эксплуатации.

## Новый малоресурсный контракт

1. Poll, watchdog, janitor и Desktop supervisor работают обычным Python.
2. Idle цикл расходует ноль model tokens, не создает задачу и не запускает
   Desktop или Browser.
3. Готовая очередь запускает Codex Desktop, только если он закрыт.
4. Одна существующая in-app heartbeat-сессия на Luna Low атомарно резервирует
   handoff и отправляет одно сообщение закрепленному Sol High owner.
5. Соседний heartbeat не может передать ту же очередь повторно.
6. Browser owner атомарно делает claim и остается единственным владельцем.
7. Short использует одну X-вкладку.
8. Pro использует максимум одну X, одну ChatGPT и одну generation.
9. Все task-owned вкладки закрываются при terminal исходе.
10. Dispatcher проверяет фактическое исчезновение каждого ID из очереди.
11. Освобожденный unresolved claim считается ошибкой, а не успехом.
12. Активный голосовой разговор откладывает только Browser, не poll и очередь.
13. Janitor архивирует старые служебные задачи, но текущая архитектура больше
    не создает новую задачу каждую минуту.
14. После пустой очереди supervisor закрывает только тот Desktop PID, который
    запустил сам. Desktop, открытый Alex, он не закрывает.

## Автоматические режимы

`resource_mode: auto` влияет только на допуск Browser. Sol High, фактчекинг и
контракт публикации не упрощаются.

| Режим | Условие | RSS Codex | Renderer | Свободная память | Swap |
| --- | --- | ---: | ---: | ---: | ---: |
| Экономный | голос или давление памяти | 2200 MiB | 5 | минимум 20% | до 896 MiB |
| Сбалансированный | пользователь работает | 2350 MiB | 6 | минимум 14% | до 1024 MiB |
| Производительный | простой 15 минут, питание от сети, свободно минимум 35% | 2500 MiB | 7 | минимум 12% | до 1152 MiB |

Жесткий предохранитель действует всегда: RSS 2700 MiB, максимум 8 renderer,
минимум 10% свободной памяти и swap до 1280 MiB.

Большое число старых легких helper само по себе не блокирует очередь, если:

- свободно достаточно памяти;
- RSS Codex не превышает recovery limit;
- суммарный RSS helper не превышает 512 MiB;
- renderer остается в лимите.

Это временный recovery envelope для уже накопленных процессов. После удаления
старой automation и одного перезапуска Codex Desktop legacy helper исчезнут и
не должны накапливаться снова.

## Голосовой приоритет

Минутный watcher, SQLite и очередь продолжают работать во время разговора.
Resource guard блокирует только тяжелый Browser owner. Последнее наблюдение
микрофона удерживает паузу еще
`voice_priority_hold_seconds`, по умолчанию 300 секунд.

Так ответы не теряются, но realtime voice получает память и CPU раньше
Browser-прохода. После паузы накопленная очередь обрабатывается обычным Sol
High owner.

## Измеренный расход

Исторический вариант с Luna scheduled task использовал в среднем 49 064
обработанных токена на пустой или отложенный запуск. При интервале пять минут
это примерно 588 768 обработанных токенов в час.

Новый idle путь:

| Этап | Model tokens | Новая задача | Browser |
| --- | ---: | ---: | ---: |
| X poll | 0 | 0 | нет |
| Watchdog | 0 | 0 | нет |
| Resource gate | 0 | 0 | нет |
| Session janitor | 0 | 0 | нет |
| Idle dispatcher | 0 | 0 | нет |

При обычном unattended idle supervisor-owned Desktop закрыт, поэтому Luna Low
не запускается. При реальной готовой очереди Luna выполняет короткую передачу.
Если Alex намеренно оставил Desktop открытым, in-app heartbeat продолжает свой
маленький минутный Luna gate. Основной расход Sol High пропорционален числу и
сложности реальных ответов, а не времени работы мониторинга.

## Архивация

Официальная документация Codex указывает, что standalone scheduled run начинает
новый chat. Поэтому модельная housekeeping automation непригодна: она сама
плодит задачи.

`scripts/session_janitor.py` запускается LaunchAgent каждую минуту. Он:

- архивирует только завершенные точно сопоставленные служебные задачи;
- не трогает активного или защищенного Browser owner;
- сохраняет историю для восстановления;
- освобождает осиротевший claim только по строгому времени и состоянию;
- пишет один перезаписываемый health state вместо растущего журнала.

Legacy helper от уже выполненных scheduled runs безопаснее окончательно
освободить одним перезапуском Codex Desktop после безопасной границы. Новый
dispatcher не запускает app-server в production и никогда не убивает чужие
процессы по эвристике.

## Проверка после обновления

1. Запустить unit test suite.
2. Выполнить idle dispatcher и подтвердить ноль новых задач.
3. Сравнить PID и RSS helper до и после idle цикла, роста быть не должно.
4. Дождаться реального X event.
5. Подтвердить один Luna relay и один Sol High owner-turn.
6. Подтвердить verified X URL, exact history и durable resolution.
7. Подтвердить исчезновение заявленного ID из очереди.
8. Проверить, что соседний heartbeat заблокирован reservation.
9. Только после полного цикла удалить старую paused automation через
   официальный API.
10. Перезапустить Codex Desktop один раз и снять базовый RSS без legacy helper.

Источники:

- [OpenAI Codex app-server](https://learn.chatgpt.com/docs/app-server)
- [OpenAI Scheduled tasks](https://learn.chatgpt.com/docs/automations)
- [OpenAI project config](https://learn.chatgpt.com/docs/config-file/config-advanced#project-config-files-codexconfigtoml)
- [Apple Activity Monitor](https://support.apple.com/en-au/guide/activity-monitor/-actmntr1004/mac)
- [Codex issue 12491](https://github.com/openai/codex/issues/12491)
- [Codex issue 11324](https://github.com/openai/codex/issues/11324)
