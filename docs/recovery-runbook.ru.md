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
- главную Sol Max сессию и Luna Low relay;
- архивный флаг, модель, минимальный effort и рабочий каталог ролей;
- официальный heartbeat `x-relay`, самостоятельный cron `x-15` и paused
  старые automations;
- пять LaunchAgent;
- SQLite integrity, очередь и Keychain helper;
- установленные `x-twitter-operator`, обе версии
  `poyasnitelnaya-brigada` и `377` с Git-копиями;
- матрицу характера и runtime overrides.

## Офлайн-эмулятор

Цифровой двойник проверяет конвейер без X API, Browser, модели и секретов:

```bash
python3 scripts/autopilot_emulator.py \
  --seed 20260727 \
  --traces 10000 \
  --steps 100 \
  --report var/emulator/latest.json
```

Первая фаза исчерпывающе перебирает все допустимые комбинации конечной модели:
poll, источник события, маршрут ответа, mandatory mode, автор, resource guard,
состояние owner, lease, reservation, Browser outcome и durable write.
Вероятностная масса считается точно для равномерного синтетического
распределения по достижимым комбинациям. Это не оценка реальной частоты аварий.

Вторая фаза генерирует длинные последовательности с отказами, гонками,
протуханием владельца, повторами, задержками durable sync и восстановлением.
Каждый контрпример воспроизводится по `seed`. Эмулятор доказывает свойства
только внутри конечной абстракции, поэтому не заменяет blind live canary.

## Автоматический supervisor

`scripts/autopilot_supervisor.py` запускается вместо старого пассивного
watchdog тем же минутным LaunchAgent. Это обычный Python без модели, Browser и
токенов. В зеленом состоянии он только обновляет
`var/autopilot-supervisor.json` и никого не будит.

Одноразовая проверка:

```bash
python3 scripts/autopilot_supervisor.py \
  --config config.json \
  --contract recovery/system-contract.json \
  run
```

Supervisor применяет только заранее разрешенный ремонт. Для stale или failing
poll это один `launchctl kickstart`. Биллинговый `HTTP 402` является отдельным
внешним состоянием: supervisor выгружает poll LaunchAgent, фиксирует
`external_action_required`, не будит модель повторно и не блокирует обработку
уже накопленной X очереди. После пополнения credits poll нужно штатно загрузить
и подтвердить одним успешным live poll. Для остальных сбоев после cooldown
он повторяет read-only диагностику. Если поломка осталась, либо
проверка относится к SQLite, очереди, Codex role, automation, skill или другому
неоднозначному ресурсу, создается один durable incident.

Dispatcher видит incident без модели, поднимает Codex Desktop штатным путем, а
существующая Luna relay-сессия передает его выделенному Sol Max service worker.
Одинаковый fingerprint не создает повторные incident каждую минуту.
Reservation, claim token, started, completed и failed защищают ремонт от двух
одновременных владельцев. `completed` для настоящей аварии принимается только
после повторного doctor с нулем FAIL и обязательного текстового отчета.
Протухшие reservation и owner lease автоматически возвращают тот же incident
в `escalation_pending`. Изменение набора FAIL во время активного ремонта
обновляет диагностику, но не заменяет owner и не сбрасывает его claim token.

Текущее состояние:

```bash
python3 scripts/autopilot_supervisor.py --config config.json status
```

Ручной repair-canary разрешен только когда нет открытой аварии:

```bash
python3 scripts/autopilot_supervisor.py --config config.json canary
```

Canary проверяет реальный dispatcher, Luna handoff и Sol claim, но не считается
проверкой X watcher. Финальный X canary всегда слепой: watcher сам обнаруживает
органический неотвеченный event, без передачи модели ID, ссылки, ручного
добавления в очередь или заранее созданного resolution.

### Приемочный прогон 27 июля 2026 года

- Token-free self-repair обнаружил искусственно состаренный poll health,
  выполнил ровно один allowlisted kickstart и сам закрыл incident после
  восстановления poll.
- Repair handoff прошел через production dispatcher, Luna relay, claim,
  started, doctor с нулем FAIL и completed с обязательным отчетом.
- Отдельный production canary намеренно протухшего owner lease вернул тот же
  incident в `escalation_pending`, выдал новый claim и завершился при FAIL 0.
- Blind X-canary естественно обнаружил два органических ответа:
  `2081816608046285048` и `2081813598079213621`.
- Sol High опубликовал ровно по одному прямому ответу:
  [2081821580779286645](https://x.com/axrbarsic/status/2081821580779286645)
  и
  [2081821803262156955](https://x.com/axrbarsic/status/2081821803262156955).
- Один штатный `browser-handoff-sync` импортировал четыре точных хода,
  создал self-audited manifest, разрешил оба event и оставил pending queue
  равной нулю.
- В эксперименте не использовались ручное добавление event ID, готовый ответ,
  прямой `resolve`, дополнительный poll или второй Browser owner.

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
ослабленную защиту. Выделенный service worker нужно разархивировать через
официальные инструменты Codex и сверить с versioned contract, а не редактировать
state SQLite вручную. Закрепление worker не требуется: в боковой панели должна
оставаться одна основная пользовательская задача «Автопилот».

### 4. Восстановить deployment points

- `~/.codex/skills/x-twitter-operator` восстановить только из
  `skill-backup/x-twitter-operator`;
- `~/.codex/skills/poyasnitelnaya-brigada` восстановить только из
  `skill-backup/poyasnitelnaya-brigada`;
- `~/.codex/skills/poyasnitelnaya-brigada-v2` восстановить только из
  `skill-backup/poyasnitelnaya-brigada-v2`;
- `~/.codex/skills/377` восстановить только из `skill-backup/377`;
- LaunchAgent перерендерить из `macos/*.plist.example` через
  `scripts/render_launchd.py`;
- heartbeat `x-relay` и cron `x-15` восстановить только через официальный
  `automation_update`. Для `x-15` обязательны local execution,
  `gpt-5.6-sol`, effort `max`, 15-минутный интервал и точный prompt из
  `macos/x-15.prompt.txt`;
- подписанный app-like Keychain helper восстановить командой
  `scripts/install_keychain_helper.sh`. Для первого выпуска нужен Apple Account
  в Xcode и Mac provisioning profile. При миграции старого login-keychain item
  один раз подтвердить системный запрос доступа macOS;
- секрет X вернуть только через установленный helper в Data Protection
  Keychain с `AfterFirstUnlockThisDeviceOnly`;
- live SQLite вернуть из последнего валидного snapshot только после сохранения
  копии повреждённого файла.

При изменении существующего `x-15` сначала поставить его на паузу официальным
инструментом, дождаться завершения всех уже запущенных задач этой automation и
проверить `owner=null` через `outbound_cycle.py status`. Обновлять prompt поверх
активного run запрещено: старый Browser owner еще может публиковать, когда
первый run нового контракта уже получил lease.

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

Состояние outbound catch-up проверяется отдельно:

```bash
python3 scripts/outbound_cycle.py \
  --state var/outbound-cycle.json \
  --lease-seconds 1800 status
```

Файл находится в `var/` и не восстанавливается из Git. При доказанном пропуске
окна добавляется одна идемпотентная корректировка с уникальным ID, точным
числом, причиной и временными границами. Нельзя вручную уменьшать счетчик или
засчитывать неподтвержденную публикацию. Обычный run закрывает текущий слот,
а только вторая durable публикация одного run уменьшает catch-up на единицу.

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
