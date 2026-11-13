# Архитектура эмулятора Алисы (alice-emulator)

Версия: 2026-11-07. Нормативный обзор: границы, инварианты, потоки. Детали модулей — в [контрактах](contracts/); при расхождении с кодом это дефект кода или документа, его исправляют.

Карта компонентов, голосового хода и хранилищ — [architecture-map.svg](architecture-map.svg) (пересборка: `python tools/gen_architecture_map.py`).
Карта сценариев — порядок выбора, поставщики ответа, фразы и ветвления каждого сценария — [scenario-map.svg](scenario-map.svg) (пересборка: `python tools/scenario_map.py`).

## Системные границы

Один процесс Python 3.11+, один asyncio event loop, один aiohttp Application. Публичный TLS-порт слушает TCP-фронт.

```
  Устройства ──► front.py (SNI) ──► HTTP/WS (uni.ws, quasar, update) ─┐
  (Яндекс Станции)     │ не в сертификате: туннель/отказ              │
                    ┌──────────────────────────────────────────────── ▼ ┐
                    │  runtime.py — сборка компонентов                  │
                    │  dispatcher.py — маршрут устройства, HTTP         │
                    │  ┌────────────┐  ┌──────────────────┐             │
                    │  │ voice.py   │  │ scenario_engine/ │  sdk.py     │
                    │  │ AAPI↔голос │  │ JSON и py        │  extensions/│
                    │  └─────┬──────┘  └────────┬─────────┘             │
                    │  ┌─────▼──────────────────▼──────────┐            │
                    │  │ agent/ (LangGraph), mcp_hub (MCP) │            │
                    │  │ voice_stack/ (адаптеры ASR/TTS/LLM)│           │
                    │  └───────────────────────────────────┘            │
                    │  upstream/ — проход к Яндексу и журнал трафика    │
                    │  state_store.py → state/emulator.sqlite3          │
                    │  UI: server_emulator/ui/ (vanilla JS)             │
                    └───────────────────────────────────────────────────┘
```

## Принципы

**Готовое прежде самописного.** Новый механизм начинается с поиска библиотеки или SDK. Своё — только если готового нет или оно не подходит, с причиной в контракте. Обосновано своё сегодня: разбор протокола колонки из её прошивки, директивы и сценарии по журналу настоящего сервера.

**Штатный функционал колонки.** Таймеры, плеер, громкость, подсветку колонка делает сама; сервер отдаёт те же директивы, что настоящий сервер, и не заводит своих планировщиков и медиатек.

**Расширяемость через контракт.** Новое подключается через SDK, расширения, адаптеры и MCP, а не правкой ядра.

## Инварианты

1. **Один event loop** — весь I/O через asyncio. Блокирующие вызовы в основном потоке запрещены; SQLite — через `aiosqlite`, тяжёлое — `asyncio.to_thread`. Исключения: снимки `:memory:` (`mcp_hub`, `archive.py`), память `StateStore` с асинхронной записью в файл (`docs/contracts/params-db.md`), прямая запись `server_log.py` вне цикла.
2. **Раздельные БД** — у каждой подсистемы свой файл (таблица ниже). Подсистемы не читают чужие базы.
3. **SQLite WAL** — все базы открываются с `journal_mode=WAL`. Один writer, много readers.
4. **Состояние эмулятора только через StateStore** — `state_store.py` владеет своими таблицами, остальные модули обращаются через его API.
5. **Адаптеры без побочных эффектов** — `voice_stack/adapters/*.py` — функции (`complete`, `transcribe`, `speech`) без собственного состояния. Конфигурация приходит аргументом.
6. **Сценарии: простое — JSON, сложное — py** — JSON v2 описывает ответ и одно действие (`compose / action / ask / finish / error`); данные, разбор фразы, ветвление и состояние — py-сценарий на SDK (`executor: script`). В ядре — только универсальные примитивы без предметной логики.
7. **Протоколы не смешиваются** — AAPI (protobuf/WS), Quasar HTTP (REST/JSON), Push (WS server_action) обрабатываются раздельными модулями.
8. **Агент изолирован от голоса** — `agent/service.py` получает текст и возвращает текст + директивы. Аудио, protobuf, AAPI — ответственность `voice.py`.

## Модель конкурентности

