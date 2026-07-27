# Аварийное восстановление Пояснительной бригады

## Назначение

GitHub хранит воспроизводимый каркас системы. Локальные секреты, live SQLite,
очередь, Browser evidence и оперативные настройки характера в Git не попадают.
Поэтому восстановление разделено на две части:

1. вернуть код и авторитетный системный контракт из Git;
2. проверить и аккуратно подключить локальное состояние, не создавая пустую
   базу поверх потерянной и не создавая второго Browser owner.

Авторитетный машинный контракт находится в
`recovery/system-contract.json`. Read-only проверка:

```bash
cd /Users/alexlane/Developer/x-mention-watcher
python3 scripts/system_doctor.py
```

JSON для автоматического разбора:

```bash
python3 scripts/system_doctor.py --json
```

Doctor ничего не архивирует, не публикует, не запускает Browser, не меняет
automation, LaunchAgent, SQLite или Keychain. Он сверяет:

- канонический каталог и Git origin;
- обязательные файлы каркаса;
- главную Sol High сессию и Luna Low relay;
- архивный флаг, модель, effort и рабочий каталог ролей;
- официальный heartbeat `x-relay` и paused старый dispatcher;
- пять LaunchAgent;
- SQLite integrity, очередь и Keychain helper;
- установленный `x-twitter-operator` с Git-копией;
- матрицу характера и runtime overrides.

## Порядок восстановления

### 1. Сохранить живое состояние

Не удалять `var/`, `config.json`, Keychain, архив X и Codex state. Не создавать
новый Browser owner только потому, что нужная сессия пропала из списка.
Сначала найти её по точному ID из системного контракта.

### 2. Вернуть каркас

Проверить origin и получить изменения штатным Git workflow. Если рабочая копия
dirty, сначала сохранить и классифицировать изменения. Нельзя применять
`git reset --hard`, `git clean` или массовый checkout.

### 3. Запустить doctor

Исправлять FAIL сверху вниз. WARN не означает потерю данных, но указывает на
ослабленную защиту. Главную сессию нужно разархивировать и закрепить через
официальные инструменты Codex, а не прямым редактированием state SQLite.

### 4. Восстановить deployment points

- `~/.codex/skills/x-twitter-operator` восстановить только из
  `skill-backup/x-twitter-operator`;
- LaunchAgent перерендерить из `macos/*.plist.example` через
  `scripts/render_launchd.py`;
- heartbeat восстановить только через официальный `automation_update`;
- секрет X вернуть только в macOS Keychain;
- live SQLite вернуть из последнего валидного snapshot только после сохранения
  копии повреждённого файла.

### 5. Повторить безопасные проверки

Doctor должен показать ноль FAIL. Затем:

```bash
python3 xmention_watcher.py --config config.json preflight
python3 scripts/autopilot_bridge.py \
  --config config.json \
  --lease-seconds 1800 \
  gate
```

Если `dispatch=false`, Browser не открывается. Если `dispatch=true`, обработка
идёт через единственный существующий Browser owner.

## Управляемый характер

База хранится в `personality/policy.json`. Она содержит общий голос и
проверенные профили тем. Быстрая локальная поправка:

```bash
python3 scripts/personality_policy.py set \
  --scope global \
  --instruction "Пиши жёстче и короче, но не теряй точность."
```

Для одной X-ветки:

```bash
python3 scripts/personality_policy.py set \
  --scope conversation \
  --selector CONVERSATION_ID \
  --instruction "Говори от первого лица и подробно показывай устройство системы."
```

Для темы используется `--scope topic --selector PROFILE_ID`. Посмотреть
разрешившийся профиль:

```bash
python3 scripts/personality_policy.py resolve \
  --text "Обсуждение протона и Вселенной" \
  --conversation-id CONVERSATION_ID \
  --author USERNAME \
  --json
```

Runtime overrides лежат в `var/` и не попадают в Git. Удачное правило сначала
обкатывается реальной публикацией, затем переносится в tracked policy отдельным
проверяемым изменением.

## Canary заблокированного экрана

27 июля 2026 года canary прошёл при физически заблокированном экране iMac.
Встроенный Codex Browser:

1. открыл X в авторизованной сессии `@axrbarsic`;
2. нашёл GitHub-публикацию и прочитал живую ветку;
3. опубликовал ответ ровно на 4000 символов;
4. проверил канонический URL публикации;
5. сделал репост нового ответа;
6. закрыл единственную task-owned вкладку.

Проверенная публикация:
`https://x.com/axrbarsic/status/2081655223475446100`.

Computer Use и пиксельное управление macOS не применялись. Это доказывает, что
блокировка экрана не мешает встроенному Browser backend на текущей версии
Codex Desktop. После обновления Desktop такой canary нужно повторить.
