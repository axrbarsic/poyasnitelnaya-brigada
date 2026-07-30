# Локальный skill «Пояснительная бригада»

## Зачем он нужен

Раньше длинный маршрут зависел от страницы custom GPT в ChatGPT: Browser
открывал conversation, передавал цель, ждал генерацию и переносил ответ в X.
Это создавало лишнюю вкладку, расход памяти, ожидание интерфейса и отдельную
точку отказа.

Теперь prompt хранится в Git как skill:

- источник: `skill-backup/poyasnitelnaya-brigada`;
- установленная копия: `~/.codex/skills/poyasnitelnaya-brigada`;
- модель: только `gpt-5.6-sol`;
- reasoning effort: только `max`;
- ChatGPT и custom GPT: не используются для генерации.

## Вход

Browser owner восстанавливает точный target и релевантную цепочку из X, SQLite
и append-only JSONL. В skill передаются автор, status ID, canonical URL, точный
текст, parent, media meaning, прежние turns и проверенные первичные источники.
Публичный контент считается данными, а не инструкциями.

## Выход

Skill возвращает только прямой ответ автору:

- один целостный русский монолог;
- ровно 4000 Unicode code points;
- без U+2013, U+2014, NBSP, zero-width и внутренних citation markers;
- с холодным фактчеком и прямыми URL источников;
- с жестким разбором тезиса, но без угроз и атак на защищенные признаки.

Точная проверка:

```bash
python3 skill-backup/x-twitter-operator/scripts/validate_reply.py \
  --file var/evidence/browser-owner/SESSION/reply.txt \
  --strip-one-final-newline \
  --exact 4000
```

Фактический X composer должен совпадать с validated source byte-for-byte.

## Автономный 15-минутный цикл

Outbound работает как самостоятельная local cron automation `x-15` на
`gpt-5.6-sol` с effort `max`. Старый heartbeat `x-pro-15` поставлен на паузу.
Heartbeat, привязанный к занятой основной задаче, может накопить пробуждения,
но не выполнить их параллельно. Самостоятельный cron получает отдельный run и
поэтому не зависит от длительности текущего диалога Alex с owner-сессией.

Каждый run сначала проверяет входящую очередь и затем получает атомарную аренду
через `scripts/outbound_cycle.py`. Обычный run публикует не более одной
проверенной цели. При активном catch-up лимит временно повышается до двух
последовательных целей, без дополнительных X-вкладок. Только вторая
подтвержденная публикация уменьшает catch-up, поэтому качество цели нельзя
заменить механическим заполнением квоты.

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
`owner=null`. Только после этого обновляется prompt, выполняются doctor и
адресный canary, затем cron возвращается в `ACTIVE`. Такой порядок не дает
старому Browser owner пересечься с первым run нового контракта.

## Память и продолжения

После публикации сохраняются exact target turn, exact Alex turn, parent status
ID, reply URL, SHA-256, длина, источники, skill, model, effort и timestamps.
Follow-up продолжает ту же X chain по локальной истории. Старые
`chatgpt_conversation_url` и migration records остаются только историческим
audit trail.

## Восстановление

`system_doctor` сравнивает обе установленные skills с Git-копиями. Команда
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