- Все HTTP/WS хэндлеры — корутины в общем event loop.
- `voice.py`: одно AAPI-соединение = одна корутина. PCM-буфер и Ogg-ассемблер — на соединение.
- `agent/service.py`: прогоны одного треда сериализуются `asyncio.Lock`, разные треды параллельны.
- `mcp_hub.py`: внешние MCP-серверы — отдельные stdio/HTTP процессы, вызовы через MCP SDK с таймаутом.
- Журналы (`server_log.py`, `upstream/journal.py`) пишут через очередь и фоновую запись: поток ответа диска не ждёт.

## Владение состоянием

| Владелец | Хранилище | Что хранит |
|---|---|---|
| `state_store.py` | `state/emulator.sqlite3` | meta, settings, users, devices, aliases, unassigned, executions, param_defs, param_values, scenario_data |
| `agent/service.py` | `state/agent.sqlite3` | Checkpoints LangGraph (история диалогов), память langmem |
| `agent/ops.py` | `state/agent_ops.sqlite3` | Журнал прогонов (30 дней), дневной бюджет, промахи |
| `agent/routines.py` | `state/routines.sqlite3` | Задания по расписанию |
| `upstream/journal.py` | `state/traffic.sqlite3` | Журнал трафика устройств по опции «Запись» |
| `server_log.py` | `state/server_log.sqlite3` | Журнал сервера |
| `miss_journal.py` | `state/*_misses.json` | Промахи: фразы, колбэки, AAPI, HTTP |
| `tts_cache.py` | `state/tts_cache/` | Ogg-файлы фраз + index.json |

## Точки расширения

| Механизм | Где | Контракт |
|---|---|---|
| Расширения: сценарии, эндпоинты, адаптеры, режимы, MCP, действия, данные | внешние — `state/extensions/<id>/`, встроенные плагины — `server_emulator/plugins/<имя>/` (id `builtin.<имя>`) | [extensions](contracts/extensions.md) |
| Поставщики данных по видам: музыка, погода, новости | объявления плагинов `contributes.providers`, реестр `providers.py`, `ui/api/providers.py` | [providers](contracts/providers.md) |
| SDK py-сценариев | `sdk.py` | [scripts-sdk](contracts/scripts-sdk.md) |
| Адаптеры голосового стека | `state/voice_stack/adapters/*.py` | [voice-stack](contracts/voice-stack.md) |
| MCP-серверы | `state/mcp.json` | [mcp](contracts/mcp.md) |
| Сценарии | `state/scenarios/` | [scenarios](contracts/scenarios.md), [JSON v2](contracts/scenario-json-v2.md) |
| Замены Quasar-конфига | поставляемые — `server_emulator/quasar_defaults/rules/`, правки — `state/quasar/rules/` | [quasar-config](contracts/quasar-config.md) |
| Шаблоны экранных директив | встроенные — `server_emulator/app_cards/`, правки — `state/screen_templates/` | [templates](contracts/templates.md) |

Приоритет при совпадении имён: **пользовательское > расширение > встроенное**. Расширение с кодом исполняется в процессе сервера без песочницы и ставится только с подтверждением.

Встроенный плагин — то же расширение, поставляемое с эмулятором: установлен всегда, включён по умолчанию, выключается, но не удаляется. Встроенный и внешний плагин одного вида живут рядом: их разводит приставка `builtin.` в id.

## Сценарии: встроенные и от плагинов

Сценарий относится к одному из трёх классов. Класс задаёт, откуда берутся фразы и данные.

| Класс | Фразы | Данные | Сценарии |
|---|---|---|---|
| Встроенный | ядро | колонка и эмулятор, плагин не нужен | таймеры и будильники, время и дата, громкость, пауза, Bluetooth, LED, открыть приложение, служебные `system.*` |
| Ядро по поставщику | ядро; сценарий активен, только если включён поставщик вида | плагин вида (только данные) | погода и виджет погоды, новости |
| Экспорт плагина | плагин, по объявленным возможностям | плагин | музыка (с возможностями: сказки, подкасты, аудиокниги), видео (каталог ссылок в настройках плагина), умный дом, список покупок, игры, болталка и знания |

Правила:

