# Контракт хранения и резервного копирования

## Один канонический корень

Канонический корень всей системы:

`/Users/alexlane/Developer/x-mention-watcher`

Git хранит исходный код, тесты, документацию и восстанавливаемую копию skill.
Git не хранит живые базы, официальные ZIP-архивы X, credentials, runtime locks
и сгенерированные evidence. Все изменяемые данные находятся внутри `var/`:

```text
x-mention-watcher/
  var/
    watcher.sqlite3
    evidence/
      browser-owner/<session-id>/
    snapshots/
      <utc-timestamp>/
        watcher.sqlite3
        conversation-history.jsonl
        initial-audit-resolutions.jsonl
        manifest.json
    backup-state/
```

Полная карта проекта и обязательных внешних deployment points описана в
[project-layout.ru.md](project-layout.ru.md).

Активный skill остаётся установленным в
`~/.codex/skills/x-twitter-operator`, потому что Codex ищет skills именно там.
Его точная восстанавливаемая версия находится в Git под
`skill-backup/x-twitter-operator`. Установленный путь считается deployment
state, а не второй базой данных.

## Правило SQLite

Нельзя синхронизировать или загружать живой `watcher.sqlite3`, его WAL или SHM
во время работы watcher. Обычная файловая копия может зафиксировать
несогласованный набор файлов. Нужно создавать транзакционно согласованный
snapshot через SQLite backup API:

```bash
python3 memory_snapshot.py --config config.json
```

Команда отказывается сохранять повреждённую память, проверяет SQLite integrity
и foreign keys, запускает полный memory audit, экспортирует историю и
resolutions в человекочитаемом виде, вычисляет SHA-256 каждого файла и атомарно
публикует каталог с manifest внутри `var/snapshots/`.

## Официальные архивы X

Официальные ZIP-архивы не входят в каталог проекта. Они хранятся неизменно в
отдельном защищённом archive vault, например:

`/Users/alexlane/Archives/x-mention-watcher/<request-date>/source.zip`

Рядом в `reports/` лежат `intake-plan.json` и `intake-receipt.json`. Для
обычного импорта ZIP не распаковывается. `archive_intake.py` сначала выполняет
dry-run, фиксирует SHA-256 ZIP и запрещает apply при любом изменении источника.
Перед append-only импортом создаётся согласованный snapshot памяти. После
импорта обязательны полный archive audit и второй snapshot. Импортёр читает
только публичные account и tweet members и игнорирует direct-message members.

```bash
python3 archive_intake.py \
  --config config.json \
  --archive /Users/alexlane/Archives/x-mention-watcher/YYYY-MM-DD/source.zip
python3 archive_intake.py \
  --config config.json \
  --archive /Users/alexlane/Archives/x-mention-watcher/YYYY-MM-DD/source.zip \
  --apply
```

## Онлайн backup

Рекомендуемая независимая цель: зашифрованный repository restic в приватном
bucket Backblaze B2 через S3-compatible API.

- restic защищает repository собственным паролем и поддерживает несколько
  ключей доступа;
- документация restic рекомендует для B2 именно S3-compatible API, а не старый
  native B2 backend;
- Backblaze B2 не зависит от Apple Account и сейчас стоит от 6.95 USD за TB в
  месяц, с оплатой по фактическому объёму и без минимального срока хранения;
- B2 application key и рабочая копия пароля restic хранятся в macOS Keychain;
- вторая копия restic password или recovery record хранится вне Apple,
  например в независимом password manager либо в запечатанной офлайн-копии.
  Потеря всех ключей restic делает repository невосстановимым.

В backup входят только:

- завершённые каталоги `var/snapshots/`;
- неизменные исходники и reports из отдельного archive vault;
- канонические raw evidence из `var/evidence/`.

Locks, health, wake, caches, live WAL/SHM и временные snapshot-каталоги не
копируются.

Рекомендуемое хранение после подключения remote:

