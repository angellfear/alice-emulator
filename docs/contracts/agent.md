---
title: "Агент: движок ответа модели"
status: stable
date: "2026-10-30"
module: "server_emulator/agent/service.py"
---

# Агент: движок ответа модели

Код: `server_emulator/agent/` — `service.py` (AgentService), `model.py` (чат-модель поверх
`Runtime.provider`), `tools.py` (инструменты), `policy.py` (классы риска), `scenarios.py`
(сценарии как инструменты). Вход — `Runtime.chat(text, history=None, thread=None, **context)`.

## Движки

| engine | Что | История |
|---|---|---|
| `graph` (по умолчанию) | LangChain `create_agent` на LangGraph | тред в `state/agent.sqlite3` |
| `legacy` | прежний цикл `Runtime._chat_legacy`, до 5 кругов | `history` вызывающего |

Выбор: переключатель «AI агент включён» во вкладке «AI агент» (`settings.agent.engine`: включён — `graph`, выключен — `legacy`); переопределяет переменная окружения `EMULATOR_AGENT_ENGINE`.
Без зависимостей extra `agent` (`pip install -e ".[agent]"`) работает `legacy`.
Выключенный агент (`legacy`) — не только прежний цикл ответа: задания по расписанию не
запускаются (запуск пишется со статусом `skipped_disabled`), `agent-event` отвечает 503,
предложения сценариев не готовятся; во вкладке разделы агента (настройки, журнал, «Память»,
«Задания») погашены. MCP-инструменты работают и в `legacy`. Состояние для меню —
`GET api/agent-ops?brief=1` → `{settings:{enabled}}`.
Заглушка `settings.model.stub_text` отвечает раньше обоих движков.

Модель вызывается через `Runtime.provider("model", payload)` в формате OpenAI Chat
Completions — те же адаптеры `voice_stack`, пресеты и профили устройств, что и раньше.

## Треды

| thread | Кто |
|---|---|
| `voice:<SN>` | колонка; без SN — `voice:session:<id соединения>` |
| `studio:panel` | вкладка «Проверка» (или `thread` из запроса) |
| `studio:<uuid>` | `Runtime.chat` без thread — разовый |

Реплика задания или события, сказанная на колонке, добавляется в `voice:<SN>` ответом агента
(`AgentService.note`; тред, ждущий подтверждения, не трогается): ответ «да» на вопрос задания
модель видит вместе с вопросом. Тред переживает переподключение колонки и рестарт сервера. `ResetSession` удаляет тред
(`AgentService.reset`). Длинный тред сжимается: при `summarize_after` сообщениях
(40) старые заменяются сводкой, последние `keep_messages` (20) остаются как есть.
Системный промпт в тред не пишется: он собирается на каждый прогон (время, колонки).

## Классы риска и подтверждение

| Класс | Примеры | Подтверждение |
|---|---|---|
| `read` | `speakers_list`, `db_query`, `scenario_list/get/probe` | нет |
| `device` | `speaker_command`, `alice_action`, `scenario_run` сценария без `risk` | нет |
| `external` | действия MCP-серверов вне колонки | да |
| `config` | правка сценариев и заданий (следующие фазы) | да |
| `destructive` | удаление | да |

Внешний MCP-инструмент: поле `risk` в `settings.tools[]` (вкладка MCP, «Класс риска»).
Без него — из прежнего `mode`: `auto` → `device`, `confirm` → `external`. Установка `risk`
синхронизирует `mode` для legacy. Заготовка без `server_id` — `external`.
`scenario_run` наследует `risk` сценария (поле JSON-сценария, по умолчанию `device`).

Вызов класса с подтверждением останавливает прогон interrupt'ом
(`HumanInTheLoopMiddleware`). Ответ `Runtime.chat`: текст модели + `confirm_prompt`,
`tool_calls` — ожидающие вызовы с `thread_id` и `origin: "agent"`. Решение:

- **голосом** — следующая реплика в том же треде, раньше роутера сценариев (агент —
  владелец диалога): `confirm_yes` → выполнить; `confirm_no` → отказ (модели: «пользователь
  отказался»); другое → отказ, реплика передаётся модели вместо результата инструмента;
- **панелью** — `POST tool-call {call_id}` подтверждает, `{call_id, reject: true}` отклоняет;
  прочие ожидающие вызовы того же прогона отклоняются. Ответ: `{content, answer, thread}`.

## Сценарии и агент

Порядок голосовой реплики: агент ждёт подтверждения → ожидающий сценарий (ask) → probe →
сценарий без LLM → агент. Агенту в системный промпт идёт индекс сценариев (id, название,
фразы). Инструменты: `scenario_list`, `scenario_get`, `scenario_probe` (dry-run),
`scenario_run {text, id?}` — запуск через `DialogManager.run_scenario` без роутера; эффекты
те же (свет, push), директивы ответа сценария добавляются в AliceResponse агента. Если
сценарий уточняет (ask) в голосовом диалоге с `DialogId`, следующая реплика уходит ему;
без `DialogId` уточнение снимается.

## Ответ `Runtime.chat` (graph)