- Ответ колонке (директивы, виджеты, стек плеера) собирает ядро по образцу настоящего сервера. Плагин отдаёт данные и не копирует исполнитель: два поставщика музыки пользуются одним стеком директив.
- Выключение плагина убирает его фразы. Ядро не держит контентных ключевых слов.
- Плагины регистрируются сами. Вид, название и возможности объявляет плагин, требования к функциям — сценарий-потребитель. Ядро не держит списка видов и не знает id плагинов. Свои сценарии плагин подключает только тогда, когда у ядра нет штатного исполнителя.
- Плагин регистрирует в справочнике фраз шаблоны, свои списки сущностей и обработчик. Числовых приоритетов нет, спор решает точность совпадения (ранжирование hassil, как в Home Assistant):
  1. совпадение по закрытому списку плагина (приложение станции, устройство умного дома) сильнее свободного хвоста;
  2. больше точных слов — сильнее;
  3. остались одни свободные хвосты («включи {X}») — опрос каталогов плагинов, отвечает нашедший;
  4. равная точность — ручной порядок плагинов, его задаёт владелец; по умолчанию первым идёт выбранный поставщик вида.

  Не нашёл никто — честный отказ.

Текущее состояние и что ещё не перенесено — [scenarios](contracts/scenarios.md), раздел «Классы сценариев».

## Потоки данных

### Голосовой поток

```
Устройство → WS uniproxy.alice.yandex.net/uni.ws → voice.py
  1. SynchronizeStateRequest → runtime.dispatch → quasar.py
  2. VoiceInput (Ogg Opus) → input_audio() → PCM буфер
  3. Close → ASR adapter → текст
  4. Router:
     a. agent ждёт подтверждения → classify → approve/reject/other
     b. scenario_engine.router → сценарий → executor → compose
     c. сценария нет → phrase_misses → режим «Интеллект»
  5. Ответ:
     a. Текст → spoken.speakable() → TTS adapter → Ogg Opus pages
     b. Директивы → AliceResponse protobuf
  6. → WS → устройство
```

### Режим «Интеллект»

Настройка `settings.intelligence`, независима от маршрута устройства. Решение — в одной точке: `ProviderManager.chat` и `agent.engine()`. Расширения добавляют свои режимы. Подробно — [agent](contracts/agent.md), [live-status](contracts/live-status.md).

| Режим | Фраза без сценария |
|---|---|
| `scenarios` | стандартный ответ `not_understood` |
| `model` | один вызов LLM: промпт + история, без MCP и памяти |
| `agent` | LangGraph-агент с инструментами и памятью |

### Маршрут устройства

У каждого устройства свой `route_mode`, неизвестное устройство — `emulate`. Журнал трафика — отдельная опция `record`. Подробно — [routing](contracts/routing.md).

| Режим | HTTP | WS |
|---|---|---|
| Эмуляция (`emulate`) | эмулятор | эмулятор |
| Вместе (`hybrid`) | эмулятор, промах — Яндекс | эмулятор, голосовой ход на промахе — параллельной сессии Яндекса |
| Прозрачно (`proxy`) | Яндекс, кроме OTA и каталога приложений | мост к Яндексу |

### Обработчики: канал + триггер → исполнитель

```
HTTP:  обновления (OTA) → builtin Quasar → эндпоинты (static/template/script)
       → нет обработчика → endpoints_misses.json → 404 (гибрид: Яндекс)
Голос: стандартные ответы → сценарии (reply/flow/script/app/mcp)
       → нет сценария → phrase_misses.json → режим «Интеллект»
Колбэк @@mm_*: protocol_event-сценарии → нет сценария → callback_misses.json
WS AAPI: разговор (voice.py) → реестр aapi_handlers → нет ответа → aapi_misses.json
```

### Отложенный ответ (модель > 3 с)

```
voice.py: таймер async_reply_s → «Секунду, думаю.» → устройство
agent: ответ готов → TTS заранее → async_reply.keep()
→ Push server_action @@mm_semantic_frame + divui_show_view
→ устройство Ack → TextInput Event → AliceResponse + TtsSpeak
→ вопрос/подтверждение: ShouldListen
```

## Подсистемы

