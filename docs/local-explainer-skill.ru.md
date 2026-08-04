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

## Основание аргументативного контракта

V2 не полагается на один «магический промпт». Его структура объединяет
проверенные подходы:

- архитектуру Project Debater с раздельными этапами поиска аргумента,
  доказательства и опровержения:
  <https://www.nature.com/articles/s41586-021-03215-w>;
- argumentation schemes и critical questions для явного бремени
  доказательства: <https://informallogica.ca/index.php/informal_logic/article/view/485>;
- выводы ChangeMyView о необходимости отвечать на уязвимую часть фактической
  аргументации, а не на образ оппонента: <https://aclanthology.org/N18-1010/>;
- структуру публичного развенчания из Debunking Handbook 2020:
  <https://skepticalscience.com/docs/DebunkingHandbook2020.pdf>.
- контекстный retrieval релевантной истории вместо статического профиля:
  <https://aclanthology.org/2026.findings-acl.858/>.

Отсюда следуют внутренние `neutral_question`, `burden_ledger` и
`inference_bridge`. Навязанная бинарность отделяется от скрытого следствия, а
объявление собственной победы требует назвать конкретную ошибку. Наивный
multi-agent debate не используется как источник истины, потому что ошибочный
консенсус способен усиливаться между раундами:
<https://arxiv.org/abs/2509.05396>. Поведение проверяется на versioned replay
cases, а не по впечатлению от одного удачного ответа.

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

### Визуальное дополнение к V2

Если Alex прямо просит усилить полноценный ответ V2 изображениями, owner
использует профиль `local_sol_max_visual`. Самодостаточный текст по-прежнему
создаёт `poyasnitelnaya-brigada-v2`, а локальный `imagegen` создаёт ровно одну
вертикальную инфографику. Вся хронология, противоречия и доказательства
собираются на одном мобильном полотне вместо серии карточек. Допустима
смысловая плотность примерно в 4-5 раз выше одной прежней карточки, но иерархия
и основные подписи должны читаться на телефоне. Визуал не заменяет факты,
источники или сам ответ.

Durable evidence сохраняет один объект `generation.media_files` с локальным
файлом, SHA-256, MIME и официальным `published_media_key`. Перед кликом
проверяется `composer_attachment_count=1`. После публикации официальный X API
обязан вернуть то же единственное вложение типа `photo`. Любое расхождение
оставляет событие незавершённым и запрещает повторный клик без новой проверки
живой ветки. Исторические evidence с четырьмя карточками остаются валидными.

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

Standalone cron `x-15` постоянно находится на паузе. Действующий минутный
heartbeat `x-relay` после repair и inbound-маршрутизации может атомарно получить
одну outbound-попытку для текущего 10-минутного окна. Это происходит только
при полностью пустой входящей очереди и свободных writer leases.

Outbound owner работает на `gpt-5.6-sol` с effort `max`, использует одну
X-вкладку и обрабатывает не более одной цели. Он повторяет inbound gate после
выбора кандидата, после локальной генерации и прямо перед публикацией. Даже одно
входящее событие вызывает `pause-slot` без клика публикации. Освободившийся
цикл можно продолжить после очистки очереди, но пропущенные 10-минутные окна не
создают catch-up и никогда не догоняются позже.

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
slot ID и историю terminal outcomes. Нормальный `catchup_remaining` равен нулю.

### Безопасное обновление scheduler

`x-15` сохраняется как paused deployment marker. Рабочий prompt relay меняется
только официальным `automation_update`. Перед обновлением нужно дождаться
`owner=null` в `outbound_cycle.py status`, затем выполнить doctor и адресный
canary без публикации. Live contract обязан ожидать `x-15` в состоянии PAUSED.

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