`{text, tool_calls, usage, tool_runs, directives, scenario_messages, thread, engine}`.
`directives` — громкость из `alice_action`; `scenario_messages` — protobuf-ответы сценариев
(только голосовому тракту, студия их не отдаёт). Лимит — `max_model_calls` (6) вызовов
модели на прогон, затем «Не получилось завершить действие: слишком много шагов.»

## Настройки `settings.agent`

`engine`, `max_model_calls`, `summarize_after`, `keep_messages`, `confirm_yes`, `confirm_no`,
`confirm_prompt`. Значения по умолчанию — `agent/service.py` `DEFAULTS`.
Эксплуатация — `daily_model_calls`, `budgets`, `pii`, `suggest_cron`, `suggest_min_count`,
`suggest_days`, `webhook_token` (`agent/ops.py` `DEFAULTS`). Вкладка «AI агент» правит их через
`POST api/agent-ops {op:"settings", settings:{enabled, max_model_calls, daily_model_calls, budgets,
pii, suggest_cron, suggest_min_count, webhook_token}}`; токен вебхука в ответах — маской,
`"__KEEP__"` — не менять.

## Память

Долговременная память владельца колонки — [memory.md](memory.md): факты, эпизоды, пожелания
в системном промпте, инструменты `manage_memory`/`search_memory`/`memory_instructions`,
фоновая рефлексия LangMem после паузы в треде.

## Задания по расписанию

[routines.md](routines.md): агент или сценарий по cron/интервалу/разово, доставка на колонку
или во входящие (передаются в следующем диалоге), инструменты `routine_*` с подтверждением.

## Правка сценариев

[scenarios.md §4b](scenarios.md): черновик → JSON Patch → проверка фраз и перехвата → активация
с подтверждением; пользователь включает только личные сценарии, общие — предложения в панель.

## Эксплуатация: журнал, бюджет, PII, промахи, события

`agent/ops.py`, `state/agent_ops.sqlite3` (хранится 30 дней).

- **Журнал прогонов** — каждый `chat`/`resume`: `ts, thread, kind, user_id, sn, text, answer,
  tools[{name,status}], model_calls, tokens, ms, status`. `kind`: `request` (новая реплика),
  `confirm` (ответ на подтверждение), `resume` (панель), `background` (задание), `event`, `suggest`.
  `status`: `ok | pending | limit | budget`.
- **Дневной бюджет** — `agent.daily_model_calls` (0 — без лимита, по умолчанию) вызовов модели в сутки на
  пользователя, свой лимит — `agent.budgets {user_id: n}`. Исчерпан — модель не вызывается,
  ответ «Дневной лимит запросов к модели исчерпан. Продолжим завтра.»
- **PII** — `agent.pii` (`["credit_card", "secret"]`; доступны также `email`, `ip`, `mac_address`,
  `url`): `PIIMiddleware` LangChain на ввод и результаты инструментов до вызова модели — карты
  маскируются, ключи/токены/пароли (`sk-…`, `Bearer …`, `пароль: …`) заменяются на `[REDACTED_SECRET]`.
- **Промахи** — реплики `kind=request` в тредах `voice:*` (их не взял ни один сценарий),
  сгруппированные по нормализованной фразе. Задание `agent.suggest_cron` («30 4 * * *», пусто —
  выключено) отдаёт агенту фразы, встреченные ≥ `suggest_min_count` (3) раз за `suggest_days` (7)
  дней: он готовит черновики сценариев, включает их администратор. Вручную — кнопка во вкладке
  «AI агент» (`POST api/agent-ops {op:"suggest"}`).
- **Внешние события** — `POST /emulator/api/agent-event {text, sn | user_id, delivery?, title?}`
  с заголовками `X-Emulator: 1` и `X-Agent-Token: <agent.webhook_token>` (без токена — только
  сессия администратора). Агент выполняет `text` фоном в треде `event:<sn>`; итог доставляется
  как у заданий (`speak | inbox | silent`); вызовы с подтверждением — в панель.

## Медленная модель: отложенный ответ (`agent.async_reply_s`)

Модель не ответила за `async_reply_s` (3 с) — колонка сразу слышит `async_wait_text`
(«Секунду, думаю.», из звукового кеша), ответ модели синтезируется до Push и приходит Push'ем
`server_action` (как напоминание облака), с карточкой `divui_show_view` (docs/contracts/aapi.md,
`async_reply.py`). Ответ, ждущий подтверждения действия или заканчивающийся вопросом, ставит
`ShouldListen` — колонка слушает «да/нет» без «Алиса»; «да» уходит агенту (тред тот же).
Озвучка по фразам (`agent.stream_reply`, ранние/частичные ответы, шум, фразы «пока думаю»,
потоковые `complete_stream` адаптеров, `RuntimeChatModel._astream`) убрана 2026-10-30: её
заменил отложенный ответ. В синтез не идут эмодзи и разметка Markdown (`spoken.speakable`);
реплике с колонки агент получает в промпт `VOICE_NOTE`. Сбой провайдера модели — в журнале
прогонов со статусом `error`, отменённый колонкой запрос — `cancelled`.

## Не реализовано (следующие фазы)

—
