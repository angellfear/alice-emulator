# Архитектура эмулятора Алисы (alica_emule)

Версия: 2026-10-31. Нормативный документ — код должен соответствовать описанному здесь.

## Системные границы

Один процесс Python 3.11+, один asyncio event loop, один aiohttp Application.

```
  Устройства ──────► HTTP/WS (uni.ws, quasar, update) ─┐
  (Яндекс Станции)                                      │
                    ┌───────────────────────────────────▼────┐
                    │  Server Emulator                        │
                    │  runtime.py — центральный объект        │
                    │  ┌────────────┐  ┌──────────────────┐  │
                    │  │ voice.py   │  │ scenario_engine/  │  │
                    │  │ AAPI↔голос │  │ JSON v2 executor  │  │
                    │  └─────┬──────┘  └────────┬─────────┘  │
                    │        │                  │             │
                    │  ┌─────▼──────────────────▼──────────┐ │
                    │  │ agent/service.py (LangGraph)       │ │
                    │  │ mcp_hub.py (MCP SDK)               │ │
                    │  │ voice_stack/ (адаптеры ASR/TTS/LLM)│ │
                    │  └───────────────────────────────────┘ │
                    │  state_store.py → state/store.db       │
                    │  UI: server_emulator/ui/ (vanilla JS)  │
                    └────────────────────────────────────────┘
```

Код встраивания эмулятора в внешний прокси (`STANDALONE_KEY`, колбэки `record`/`capture`,
`disconnect_proxy`, `client_peer`, `emulation_excluded_sn`) остался от прежней структуры и
заменяется заданиями TASK-043/044 (проход в Яндекс, режимы устройств).

## Инварианты

1. **Один event loop** — весь I/O через asyncio. Блокирующие вызовы запрещены в основном потоке. SQLite доступ через `aiosqlite` или в `run_in_executor`.
2. **Раздельные БД** — Emulator (`state/store.db`), Agent (`state/agent.sqlite3`, `state/agent_ops.sqlite3`). Подсистемы не читают чужие базы.
3. **SQLite WAL** — все базы открываются с `journal_mode=WAL`. Один writer, много readers.
4. **Состояние эмулятора только через StateStore** — `state_store.py` владеет таблицами `users`, `devices`, `aliases`, `settings`, `scenarios`, `timers`, `params`. Остальные модули обращаются через его API.
5. **Адаптеры без побочных эффектов** — `voice_stack/adapters/*.py` — чистые функции (`complete`, `transcribe`, `speech`), без собственного состояния. Конфигурация приходит аргументом.
6. **Сценарии: простое — JSON, сложное — py** — JSON v2 (`scenario_engine/`) описывает простой ответ и одно действие (`compose / action / ask / finish / error`); логика с данными, разбором фразы, ветвлением и состоянием — py-сценарий на SDK (`sdk.py`, `executor: script`). Шаги `fetch/extract/load/refresh/store/branch/set/wait_state` удалены (TASK-027): валидатор отвергает их с ошибкой «перенесите в py». В коде — только универсальные примитивы без предметной логики.
7. **Протоколы не смешиваются** — AAPI (protobuf/WS), Quasar HTTP (REST/JSON), Push (WS server_action) обрабатываются раздельными модулями.
8. **Агент изолирован от голоса** — `agent/service.py` получает текст и возвращает текст + директивы. Аудио, protobuf, AAPI — ответственность `voice.py`.

## Модель конкурентности

- Все HTTP/WS хэндлеры — корутины в общем event loop.
- `voice.py`: одно AAPI-соединение = одна корутина. PCM-буфер и Ogg-ассемблер — per-connection.
- `agent/service.py`: прогоны одного треда сериализуются `asyncio.Lock`. Разные треды параллельны.
- `mcp_hub.py`: внешние MCP-серверы — отдельные stdio/HTTP процессы. Вызовы через MCP SDK с таймаутом.

## Владение состоянием

| Владелец | Хранилище | Что хранит |
|---|---|---|
| `server_emulator/state_store.py` | `state/store.db` | users, devices, aliases, settings, scenarios, timers, params, executions |
| `server_emulator/agent/service.py` | `state/agent.sqlite3` | LangGraph checkpoints (история диалогов), langmem store (память) |
| `server_emulator/agent/ops.py` | `state/agent_ops.sqlite3` | Журнал прогонов (30 дней), дневной бюджет, промахи |
| `server_emulator/miss_journal.py` | `state/{phrase,callback,aapi}_misses.json` | Очередь «Без ответа»: фразы без сценария, колбэки @@mm_* без сценария, WS AAPI без ответа; лимит 300 на журнал |
| `server_emulator/tts_cache.py` | `state/tts_cache/` | Ogg-файлы фраз + index.json. Ключ: движок+URL+модель+голос+текст |

## Точки расширения

