# Автопилот ответов X

[Русский](README.ru.md) | [English](README.md)

Этот репозиторий связывает официальный X API, локальные скрипты и существующую
задачу Codex с авторизованным встроенным Browser. Система обнаруживает новые
прямые ответы и продолжения любых сохраненных диалогов, не зависит от
индикатора непрочитанных уведомлений X, защищается от дублей и передает сильной
модели только реальные события.

## Как это устроено

1. Python watcher каждую минуту читает официальный endpoint упоминаний X.
2. `since_id` и SQLite не дают одному статусу попасть в очередь дважды.
3. `wake-request.json` содержит неразрешенные прямые ответы и продолжения любых
   диалогов, где сохранен ход Alex.
4. Локальный dispatcher выдает новым событиям 30-минутную аренду.
5. Запланированная задача Codex Desktop раз в пять минут завершает пустой run
   либо сама становится Browser owner для непустого claim.
6. Sol High открывает живую ветку X, анализирует текст и изображения, проверяет
   первичные источники, историю диалога и отсутствие дубля.
7. Короткий ответ пишет Sol High. Длинный follow-up может продолжаться только в
   точной исторической сессии кастомного GPT.
8. После публикации сохраняются точный URL, текст, источники и durable
   disposition. Только после этого событие исчезает из очереди.

При `mandatory_response_mode=true` каждое доступное eligible-событие в
заданном временном окне получает ровно один ответ. Поддержка, сарказм, шутка,
оскорбление, мем или реакция без тезиса не являются причинами для `skip`.
`skip` разрешен только при доказанном прямом ответе Alex именно на это событие.

Watcher и dispatcher сами никогда ничего не публикуют. Авторизованным Browser
управляет только одна задача Sol High.

## Зачем такое разделение

- Получение и дедупликация X выполняются Python без токенов модели.
- Пустой Sol High run завершается до Browser и содержательной работы.
- Ручное открытие уведомлений X не ломает обнаружение, потому что watcher
  использует ID статусов API, а не синий индикатор интерфейса.
- Аренда dispatcher предотвращает повторные пробуждения, пока идет обработка
  или генерация Pro.
- Если задача упала, событие снова станет доступно после окончания аренды.
- SQLite, ledger и история ветки защищают от повторной публикации.
- Каждый непустой scheduled run является единственным Browser owner своего
  batch. Аренда не позволяет параллельному run получить тот же claim.

## Быстрый старт

Требуются macOS, Python 3.9 или новее, Codex Desktop с встроенным Browser,
X Developer App и Bearer Token с доступом к user mentions.

```bash
cp config.example.json config.json
```

Укажите в `config.json` числовой X user ID. Токен не записывайте в файл.
Сохраните его в macOS Keychain с помощью bundled helper:

```bash
mkdir -p var
xcrun swiftc -framework Security \
  scripts/keychain_helper.swift \
  -o var/keychain-helper

printf '%s' "$X_BEARER_TOKEN" | \
  var/keychain-helper set axrbarsic-x-mention-watcher axrbarsic
```

Проверьте готовность и тесты:

```bash
python3 xmention_watcher.py --config config.json preflight
python3 -m unittest discover -s tests -v
```

Сделайте первый poll вручную и проверьте очередь:

```bash
python3 xmention_watcher.py --config config.json poll
python3 xmention_watcher.py --config config.json status
```

После этого установите два LaunchAgent и создайте запланированную задачу Codex
Desktop на Sol High по
[русскому руководству автопилота](docs/autopilot-setup.ru.md) и
[deployment checklist](docs/deployment-checklist.md).

## Важные контракты

- Один Browser owner, одна вкладка X, одна вкладка ChatGPT.
- На iMac с 8 GB памяти одновременно работает максимум одна Pro generation.
- Публикационные решения принимает только Sol High.
- Новая Pro-цель начинает новую conversation.
- Follow-up к Pro продолжает точную историческую conversation.
- В custom GPT отправляется только screenshot цели и 0 символов текста.
- Ответ custom GPT переносится без смыслового редактирования.
- Разрешенная длина X ответа: от 1 до 4000 Unicode code points.
- Перед публикацией проверяются дубль, parent status, composer и фактический URL.
- Media-only ответ классифицируется относительно точного parent, а незнакомый
  мем проверяется по интернет-источникам.
- `blocked` не подменяется `skip`, если обязательная историческая сессия или
  другой контрактный ресурс недоступен.
- Terminal `blocked` требует точного `blocker_code`. Временная ошибка Browser,
  Pro, rate limit или валидации остается в очереди для повтора.
- На оскорбление публикуется спокойный умный ответ без встречного оскорбления.
  Экспериментальный media-маршрут может использовать одного бота `377` или
  `Ложкин`, но картинка высмеивает аргумент, а не внешность или достоинство.
- Перед ответом Sol получает компактную межветочную память автора по стабильному
  X user ID: точные публичные реплики, даты, URL и наши точные ответы.
  `commenter-history` позволяет поднять более глубокую историю без ограничения
  по давности. Прежнюю цитату можно использовать для доказуемого противоречия,
  смены критерия или повторения уже разобранного тезиса, но не для домыслов о
  личности, чувствительных характеристиках или навязчивого преследования.

```bash
python3 xmention_watcher.py --config config.json commenter-history EVENT_ID \
  --limit 50
```

После включения строгого режима можно без списка event ID вернуть недавние
content-based skips в обычную очередь:

```bash
python3 xmention_watcher.py --config config.json mandatory-response-requeue \
  --hours 12 --as-of 2026-07-25T20:00:00Z --dry-run
python3 xmention_watcher.py --config config.json mandatory-response-requeue \
  --hours 12 --as-of 2026-07-25T20:00:00Z
```

## Состояние и восстановление

```bash
python3 xmention_watcher.py --config config.json status
python3 scripts/autopilot_dispatch.py \
  --config config.json \
  --lease-seconds 1800 \
  status

python3 scripts/autopilot_dispatch.py \
  --config config.json \
  snapshot

python3 scripts/autopilot_bridge.py \
  --config config.json \
  --lease-seconds 1800 \
  claim
```

Runtime-файлы, SQLite, токены, Browser cookies и локальные журналы исключены из
Git. В репозитории хранятся только код, тесты, документация, шаблоны и backup
skill. Историю диалогов можно экспортировать отдельно в приватный backup:

```bash
python3 xmention_watcher.py --config config.json history-export \
  --output history-backup/conversation-history.jsonl
```

Подробная архитектура находится в [docs/architecture.md](docs/architecture.md).
