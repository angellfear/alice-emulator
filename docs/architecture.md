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
`disconnect_proxy`, `client_peer`) остался от прежней структуры и заменён собственным
проходом наверх эмулятора (TASK-043) и режимами маршрута устройств (TASK-044).

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
| `server_emulator/miss_journal.py` | `state/{phrase,callback,aapi}_misses.json` | Промахи (без отдельной вкладки, TASK-061): фразы — в «Сценариях», HTTP-запросы — в «HTTP», AAPI и колбэки — в «WS» (свёрнутые блоки, `ui/misses.js`); лимит 300 на журнал |
| `server_emulator/upstream/journal.py` | `state/traffic.sqlite3` | Журнал трафика устройств по опции «Запись»: обмены HTTP (ответы эмулятора и Яндекса) и WS (lifecycle, AAPI-кадры, неизвестные кадры) с телами (zstd); группы по устройству и ключу события, ≤ 10 записей на пару |
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

Ось, независимая от режима маршрута (TASK-044): `settings.intelligence`, модуль `intelligence.py`.
Решение — в одной точке: `ProviderManager.chat` и `agent.engine()`; вызывающий код не ветвит.

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

### Режимы маршрута устройств (TASK-044)

Ось «кто отвечает» — у каждого устройства своя: `route_mode` в реестре (identities),
устройство без своего маршрута и неизвестное устройство — всегда `emulate` (TASK-064: маршрута по
умолчанию нет). Глобального `settings.mode` больше нет (standalone-запрет снят — проход наверх
свой, TASK-043). Миграция: `mode: emulate` → `emulate`; `mode: proxy` → `proxy` и опция «Запись»
`on` устройствам без своего маршрута; каждый SN из `emulation_excluded` → `route_mode: proxy`
(очередь в meta `route_migration` — применяется и устройствам, подключившимся позже);
ключи `mode`/`emulation_excluded` из настроек удалены. Прежний маршрут «Запись» (TASK-054)
больше не маршрут: `route_mode: record` → `proxy` + `record: on`, `default_route_mode: record` →
`proxy` + `record: on` устройствам без своего маршрута (однократно, meta `record_migration`).
Прежний `settings.default_route_mode` ≠ `emulate` (в том числе полученный из legacy-миграций)
однократно переносится устройствам реестра без своего `route_mode`, ключ из настроек удаляется
(`SettingsService._migrate_default_route`); не вошедшие в реестр устройства получают `emulate`.

| Режим | HTTP | WS |
|---|---|---|
| Эмуляция (`emulate`) | эмулятор | эмулятор |
| Гибрид (`hybrid`) | эмулятор, где есть обработчик; промах — upstream с отметкой «ушёл в Яндекс» в «Без ответа» | эмулятор (фаза 1); пересылка отдельных необработанных сообщений — фаза 2, отдельное задание |
| Прозрачно (`proxy`) | upstream; `get_sync_info` с источником «Напрямую» и галочкой «И в прозрачном маршруте» — через замены (TASK-065) | мост upstream |

#### Опция устройства «Запись» (TASK-054)

Журнал трафика — отдельная ось, не маршрут: опция устройства `record` (`off` по умолчанию |
`on` | `misses`; поле реестра, `PUT /api/record_mode {sn, mode}`) читается на каждом обмене и
кадре, соединение не закрывается — смена действует сразу. Нет SN (устройство не опознано) —
не пишется.

| `record` | HTTP | WS |
|---|---|---|
| `off` | ничего | ничего |
| `on` | каждый обмен: ответ эмулятора (`source=emulator`) или Яндекса (`source=upstream`) | open/close сессии, AAPI-кадры обеих сторон, неизвестные кадры |
| `misses` | только запросы без локального обработчика | только AAPI-события колонки без обработчика и ответы на них (по `RefMessageId`) |

