# Настройка автопилота Codex

[Русский](autopilot-setup.ru.md) | [English](autopilot-setup.md)

## Назначение

Автопилот запускает Codex только тогда, когда в надежной очереди X есть
eligible событие. Пустые проверки watcher и dispatcher не используют модель.
Весь контекст и все мутации остаются у Sol High.

## Компоненты

| Компонент | Частота | Модель | Ответственность |
| --- | --- | --- | --- |
| X watcher LaunchAgent | 5 минут | нет | Получение, дедупликация, SQLite, очередь |
| Watchdog LaunchAgent | 1 минута | нет | Контроль зависания и повторных ошибок |
| Autopilot LaunchAgent | изменение очереди, fallback 1 минута | нет при пустой очереди | Lease и условный запуск Codex |
| Легкая Browser-owner задача | по событию | Sol High | Контекст, факты, текст, публикация |
| Кастомный GPT | только Pro | настроенный Pro | Длинный ответ в точной conversation |

## Настройка dispatcher

Добавьте локальные значения в `config.json`:

```json
{
  "autopilot_state_file": "var/autopilot-dispatch.json",
  "autopilot_health_file": "var/autopilot-health.json",
  "autopilot_last_message_file": "var/autopilot-last-message.txt",
  "autopilot_process_lock": "var/autopilot-resume.lock",
  "autopilot_owner_rotation_after_runs": 20,
  "browser_owner_thread_id": "REPLACE_WITH_CODEX_TASK_UUID",
  "browser_owner_cwd": "/absolute/path/to/browser-owner-workspace",
  "codex_cli_path": "~/.local/bin/codex"
}
```

Храните runtime state в игнорируемом `var/`, а не в `/tmp`, чтобы lease
переживал обычные перезапуски.

Проверьте состояние:

```bash
python3 scripts/autopilot_dispatch.py \
  --config config.json \
  --lease-seconds 1800 \
  status
```

Не коммитьте `config.json`, `var/`, токены, cookies и runtime-state.

## Инициализация Browser owner

Переиспользуйте или создайте одну легкую задачу Codex и оставьте ее
неархивированной. Один раз выполните в ней через Codex Desktop read-only IAB
preflight и подтвердите нужный X аккаунт. Этот app turn создает Browser
eligibility.

Локальный launcher затем использует официальный CLI:

```bash
codex exec resume --ephemeral TASK_UUID \
  -m gpt-5.6-sol \
  -c 'model_reasoning_effort="high"' \
  --skip-git-repo-check -
```

`--ephemeral` продолжает IAB-eligible задачу и не создает новую задачу в
sidebar. Точная история X берется из SQLite, append-only ledger и сохраненных
ChatGPT conversation URL.

Codex CLI 0.146 все равно показывает resumed ephemeral turns внутри owner
задачи. Поэтому она должна быть выделенной и маленькой. Следите за суммарной
историей и заранее ротируйте на другой маленький preflight-verified owner. Не
направляйте launcher в огромную общую X conversation.

`autopilot-health.json` хранит `completed_runs` и выставляет
`rotation_recommended` на заданном пороге. Замена owner остается осознанной
операцией, потому что новая задача сначала должна пройти app IAB preflight.

Нельзя использовать голый `codex exec --ephemeral`: свежая изолированная
сессия не наследует IAB eligibility. Нельзя продолжать огромную историческую
задачу, иначе накопленный контекст уничтожит экономию токенов.

## Установка launcher

Сгенерируйте три LaunchAgent:

```bash
python3 scripts/render_launchd.py \
  --config /absolute/path/to/config.json \
  --output-dir /absolute/path/to/staging
```

Установите `com.axrbarsic.xmention.autopilot.plist` вместе с poll и watchdog.
`WatchPaths` реагирует на изменение очереди, а минутный interval служит
страховкой восстановления. Пустой запуск завершается до старта Codex.

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
- Process lock сериализует launcher.
- Atomic JSON state сериализует claims.
- Общий wake-file lock не дает claim прочитать файл посреди атомарной замены
  очереди.
- Аренда 30 минут подавляет повторные пробуждения.
- Ошибка запуска Codex сразу освобождает claim.
- Если задача Sol упала, событие останется unresolved и снова станет доступно
  после окончания аренды.
- Перед каждой публикацией Sol все равно проверяет живой X и ledger.
- Локальный timeout IAB внутри одной сессии не доказывает глобальный отказ
  Browser. В той же Browser-owner задаче надо начать один свежий turn, один раз
  выполнить официальный bootstrap и продолжать только после успешного
  авторизованного read-only preflight.
- Browser-owner runs используют одну выделенную задачу и не создают задачу на
  каждое событие. Ее надо ротировать до сильного роста контекста.
- Если Codex вернул нулевой exit code, но leased event остался в очереди,
  health получает `completed_unresolved`. Ошибка или такой незавершенный run
  создают одно локальное macOS уведомление на смену состояния.

## Проверка

```bash
python3 -m unittest discover -s tests -v
python3 scripts/autopilot_dispatch.py \
  --config config.json \
  --lease-seconds 1800 \
  status
python3 scripts/autopilot_resume.py \
  --config config.json \
  --lease-seconds 1800
```

Затем используйте один реальный прямой ответ как canary:

1. Watcher создает ровно одно событие.
2. Launcher создает один claim и один ephemeral Sol run.
3. Повторный запуск внутри аренды не запускает второй Sol turn.
4. Sol публикует ответ или делает durable skip.
5. Очередь становится пустой.
6. Dispatcher удаляет разрешенный ID из своего state.
7. Новая задача Codex не появляется, а легкий owner остается в выбранном
   бюджете контекста.