| Подсистема | Контракт |
|---|---|
| Протокол голоса и AAPI | [aapi](contracts/aapi.md), [uniproxy](contracts/uniproxy.md), [ws-handlers](contracts/ws-handlers.md) |
| HTTP и Quasar | [http](contracts/http.md), [endpoints](contracts/endpoints.md), [quasar-config](contracts/quasar-config.md) |
| Фронт TLS | [front](contracts/front.md) |
| Маршруты и журнал трафика | [routing](contracts/routing.md) |
| Плеер, громкость, директивы | [player](contracts/player.md), [station-media](contracts/station-media.md), [device-actions](contracts/device-actions.md) |
| Сценарии и данные | [scenarios](contracts/scenarios.md), [providers](contracts/providers.md), [datasets](contracts/datasets.md), [params-db](contracts/params-db.md), [coverage](contracts/coverage.md) |
| Расширения и встроенные плагины | [extensions](contracts/extensions.md) |
| Агент | [agent](contracts/agent.md), [memory](contracts/memory.md), [routines](contracts/routines.md) |
| Журнал сервера | [server-log](contracts/server-log.md) |
| Версия, релизы, обновление | [self-update](contracts/self-update.md) |
| Каталог приложений | [catalog](contracts/catalog.md) |
| Умный дом: реестр устройств | [smart-home](contracts/smart-home.md) |
| Кабинет пользователя | [cabinet](contracts/cabinet.md) |
| Настройки эмулятора | [settings](contracts/settings.md) |

## Зависимости

Полный список — `pyproject.toml`.

| Пакет | Назначение |
|---|---|
| aiohttp | HTTP/WS сервер и клиент |
| protobuf, grpcio-tools | AAPI wire format, сборка схем |
| androguard | Импорт протокола и картинок из APK пользователя |
| zstandard | Сжатие журнала |
| numpy, soxr, PyOgg | Обработка аудио |
| langchain, langgraph, langmem | Агент и долговременная память |
| mcp | MCP SDK (stdio + streamable HTTP) |
| apscheduler | Задания по расписанию |
| hassil | Шаблоны фраз сценариев |
| jinja2 | Песочница шаблонов сценариев |
| pydantic | Типизированные модели настроек |
| alembic, sqlalchemy | Миграции схем SQLite при старте (`server_emulator/migrations/`, по ветке на базу). Накладные расходы: ~24 МБ установки (SQLAlchemy ~20 МБ, alembic ~2,5 МБ, Mako ~0,8 МБ), ~0,07–0,09 с на проверку «все базы на head» при старте (замер: 3 запуска, медиана старта сервера 7,3 с — вклад миграций незаметен) |
| aiodns | DNS прохода наверх и туннеля |
| dulwich, packaging | Версия и самообновление без git CLI (чистый Python, без нативных бинарников под платформы) |
| ddgs, trafilatura, httpx | web_search / web_fetch |
| sherpa-onnx (внешний сервис) | Локальный ASR/TTS, см. `tools/builder_*.py` |

UI — vanilla JS (`server_emulator/ui/`), без сборки.

## Архитектурный долг

| Приоритет | Проблема | Где | Направление |
|---|---|---|---|
| Критический | **God-object Runtime**: десятки компонентов в конструкторе, WS-сессии, переключение режимов; `runtime.*` читают 79 модулей | `runtime.py` | Явные зависимости и `typing.Protocol` между подсистемами, Runtime — только сборка |
| Критический | **Монолит voice.py**: ASR, сборка ответа, TTS-поток, Push в одном файле | `voice.py` | Разделение по этапам; страховка — `tests/test_voice_e2e.py` |
| Высокий | **Миграции баз разбросаны** по конструкторам (`ALTER`, `PRAGMA user_version`) | все базы | Alembic |
| Средний | **LLM-адаптеры на голом aiohttp** и своя модель LangChain | `voice_stack/adapters/`, `agent/model.py` | SDK или LiteLLM |
| Средний | Интеграционные тесты есть только у голосового потока | `tests/` | Сквозные тесты маршрутов и расширений |
| Низкий | Фоновые циклы на `asyncio.sleep`, хотя APScheduler уже есть | `updates.py`, `self_update/`, `server_log.py` | Единый планировщик |
| Низкий | Остатки встраивания во внешний прокси: `STANDALONE_KEY`; запасная ветка на `ImportError` при том, что LangChain — обязательная зависимость | `runtime.py`, `server.py`, `ui/api/status.py`, `agent/__init__.py` | Удалить |
| Низкий | Свой декодер protobuf без схемы для показа трафика | `protowire.py` | Проверить `UnknownFieldSet` и готовые декодеры |