«Нет обработчика» определяют функции без побочных эффектов, общие для всех маршрутов:
`Dispatcher.local_handler(host, method, path)` для HTTP (OTA-эндпоинты, builtin Quasar по
таблице маршрутов, включённый эндпоинт каталога; скриптовый эндпоинт — «нет обработчика»,
каталог приложений — «есть») и `aapi_handlers.has_handler(trigger, settings)` для AAPI.
Обработчики AAPI-событий регистрируются декоратором `@aapi_handler(trigger, enabled=…)` рядом
со своим кодом (`Conversation` в `voice.py`); `Conversation.receive` и `has_handler` берут их из
одного реестра, отдельного списка событий нет. `enabled(settings)` — условие по настройкам
устройства (например `TextInput` — при `aapi_model`). Сомнение — «нет обработчика», то есть
пишется. Запрос с
обработчиком, ушедший в Яндекс (прокси), в `misses` не пишется; промах эмуляции и промах
гибрида — пишутся. Запись источников TASK-052 (`quasar_source: direct`, «Получить с
оригинального сервера») идёт по той же опции; у `get_sync_info` обработчик есть, поэтому
`misses` её не пишет. В режиме «Напрямую» ответ Яндекса может править список замен regexp устройства
(`state.quasar_direct_replace`, `quasar_replace.py`, TASK-063); результат, не прошедший как JSON, колонке
не уходит — отдаётся оригинал; журнал хранит ответ Яндекса. Замены применяются и при маршруте
«Прозрачно», если у устройства включена галочка `state.quasar_direct_transparent` (TASK-065): только
`get_sync_info`, остальной трафик маршрута — прямой проход.

WS: бинарный кадр без префикса `AAPI` — звук, данные не пишутся (в записи закрытия сессии —
счётчики кадров и байт по направлениям). AAPI-кадр — отдельная запись: направление, имя
сообщения, `MessageId`/`RefMessageId`, тело (protobuf; в API карточки — `text_format`).
Не разобранный или текстовый кадр — запись `unknown` как есть. Источник: эмулируемая сессия
(`runtime.websocket`) и мост наверх (`Forwarder.bridge_ws`) пишут через общий `upstream.record.WsTap`.

Журнал группирует записи по устройству и ключу события (HTTP — `METHOD host path` без query,
WS — `ws → Event.<имя>` / `ws ← Event.<имя>`, `ws open`, `ws close`, `ws unknown →`): на пару
хранятся последние 10 записей, счётчик всех случаев и время первого/последнего — в таблице
`journal_groups` (`PRAGMA user_version=2`; старые записи при открытии получают ключ, группы
строятся с прежним числом случаев, звуковые кадры старых WS-сессий удаляются, остаются счётчики).

Определение устройства запроса — `dispatcher.route_mode_for(request) → (sn, device_id, mode)`:
заголовки uprx/quasar → query device_id → реестр; resolve-only. Смена маршрута устройства
(`PUT /api/route_mode`) действует сразу, без подтверждения; ответ не ждёт колонку. Открытые WS
закрываются (1012, колонка переподключается сама) только когда меняется способ их обслуживания:
эмулятор (`emulate`, `hybrid`) или мост в Яндекс (`proxy`) — то есть при переходе в «Прозрачно»
или из него. `emulate` ↔ `hybrid` WS не рвёт, HTTP маршрутизируется заново на каждом запросе.
Цель upstream строит `upstream.target_for`: канонический хост из алиасов эмулятора, https/wss
к Яндексу (значение алиаса может задавать полный `http://…` upstream — тесты и кастомные
сервера). В proxy устройство всё равно регистрируется в реестре при представлении —
маршрут виден и сменяем в UI (таблица «Устройства», колонки «Маршрут» и «Запись»).

### Ответы: эндпоинты, Quasar и сценарии — одна система

Каждый обработчик — **канал + триггер → исполнитель**. Хранилища раздельные (протоколы не смешиваются),
общие — UI-группа «Эмулятор» и очередь промахов «Без ответа» (`api/misses`).

