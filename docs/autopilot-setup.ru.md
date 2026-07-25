# Настройка автопилота Codex

[Русский](autopilot-setup.ru.md) | [English](autopilot-setup.md)

## Назначение

Автопилот пробуждает существующую задачу Codex с авторизованным Browser только
тогда, когда в надежной очереди X есть событие. Watcher, dispatcher и автоматика
Luna не пишут и не публикуют ответы. Весь контекст и все мутации остаются в
задаче Sol High.

## Компоненты

| Компонент | Частота | Модель | Ответственность |
| --- | --- | --- | --- |
| X watcher LaunchAgent | 5 минут | нет | Получение, дедупликация, SQLite, очередь |
| Watchdog LaunchAgent | 1 минута | нет | Контроль зависания и повторных ошибок |
| Автоматика Codex | 5 минут | Luna Low | Snapshot, persistent lease, одно пробуждение |
| Существующая Browser-owner задача | по событию | Sol High | Контекст, факты, текст, публикация |
| Кастомный GPT | только Pro | настроенный Pro | Длинный ответ в точной conversation |

## Настройка dispatcher

Добавьте необязательный путь в `config.json`:

```json
{
  "autopilot_state_file": "var/autopilot-dispatch.json"
}
```

Если projectless-автоматика из-за sandbox не может писать в репозиторий, не
переносите state в `/tmp`. Используйте read-only команду `snapshot`, а аренду
храните в persistent `memory.md` автоматики. Это официальный writable path,
который Codex передает каждому scheduled run.

Проверьте состояние:

```bash
python3 scripts/autopilot_dispatch.py \
  --config config.json \
  --lease-seconds 1800 \
  status
```

Не коммитьте `config.json`, `var/`, токены, cookies и runtime-state.

## Создание автоматики Codex

Создайте одну активную автоматику с периодом пять минут:

- модель: `gpt-5.6-luna`;
- reasoning: Low;
- окружение: local;
- уведомления: только при ошибке;
- адресат: существующая закрепленная Browser-owner задача.

Промпт projectless-автоматики должен:

1. Выполнить одну read-only команду `snapshot`.
2. Сравнить pending IDs с `leased_event_ids` в persistent memory.
3. Удалить resolved IDs и считать аренду истекшей через 30 минут.
4. Тихо завершиться, если eligible IDs нет.
5. Записать eligible IDs до доставки.
6. Отправить одно сообщение в точную существующую задачу.
7. Запустить целевой turn на `gpt-5.6-sol`, High.
8. Самому никогда не открывать Browser, X или ChatGPT.
9. Удалить новые leased IDs, если доставка не удалась.
10. Никогда не создавать новую задачу для каждого события.

Замените локальные пути и ID задачи Codex значениями своей машины. Не
публикуйте эти machine-specific значения в открытом репозитории.

## Контракт сообщения для Sol High

Wake message должен требовать:

- открыть точные URL из claim;
- прочитать полную живую ветку и все media;
- проверить факты по первичным источникам;
- проверить дубль по X и ledger;
- выбрать short, Pro или skip;
- для Pro follow-up открыть точную историческую custom GPT conversation;
- проверить composer и Unicode до одного клика публикации;
- проверить живой URL опубликованного ответа;
- сохранить append-only историю и durable resolution;
- сделать один финальный poll;
- на Mac с 8 GB держать одну вкладку X, одну ChatGPT и одну Pro generation.

Владелец аккаунта должен явно дать постоянное разрешение на автопилот. Оно
ограничивается прямыми ответами. Лайки, репосты, подписки, личные сообщения,
новые исходные посты и удаления требуют отдельного разрешения.

## Поведение при сбоях

- Ошибка API не двигает курсор X.
- Поврежденный wake JSON останавливает dispatcher до изменения state.
- File lock сериализует прямой script claim.
- Persistent automation memory сериализует projectless scheduled lease.
- Аренда 30 минут подавляет повторные пробуждения.
- Ошибка доставки сразу освобождает claim.
- Если задача Sol упала, событие останется unresolved и снова станет доступно
  после окончания аренды.
- Перед каждой публикацией Sol все равно проверяет живой X и ledger.

## Проверка

```bash
python3 -m unittest discover -s tests -v
python3 scripts/autopilot_dispatch.py \
  --config config.json \
  --lease-seconds 1800 \
  status
```

Затем используйте один реальный прямой ответ как canary:

1. Watcher создает ровно одно событие.
2. Автоматика создает ровно один claim.
3. Повторный запуск внутри аренды не отправляет второе пробуждение.
4. Sol публикует ответ или делает durable skip.
5. Очередь становится пустой.
6. Dispatcher удаляет разрешенный ID из своего state.