- почасовые snapshots за 48 часов;
- ежедневные за 30 дней;
- еженедельные за 12 недель;
- ежемесячные за 24 месяца.

Раз в неделю выполняется `restic check`. Раз в месяц выполняются
`restic check --read-data` и настоящий пробный restore. Restore считается
успешным, только если восстановленная SQLite проходит:

```bash
python3 xmention_watcher.py --config restored-config.json \
  memory-audit --require-archive
```

Нельзя включать default Object Lock для живого restic repository без отдельной
проверки: защита lock и pack objects может конфликтовать с очисткой restic.
Если нужна неизменяемость, используется отдельный bucket или prefix для
периодических экспортных snapshot bundles.

### Backup-оркестратор

`restic_backup.py` обеспечивает контракт хранения, а не передаёт restic весь
каталог проекта. Он отклоняет повреждённые и незавершённые snapshots, проверяет
каждое manifested Browser evidence tree, исключает живую комбинацию
SQLite/WAL/SHM, запрещает слишком широкие archive-vault paths и передаёт
credentials только через окружение дочернего процесса. Для iMac с 8 ГБ памяти
restic ограничен двумя S3-соединениями и двумя Go scheduler threads.

Скопируйте `backup.example.json` в игнорируемый `backup.json`, затем укажите
регион bucket B2 и безопасное имя Keychain service. Пока официальный архив не
получен, `archive_vault` должен быть равен `null`. После получения указывается
точный внешний путь. Настроенный отсутствующий или пустой vault останавливает
backup. Под этим service сохраняются четыре generic-password items:

| Account | Значение |
| --- | --- |
| `repository` | `s3:https://s3.<region>.backblazeb2.com/<bucket>/<dedicated-prefix>` |
| `restic-password` | независимый пароль restic repository |
| `aws-access-key-id` | ID ограниченного bucket B2 application key |
| `aws-secret-access-key` | ограниченный bucket B2 application key |

Значения не должны попадать в JSON, Git, shell history или аргументы команд.
Для сохранения используется bundled Keychain helper, значение передаётся через
standard input.

До первой remote-записи:

```bash
python3 restic_backup.py plan
python3 restic_backup.py preflight
```

После создания и проверки существующего private B2 bucket его restic prefix
инициализируется один раз:

```bash
python3 restic_backup.py init --confirm-existing-private-bucket
```

Регулярные команды:

```bash
python3 restic_backup.py backup
python3 restic_backup.py check
python3 restic_backup.py retention
python3 restic_backup.py retention --apply
python3 restic_backup.py check --read-data
python3 restic_backup.py restore-smoke
```

`retention` выполняет только dry run без `--apply`. Каждый успешный backup
сохраняет точный restic snapshot ID и fingerprinted receipt источников.
`restore-smoke` восстанавливает только этот snapshot, сверяет каждый источник
с receipt, заранее проверяет свободное место с запасом, записывает результат
без секретов в `var/backup-state/latest.json` и в долговечную квитанцию
`var/backup-state/last-successful-restore-smoke.json`, затем удаляет временное
восстановление после успешной или неуспешной проверки.

Итоговая готовность проверяется одной командой:

```bash
python3 readiness_audit.py
```

Полный контракт компонентов и машинных blocker-кодов описан в
[readiness-audit.ru.md](readiness-audit.ru.md).

## Официальные источники

- [Настройка repository restic и рекомендации для B2](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)
- [Backup и exclusions restic](https://restic.readthedocs.io/en/stable/040_backup.html)
- [Проверка repository restic](https://restic.readthedocs.io/en/stable/077_troubleshooting.html)
- [Цена Backblaze B2](https://www.backblaze.com/cloud-storage/pricing)
- [Backblaze B2 Object Lock](https://www.backblaze.com/docs/cloud-storage-object-lock)
- [Backblaze B2 lifecycle rules](https://www.backblaze.com/docs/en/cloud-storage-lifecycle-rules)