```
HTTP:  обновления (OTA) → builtin Quasar (quasar.py + шаблоны конфигов) → эндпоинты (static/template/script)
       → нет обработчика → endpoints_misses.json → честный 404
       (гибрид: вместо 404 — upstream, промах остаётся в «Без ответа» с отметкой)
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

### Проход наверх и журнал (TASK-043)

Отдельный эмулятор умеет не только эмулировать: модуль `server_emulator/upstream/` даёт
собственный проход к настоящим серверам Яндекса и журнал трафика `state/traffic.sqlite3`.
Какие запросы пересылать, решают режимы маршрута устройств (TASK-044, `dispatcher.route_mode_for`
+ `upstream.target_for`).

- **Резолвер** (`upstream/resolver.py`, aiodns): внешние DNS-серверы из настройки
  (`upstream.dns_servers`, по умолчанию `77.88.8.8`, `1.1.1.1`) — колонки резолвят хосты
  Яндекса на сам сервер, системный DNS для пересылки не годится; закреплённые адреса
  (`upstream.upstream_ip`), кеш по TTL с защитой от полного отказа DNS. Петля — адрес,
  совпавший с адресами собственных интерфейсов сервера, → `LoopError`, пересылки нет.
- **Пересылка** (`upstream/forward.py`): собственная клиентская сессия с коннектором на
  резолвере (`use_dns_cache=False`), TLS к Яндексу с проверкой сертификата; заголовки —
  без hop-by-hop и перечисленных в Connection (`forward_headers`); тело запроса уже
  прочитано диспетчером и передаётся как есть; ответ буферизуется (лимит 32 МиБ).
  Мост WS: соединение колонки ↔ настоящий uniproxy, текстовые и бинарные кадры и
  ping/pong в обе стороны, закрытие с обеих сторон.
- **Журнал** (`upstream/journal.py`): обмены (время, SN/DeviceId, направление, хост,
  метод, путь, статус, размеры, длительность, источник ответа `emulator|upstream`,
  ключ события и признак «без обработчика»); что писать — опция устройства «Запись»
  (см. выше), для WS — lifecycle, AAPI-кадры и неизвестные кадры, звук не пишется.
  Тела сжаты `zstandard` с лимитом на тело; запись асинхронная, через очередь — поток
  ответа диска не ждёт (`flush()` дожидается при остановке и очистке); ротация по сроку
  и объёму — настройки `upstream.journal`.
- **API и UI**: домен `/api/traffic` — список с фильтрами (устройство/хост/источник/без обработчика),
  группы по устройствам (`?groups=1`) и записи группы (`?device_sn=&key=`), карточка обмена (заголовки; тела — JSON/protobuf текстом, если декодер есть в
  эмуляторе, иначе hex/base64), экспорт записи JSON, очистка журнала. Подвкладка
  «Журнал» в группе «Эмулятор» (после «Без ответа»).

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
| dulwich | git-операции без git CLI: версия рабочей копии (`version.py`) и самообновление (TASK-039/040). Выбор между pygit2 и dulwich: pygit2 требует нативные бинарники libgit2 под каждую платформу, dulwich — чистый Python, ставится pip везде; для операций тег/checkout/status его возможностей достаточно |
| ddgs, trafilatura, httpx | web_search / web_fetch |
| sherpa-onnx (внешний сервис) | Локальный ASR/TTS, см. `tools/builder_*.py` |

### Frontend

Vanilla JS (`server_emulator/ui/`) — UI эмулятора, без сборки.

## Кабинет пользователя и вход по ссылке (TASK-062)

Кабинет `/me` (`ui/me.html`) — страница самого пользователя: свои параметры, колонки дома, название и комната
колонки. Серверная часть — `ui/api/me.py`, монтируется в `mount_ui` на корне (`/me`, `/api/me/*`): путь
`/api/me/*` `auth_middleware` пускает под любой ролью со своей сессией (остальной админ-API для `user` закрыт).
Дом определяет принципал: пользователь — по `identities_user` сессии, админ и открытый режим — по `?user=`.
Во вкладке «Пользователи» у каждого пользователя есть кнопка «Кабинет»: `POST /emulator/api/me_link` при
включённом входе выдаёт ссылку `/me#t=<токен>`. Токен (в памяти, 12 ч, привязан к
пользователю) действует только на `/api/me/*` и `whoami` как сессия `role: user`, во фрагменте адреса — вне
логов; админ попадает в кабинет без логина, его собственная сессия не затрагивается. Выход админа и
`AuthStore.revoke_all` гасят токены. Контракт — `docs/contracts/http.md`.

## Версия и релизы (TASK-039)

Версия рабочей копии — `server_emulator/version.py`: git-описание через dulwich (последний
semver-тег `vX.Y.Z`, коммиты после тега — `X.Y.Z+N.g<sha>`, изменённые отслеживаемые файлы —
`dirty`); без `.git` (zip-установка) — из `.release-please-manifest.json`. Показывается в API
статуса и во вкладке «Дополнительно → Сервер». Релизы ведёт release-please (GitHub Action) по
Conventional Commits: PR релиза, `CHANGELOG.md`, тег `vX.Y.Z`, GitHub Release с заметками.
Установка — `git clone` скриптами `install.bat`/`install.sh` (editable-установка: после переключения
тега код подхватывается без переустановки); данные пользователя — только в игнорируемых путях
(`server_emulator/state*/`), работа сервера оставляет `git status --porcelain` пустым.

## Самообновление (TASK-040)

Обновление только по клику, автоматически ничего не ставится. Проверка — GitHub Releases API
(`self_update/check.py`: `/releases/latest` для stable, `/releases` для канала beta, ETag/If-None-Match,
ошибки сети — в статус, не в исключения; приватный репозиторий без токена — 404 и пометка
«недоступен»). Сравнение версий — `packaging.version.Version` против `version.py`. Цикл проверки
живёт на Runtime (`runtime.self_update`, старт с задержкой 45 с, затем раз в `interval_hours`).

Возможность обновления (`self_update/repo.py`, dulwich): git-клон с origin, совпадающим с
настройкой `self_update.repo`, без правок в отслеживаемых файлах (untracked и ignored не мешают),
не контейнер. Обновление — отдельным процессом `self_update/updater.py` (копируется в
`state/update/`, чтобы замена файлов рабочей копии не задевала исполняемый код updater'а):
ждёт выхода сервера (код `UPDATE_CODE` = 76 в `server_config.py`), бэкапит данные `state/` в zip
с ротацией до 5, fetch тегов и переключение на тег (ветка релизов — fast-forward через
`reset --hard`, иначе checkout detached), при изменении pyproject между релизами —
`pip install -e ".[emulator]"`, запуск сервера и ожидание `/__control/status` с новой версией.
Сбой любого шага — возврат на прежний коммит, повтор шага зависимостей, запуск; итог ok/failed —
в `state/update/plan.json`, лог — `state/update/update.log`.

Кто поднимает сервер после обновления (`launch` в плане): под треем Windows (`ALICA_LAUNCHER`) —
трей: код UPDATE_CODE он не перезапускает, ждёт план, затем поднимает сервер и перезапускает себя
(новый трей стартует с `ALICA_TRAY_TAKEOVER` — ожидание освобождения mutex одного экземпляра);
под менеджером служб (настройка `restart: manager`, systemd `INVOCATION_ID`) — менеджер; иначе
updater сам.

API `ui/api/self_update.py`: GET — статус (current/latest, can_update и причины, blocked_files,
last_check, error, last_update, previous, installed_kind git|zip); POST `{action}` — `check`,
`update`, `stash_update` (правки → refs/stash по клику), `rollback` (предыдущий тег тем же путём),
`skip` (пропустить версию), `snooze` («Позже» до следующей проверки). UI — кнопка в навигации при
новой версии (одна пульсация, `prefers-reduced-motion` её отключает) — ручной режим, от галочки
автообновления не зависит; окно `<dialog>` с заметками релиза (текстом, без markdown), причинами
блокировки и кнопками. В форме настроек («Дополнительно → Сервер», блок «Обновление») — только
галочка автообновления и канал; остальные поля `self_update` не редактируются (дефолты).

## Протокольный слой: вкладка «WS» (TASK-058)

«Эмулятор» разделён по смыслу: «Сценарии» — намерения пользователя (фразы, шаблоны), «WS» —
всё, что приходит и уходит по протоколу колонки: «Входящие» — реестр обработчиков
(`aapi_handlers.py`: источник builtin/callback/extension, счётчики пришло/обработано/последний),
«Колбэки» — служебные колбэки: маршрут (`aapi_handlers.route`) → внутреннее событие-запрос →
сценарий-поставщик (`kind: provide`, пользовательский перекрывает встроенный; без поставщика —
тихое подтверждение), «Отправка» — ручные push-директивы (переехали из «Устройства → Действия»);
сверху — свёрнутый блок «AAPI без ответа». API — `ui/api/ws_handlers.py`, UI — `ui/ws.js`.
Триггер `kind: event` сохранён для расширений и сценариев из промахов; встроенные колбэки
переведены на `provide`.

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