| Механизм | Где | Как |
|---|---|---|
| **Voice Stack адаптеры** | `state/voice_stack/adapters/*.py` | Модуль с `SERVICES`, `complete`/`transcribe`/`speech`. Hot-reload по mtime |
| **MCP серверы** | `state/mcp.json` | stdio или streamable HTTP. Инструменты автоматически в агенте |
| **Сценарии** | `state/scenarios/*.json` | JSON v2 через scenario_engine. CRUD через UI/API |
| **Voice Stack пакеты** | `voice_stack/packages/*.json` | Описание локального сервера (sherpa, piper, f5-tts) с установщиком |
| **Quasar config шаблоны** | `state/quasar_config/` | Шаблоны + device overrides для get_config_* ответов |

## Расширения

Расширение — git-релиз, zip, каталог или локальная папка с `extension.json` в корне
(контракт: `docs/contracts/extensions.md`, схема: `server_emulator/extensions/schema.json`,
менеджер: `server_emulator/extensions/` — TASK-029).

| Источник | Вид |
|---|---|
| GitHub-репозиторий | последний релиз; `…#v1.2.0` / `…#<sha>` — закреплённый |
| Прямая ссылка | zip с `extension.json` в корне |
| Каталог | JSON-индекс `{extensions: [{id, url, version, sha256?}]}` |
| Локальная папка | режим разработки: без копирования, правки подхватываются |

Скачивание — только https, лимит размера, sha256 если указан, защита от path traversal;
проверка совместимости `requires.emulator/sdk` (`SDK_VERSION` из `server_emulator/sdk.py`).
Установленное лежит в `state/extensions/<id>/<version>/`, `current` — активная версия
(атомарное переключение), предыдущая хранится для отката.

Приоритет при совпадении имён: **пользовательское > расширение > встроенное** — каталоги
читают слои источников в этом порядке, запись хранит `origin` (`builtin | extension:<id> | user`);
записи расширения — только чтение (правка создаёт пользовательскую копию). Имена элементов —
с префиксом `<ext-id>.`.

| Класс | Когда | Установка |
|---|---|---|
| `data` | только json/yaml/gif/wav | сразу |
| `code` | есть `.py` — исполняется в процессе сервера без песочницы | только с подтверждением |
| `install` | скачивает программы/модели (пакеты голосового стека) | только с подтверждением |

`code`/`install` ставятся вторым подтверждённым запросом: карточка показывает список `.py`,
разрешений (`permissions`: `network: [хосты]`, `actions: [ref]`, `device_push`, `install`),
размера и python-зависимостей. Разрешения — не песочница, а основа информированного
подтверждения; `requires.python_packages` ставятся отдельной кнопкой.

Расширение добавляет сценарии, эндпоинты, конфиги Quasar, пакеты/адаптеры/пресеты голосового
стека, списки hassil, режимы «Интеллекта», MCP-серверы, действия, датасеты и данные разделов
(анимации, параметры, ответы, каталог, обновления) — полная таблица «папка → что подключает →
класс» в контракте. Хуки `hooks.py` (`on_install/on_enable/on_disable/migrate(from_version)`)
менеджер вызывает с таймаутом, ошибка хука откатывает установку; `tests/` — самопроверка
фраз через dry-run после установки (провал — предупреждение, автовключения нет).

## Потоки данных

### Голосовой поток

```
Устройство → WS uniproxy.local/uni.ws → voice.py
  │
  1. SynchronizeStateRequest → runtime.dispatch → quasar.py
  2. VoiceInput (Ogg Opus) → input_audio() → PCM буфер
  3. Close → ASR adapter → текст
  4. Router:
     a. agent ждёт подтверждения → classify → approve/reject/other
     b. scenario_engine.router → сценарий → executor → compose
     c. сценария нет → phrase_misses (очередь «Без ответа») → режим «Интеллект»:
        scenarios — стандартный ответ not_understood (пакет озвучки),
        model — один вызов LLM без инструментов, agent — agent/service.py → LangGraph run
  5. Ответ:
     a. Текст → spoken.speakable() → TTS adapter → Ogg Opus pages
     b. Директивы (громкость, карточка) → AliceResponse protobuf
  6. → WS → устройство
```

### Режим «Интеллект»

Ось, независимая от `mode` (proxy/emulate): `settings.intelligence`, модуль `intelligence.py`.
Решение — в одной точке: `ProviderManager.chat` и `agent.engine()`; вызывающие код не ветвят.

| Режим | Фраза без сценария | Нужно | Вкладки «Интеллект» |
|---|---|---|---|
| `scenarios` | стандартный ответ `not_understood` | ASR; TTS не обязателен (пакет озвучки) | скрыты |
| `model` | один вызов LLM: промпт + история, без MCP/памяти/заданий | + LLM API | Модель |
| `agent` | LangGraph-агент (без extra `agent` — legacy-цикл с инструментами) | + extra `agent` | все |

Старые настройки мигрируют из `agent.engine`: `graph` → `agent`, иначе `model`.

