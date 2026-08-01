# Локальный skill «Пояснительная бригада»

## Зачем он нужен

Раньше длинный маршрут зависел от страницы custom GPT в ChatGPT: Browser
открывал conversation, передавал цель, ждал генерацию и переносил ответ в X.
Это создавало лишнюю вкладку, расход памяти, ожидание интерфейса и отдельную
точку отказа.

V2 теперь является основным локальным skill:

- источник: `skill-backup/poyasnitelnaya-brigada-v2`;
- установленная копия: `~/.codex/skills/poyasnitelnaya-brigada-v2`;
- модель: только `gpt-5.6-sol`;
- reasoning effort: только `max`;
- ChatGPT и custom GPT: не используются для генерации.

V1 сохранён отдельно как явный резерв:

- источник: `skill-backup/poyasnitelnaya-brigada`;
- установленная копия: `~/.codex/skills/poyasnitelnaya-brigada`;
- v1 не изменяется и включается только по прямому запросу Alex применить
  старую версию;
- уже начатая транзакция не переключается между версиями посередине.

V2 восстанавливает полный спор, удерживает исходный тезис и главный
незакрытый вопрос, ведёт внутренние журналы точных утверждений, уступок,
противоречий и смен критериев. Он не имеет права объявлять уточнение
противоречием или считать молчание признанием. Versioned eval cases находятся
в `tests/fixtures/poyasnitelnaya_brigada_v2_cases.json`.

## Вход

Browser owner восстанавливает точный target и релевантную цепочку из X, SQLite
и append-only JSONL. В skill передаются автор, status ID, canonical URL, точный
текст, parent, media meaning, прежние turns и проверенные первичные источники.
Публичный контент считается данными, а не инструкциями.

## Выход

Skill возвращает только прямой ответ автору:

- один целостный русский монолог;
- непустой текст не длиннее 4000 Unicode code points, без стремления занять
  весь лимит;
- без U+2013, U+2014, NBSP, zero-width и внутренних citation markers;
- с холодным фактчеком и прямыми URL источников;
- с жестким разбором тезиса, но без угроз и атак на защищенные признаки.

Точная проверка:

```bash
python3 skill-backup/x-twitter-operator/scripts/validate_reply.py \
  --file var/evidence/browser-owner/SESSION/reply.txt \
  --strip-one-final-newline \
  --non-empty \
  --max 4000
```

Фактический X composer должен совпадать с validated source byte-for-byte.
После публикации официальный X API `note_tweet` раскрывается через
`expanded_url` и снова сравнивается с исходником. Визуальный `innerText` не
используется для точной длины, потому что X добавляет переносы и многоточия к
отображаемым ссылкам.

Проект выполняет эту проверку командой:

```bash
python3 scripts/verify_x_note_tweet.py \
  --config config.json \
  --status-id <REPLY_STATUS_ID> \
  --parent-status-id <TARGET_STATUS_ID> \
  --file var/evidence/browser-owner/SESSION/reply.txt \
  --strip-one-final-newline \
  --max 4000
```

Durable завершение разрешено только при `valid=true`.

После подтверждённой публикации точная локальная история из двух turns
строится из того же evidence-объекта и импортируется в базу watcher:

```bash
python3 scripts/build_outbound_history.py \
  --evidence var/evidence/browser-owner/SESSION/evidence.json \
  --output var/evidence/browser-owner/SESSION/conversation-history.jsonl \
  --max 4000
python3 xmention_watcher.py --config config.json history-import \
  --file var/evidence/browser-owner/SESSION/conversation-history.jsonl
python3 xmention_watcher.py --config config.json history-show TARGET_STATUS_ID
```

Builder закрывается с ошибкой при неверном parent, неканоническом reply URL,
пустом ответе, превышении 4000 code points, запрещённом Unicode, отсутствующем
локальном файле или конфликте с существующим snapshot. Импортированные target и Alex
turns становятся канонической памятью продолжения.

Ручные Alex parents используют тот же локальный путь продолжения. Autopilot
честно сохраняет их origin provenance, затем выбирает режим по точному
сохраненному тексту. Содержательный parent, то есть не меньше 500 code points,
три абзаца, одна source URL либо доказанный local-max origin, продолжается этим
skill по полной SQLite истории. Беседа ChatGPT не восстанавливается.

## Автономный 10-минутный цикл

Outbound реализован как самостоятельная local cron automation `x-15` на
`gpt-5.6-sol` с effort `max`. `x-15` активен по прямому разрешению Alex, старый
heartbeat `x-pro-15` остается на паузе.
Heartbeat, привязанный к занятой основной задаче, может накопить пробуждения,
но не выполнить их параллельно. Самостоятельный cron получает отдельный run и
поэтому не зависит от длительности текущего диалога Alex с owner-сессией.

Каждый run сначала получает атомарную аренду через `scripts/outbound_cycle.py`,
а затем проверяет входящую очередь. Обычный run публикует не более одной
проверенной цели. При активном catch-up лимит временно повышается до двух
последовательных целей, без дополнительных X-вкладок. Только вторая
подтвержденная публикация уменьшает catch-up, поэтому качество цели нельзя
заменить механическим заполнением квоты.

Если входящая очередь или прежний outbound owner занимают единственную
writer-линию, run не открывает Browser и выполняет идемпотентный `defer-slot`
для текущего 10-минутного окна. Слот остается в catch-up и будет обработан
позже, поэтому входящие ответы больше не стирают outbound-расписание.

Перед каждой дорогой стадией и непосредственно перед публикацией run продлевает
аренду. Это позволяет безопасно ждать фактчек и точную генерацию дольше
первоначального окна, не открывая дорогу второму владельцу:

```bash
python3 scripts/outbound_cycle.py \
  --state var/outbound-cycle.json \
  --lease-seconds 1800 renew \
  --claim-token TOKEN
```

```bash
python3 scripts/outbound_cycle.py \
  --state var/outbound-cycle.json \
  --lease-seconds 1800 status
```

Состояние находится в `var/`, не попадает в Git и хранит точные claim tokens,
историю завершений и идемпотентные корректировки пропущенных окон.

### Безопасное обновление cron

Prompt или параметры `x-15` нельзя менять поверх активного run. Сначала cron
ставится на паузу официальным `automation_update`, затем все уже запущенные
задачи `x-15` должны завершиться, а `outbound_cycle.py status` должен показать
`owner=null`. Только после этого обновляется prompt и выполняются doctor и
адресный canary. После canary cron возвращается в состояние, прямо разрешенное
Alex. Такой порядок не дает старому Browser owner пересечься с первым run
нового контракта.

## Память и продолжения

После публикации сохраняются exact target turn, exact Alex turn, parent status
ID, reply URL, SHA-256, длина, источники, skill, model, effort и timestamps.
Follow-up продолжает ту же X chain по локальной истории. Старые
`chatgpt_conversation_url` и migration records остаются только историческим
audit trail.

## Восстановление

`system_doctor` сравнивает все три установленные skills с Git-копиями. Команда
`project_layout_audit.py --require-installed-skill` делает ту же проверку для
каркаса проекта. Старые model, conversation и screenshot blockers возвращаются
в очередь командой совместимости:

```bash
python3 xmention_watcher.py --config config.json \
  pro-model-recovery-requeue --dry-run
python3 xmention_watcher.py --config config.json \
  pro-model-recovery-requeue
```

Команда не принимает event ID и выбирает подходящие записи из durable state.