Режимы расширяемы (TASK-034): включённое расширение добавляет режимы из `modes/*.py`
(`MODE` = `id`, `title`, `tabs`, `preset`? + `async answer(ctx, text, history)`, ctx — SDK
`ScenarioContext`); значение `settings.intelligence` — `ext:<ext-id>.<MODE.id>`, сегменты
переключателя UI строятся из списка `/api/intelligence`. Расширение выключено, удалено или
режим упал — откат на `scenarios` с записью в `recent`; контракт — `docs/contracts/extensions.md`.

### Ответы: эндпоинты, Quasar и сценарии — одна система

Каждый обработчик — **канал + триггер → исполнитель**. Хранилища раздельные (протоколы не смешиваются),
общие — UI-группа «Эмулятор» и очередь промахов «Без ответа» (`api/misses`).

```
HTTP:  обновления (OTA) → builtin Quasar (quasar.py + шаблоны конфигов) → эндпоинты (static/template/script)
       → нет обработчика → endpoints_misses.json → честный 404
Голос: стандартные ответы (unclaimed) → сценарии (reply/flow/script/app/mcp)
       → нет сценария → phrase_misses.json → режим «Интеллект»
Колбэк server_action (@@mm_*): protocol_event-сценарии → нет сценария → callback_misses.json
WS AAPI: разговор (voice.py) → протокольный каркас (control_response) → нет ответа → aapi_misses.json
       (кроме Ack/StreamControl и TextInput/VoiceInput, выключенных настройками)
```

Из записи очереди создаётся обработчик: HTTP-промах → эндпоинт, фраза → сценарий, колбэк → protocol_event-сценарий;
AAPI-сообщение без ответа требует кода (образец сообщения хранится в журнале).

### Отложенный ответ (модель > 3 с)

```
voice.py: таймер async_reply_s → «Секунду, думаю.» (звуковой кеш) → устройство
agent: ответ готов → TTS заранее → async_reply.keep()
→ Push server_action @@mm_semantic_frame + divui_show_view
→ устройство Ack → TextInput Event → AliceResponse + TtsSpeak
→ вопрос/подтверждение: ShouldListen (колонка слушает без «Алиса»)
```

## Зависимости

### Python

Полный список — `pyproject.toml`. Основные:

| Пакет | Назначение |
|---|---|
| aiohttp | HTTP/WS сервер и клиент |
| protobuf | AAPI wire format |
| zstandard | Сжатие данных |
| langchain, langgraph, langmem | Агент и долговременная память |
| mcp | MCP SDK (stdio + streamable HTTP) |
| apscheduler | Задания по расписанию |
| hassil | Шаблоны фраз сценариев |
| aiodns | DNS-резолвинг |
| ddgs, trafilatura, httpx | web_search / web_fetch |
| sherpa-onnx (внешний сервис) | Локальный ASR/TTS, см. `tools/builder_*.py` |

### Frontend

Vanilla JS (`server_emulator/ui/`) — UI эмулятора, без сборки.

## Исполнитель сценариев: размеры и вывод шагов (TASK-027)

Встроенные сценарии переведены в py (TASK-024…026), в JSON остались простые ответы. Шаги
`fetch/extract/load/refresh/store/branch/set/wait_state` удалены: исполнитель их не знает, валидатор
отвергает сценарий с ними («шаг <op> больше не поддерживается — перенесите в py»).

| Файл | Строк до | Строк после |
|---|---|---|
| `scenario_engine/executor.py` | 841 | 615 |
| `scenario_engine/bindings.py` | 384 | 384 (остаётся: compose py-сценариев раскрывает `{{state.*}}`, фильтры, `$each`) |
| `scenario_engine/parsers.py` | 162 | 162 (остаётся: `ctx.parse_*` SDK) |
| Итого | 1387 | 1161 (−16%) |

Остались в `executor.py`: `refresh_dataset`/`_run_plugin`/`_dataset_id` (SDK `ctx.dataset`), `_load_store`
(`store.*` в контексте), `fetcher` (SDK `ctx.fetch_json`). `validate.py` (контракты шагов, граф) — 482 → 424 строки.

## Архитектурный долг

### Критический

| ID | Проблема | Файл | Строк | Влияние |
|---|---|---|---|---|
| DEBT-01 | **God-object Runtime** — 26 компонентов в конструкторе, WS-сессии, mode switching в одном классе (вынесено: SettingsService, Dispatcher, ProviderManager — TASK-003/004/007) | `runtime.py` | 584 | Невозможно тестировать компоненты изолированно, любое изменение рискует сломать всё |
| DEBT-02 | **Монолит voice.py** — ASR pipeline, response composition, TTS streaming, Push delivery в одном файле (вынесено: audio_buffer, voice_response — TASK-005/008) | `voice.py` | 1200 | Нельзя менять ASR без риска для TTS; есть e2e-страховка (TASK-009) |

### Средний

| ID | Проблема | Влияние |
|---|---|---|
| DEBT-07 | Нет интерфейсов/протоколов между подсистемами — всё через прямые импорты runtime | Сильная связанность |
| DEBT-08 | Тесты: ~100 файлов; e2e голосового потока есть (`tests/test_voice_e2e.py`, TASK-009), интеграционных тестов остальных потоков нет | Регрессии при рефакторинге |
