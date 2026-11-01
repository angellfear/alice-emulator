---
title: "Контракт: сценарный движок, каталог и реестр"
status: stable
date: "2026-10-24"
module: "server_emulator/scenario_engine/"
---

# Контракт: сценарный движок, каталог и реестр

Дата: 2026-10-24. Всё, что ниже — CODE_TESTED (автотесты в `tests/`); физическая приёмка на колонке не проводилась.

## 1. Порядок ответа (приоритет)

```
входящая реплика/событие
  └─ агент ждёт «да/нет» на своё действие? → ответ агенту (движок graph, docs/contracts/agent.md)
  └─ ожидающий диалог (ask-уточнение)? → продолжение своего сценария (continuation раньше роутинга)
  └─ probe (side-effect-free, H02): supported / unsupported / ambiguous / rejected + reason code
       └─ supported → executor сценария (без обращения к AI-модели)
       └─ иначе → AI-модель (chat) с tool_calls alice_action (если settings.model_actions)
```

Агент (движок graph) знает каталог: индекс сценариев в системном промпте, инструменты
`scenario_list/get/probe/run`. Поле сценария `risk` (`read|device|external|config|destructive`,
по умолчанию `device`) задаёт, нужно ли подтверждение, когда сценарий запускает агент.

Каталог builtin включается `settings.builtin_scenarios`, отдельные встроенные сценарии — `settings.builtin_disabled` (аддитивная миграция дефолтов доносит ключи в старые БД). Выключенные (`enabled:false` / id в `builtin_disabled`) из роутинга исключаются.

## 1a. Поставщики данных для колбэков

Служебные колбэки колонки маршрутизируются во внутренние события (`informers.requested`,
`timesync.requested`, `callbacks.ack`, … — aapi_handlers.py), ответ собирает сценарий-поставщик
с триггером `{"kind": "provide", "event": …}`. Пользовательский сценарий с тем же событием
перекрывает встроенного поставщика; поставщика нет — колбэк отвечает умолчанием (тихое
подтверждение). Триггер `kind: event` сохранён для расширений и сценариев из промахов.

## 2. Встроенные семейства (без AI)

| id | фразы | исполнение | тесты |
|---|---|---|---|
| builtin.clock.get_time | «сколько времени», «который час» | local_time_phrase, карточка | test_builtin_scenarios |
| builtin.timer.set | «поставь таймер на N…» | планировщик (persist, dedupe, missed-after-restart), уточнение при отсутствии длительности | test_timers |
| builtin.timer.cancel | «отмени таймер» | отмена таймеров SN (будильники не трогает) | test_timers |
| builtin.alarm.set | «поставь будильник на HH:MM» | zoneinfo/DST, rollover, snooze, пропуски | test_alarms |
| builtin.alarm.cancel | «отмени будильник» | отмена будильников SN | test_alarms |
| builtin.light.switch | «включи/выключи свет (комната)» | state.light устройства; нет ламп — честный отказ | test_light |
| builtin.volume.set | «громче/тише/громкость на N/максимум/выключи звук» | Push sound_set_level/louder/quiter в сессию SN | test_simple_commands |
| builtin.app.open | «открой/запусти X» | резолв по state.apps; запуск НЕ поддержан (wire не восстановлен) — честная карточка | test_simple_commands |
| builtin.weather.get | «погода…» | внешний провайдер (Open-Meteo-совместимый), кеш TTL, недоступность честно | test_weather |

Passport provenance — `scenario_engine/builtin.py::BUILTIN_PROVENANCE` (перенесено/адаптировано/неподдержано).

## 3. Executor / flow

Операции: `resolve/action/compose/ask/finish/error` (контракты `FLOW_OPS`, граф-валидация: ссылки next/on_failure, достижимость, выход к терминалу, fallthrough по списку). Ограничения: 64 шага, deadline 300 c, TTL ожидания. Action ID → идемпотентность side-effect. awaiting-диалоги персистентны (StateStore.executions), переживают рестарт. Композеры: `builtin.reply@1` (текст из слота/состояния — база для пользовательских сценариев-ответов).

## 3a. Возможности JSON-сценария (S01+)

**Триггеры phrase**: `name` (слово/предложение) и/или `phrases:[]` (список), режим `match`:
`contains` (по умолчанию — вхождение), `exact` (равенство), `mask` (глоб `*`/`?` по нормализованной
реплике целиком, `*погод*` — вхождение по маске). Матчинг идёт после нормализации ASR
(`normalize.py`: регистр/ё/wake-слово/разглей/однобуквенные огрехи).

**Guard — право отказа**: `guard` на сценарии — декларативное условие над контекстом:
`{all|any|not: …}` и листья `{exists|eq|ne|in: [binding, значение]}`. Условие не сошлось
(например, нет гео пользователя) — сценарий ОТКАЗЫВАЕТСЯ, реплика уходит следующему кандидату
по порядку каталога или в LLM; в отчёте reason `guard_declined:<id>`.

**Контекст запроса**: bindings `{"source":"user","field":"…"}` и `{"source":"device","field":"…"}`
(точечный путь: `location.latitude`, `fields.city`). У пользователя — `location`
({latitude, longitude, city}, профиль) и произвольный `fields` (≤16 КиБ); у устройства —
`room` (комната колонки: «включи свет» без уточнения) и `fields`. Погода берёт координаты
из гео пользователя, при его отсутствии — из settings.weather.

**compose.reply — ответ без кода**: `{text|speech (строка|binding), speech_prefix, card,
directives:[{name, type?, analytics_type?, is_led_silent?, payload (Struct; в листьях
bindings), payload_raw (base64 — реплей перехваченного wire)}]}`. Реплика озвучивается
голосовым трактом (TTS); `HasVoiceResponse` поднимается только при реальном аудио, как облако.

## 4. Каталог API (`/emulator/api/scenarios`)

- GET → `{scenarios, builtin, builtin_enabled}`; пользовательские — полный рабочий объект (`_group` — путь каталога «clock/watches», любая глубина; `enabled`); `builtin` — полные определения + `title`/`description` (ru/en, `builtin.py::BUILTIN_META`) + `catalog:"builtin"` + индивидуальный `enabled` (главный флаг И id ∉ `builtin_disabled`) + `provenance`.
- PUT `{scenario}` → валидация (формат §4a) → сохранить (замена по id).
- PUT `{id, enabled}` → индивидуальный переключатель без редактора: пользовательские — флаг сценария; `builtin.*` — список `settings.builtin_disabled` (роутинг уважает оба уровня).
- DELETE `?id=` → удалить.
- POST `{text}` → dry-run без устройства: `{handled, verdict?, reason, scenario?, status?, trace}`.

## 4a. Файл на сценарий и поля определения

Каждый сценарий — отдельный JSON-файл `state/scenarios/<id>.json` (полный рабочий объект, включая `enabled`/`_group`). При старте непустой каталог файлов — источник правды (определения можно править при остановленном сервере); каждое сохранение/удаление синхронизирует файлы (запись изменённых, удаление лишних); существующие сценарии выгружаются в файлы при первом старте (миграция). Битые файлы пропускаются, остальные работают.

Опциональные поля определения (валидатор `scenario_packages/validate.py`; формат v4 §10 без изменений для format_version/id/version/triggers/slots/flow/dependencies/publication):

- `title`, `description` — строка или `{ru, en}`; пустая строка внутри = перевода нет; UI показывает ru → en → id/фразы.
- `executor` — декларативный выход: `{kind: reply|flow|mcp_stdio|script|app}` + `server/tool/args/ref/entrypoint/note`. Исполняются движком только `reply`/`flow`; `mcp_stdio`/`script`/`app` — объявление привязки (хранится и показывается; инлайн-код по-прежнему запрещён правилами пакета). Реальное исполнение — будущая работа (см. plan-index).

UI каталога: плоские группы-каталоги (`builtin`, путь `_group`, «без раздела») — без вложенных аккордеонов; галочка каждого сценария и группы (группа `builtin` — мастер-флаг; включение снимает индивидуальные отключения; смешанные состояния — indeterminate). «Редактировать» открывает окно-редактор (`<dialog>`); встроенного (и сценария расширения) — окно с черновиком копии `user.<имя>`: на диск и в настройки ничего не пишется, «Сохранить» записывает копию (с кодом `.py` у py-сценариев: `GET scenarios?script=<id>` → `PUT {scenario, script}`), затем выключает встроенный; «Отмена» без следов. Установка пакетов сценариев — «Дополнительно → Расширения» (кнопка-ссылка в панели каталога).

## 4b. Правка агентом

Код: `server_emulator/agent/scenario_edit.py`, API `scenario-drafts`, панель «Черновики и предложения
агента» во вкладке «Сценарии» (`ui/scenario_drafts.js`).

Цикл: `scenario_draft` (копия существующего или новый JSON; встроенный → `user.<id>`) →
`scenario_patch` (JSON Patch RFC 6902, ошибка валидатора возвращается модели) → `scenario_test`
(фразы черновика + фразы других сценариев с добавочным словом: не перехвачены и не стали
неоднозначными) → `scenario_activate` (класс `config`, подтверждение) → `scenario_history` /
`scenario_revert`.

Черновики — `state/scenario_drafts/<draft_id>.json` (в роутинг не попадают), версии —
`state/scenario_history/<id>/<n>.json` (прежнее определение перед каждой активацией и откатом).
Активация встроенного копией выключает встроенный (`builtin_disabled`).

Каталог общий для всех колонок: агент в диалоге с пользователем включает только **личный**
сценарий — с guard `{"eq": ["{{user.id}}", <user>]}` (по умолчанию `scenario_draft` ставит его
сам); общий получает статус `proposed` и включается администратором в панели. Откат общего —
только в панели. В студии (без пользователя) агент правит каталог целиком, после подтверждения.

API: `GET scenario-drafts` → `{drafts}`; `POST {op: test|activate|discard|history|revert}`.

## 5. Реестр пакетов — каталог расширений

Отдельного API `registry` больше нет: пакеты сценариев ставятся тем же механизмом, что и расширения
(`docs/contracts/extensions.md`, `server_emulator/extensions/`).

- Манифест прежнего формата `scenario-registry` — `{format, packages:[{id, version, name, path?, url, sha256?}]}` —
  читается каталогом расширений как обычный источник-каталог: пакет → запись каталога (`id`, `version` приводится к
  semver, `name` → описание). `path` (подраздел `_group`) слоем расширений не поддержан — раздел по `category`.
- Установка записи — пакет `scenario-package` (JSON или zip-бандл с `package.json`) разворачивается в data-расширение
  (`extension.json` + `scenarios/<category>/<id>.json`); дальше включение, обновление, откат, удаление — штатные.
  Защиты прежнего реестра сохранены: пакет ≤4 МБ **того же origin**, что и каталог, sha256-пин, traversal в zip.
- Миграция при старте (`extensions/legacy_registry.migrate_registry`, один раз): `settings.registry.url` → источник
  в `settings.extensions.sources` (если подходит: https или loopback), каждый пакет из `registry.installed` →
  расширение (версии журнала лежат на диске, последняя активна, предыдущая — для отката), нетронутые копии
  сценариев пакета убираются из пользовательских, изменённые и выключенные остаются и перекрывают расширение;
  ключ `registry` удаляется.
- `tools/make_registry.py` по-прежнему публикует каталог в этом формате; его URL добавляется как источник расширений.

## 6. Карточка диалога (каркас реальных ответов)

Каждый исходящий AliceResponse финализируется (`response_compose.finalize`): `Cards[i].CardId` (новый на карточку), `Header.SequenceNumber` (монотонный счётчик runtime), `Header.DialogId=""`, директива `tts_play_placeholder {channel:"Dialog"}`. Громкость из модельных действий — директива `sound_set_level {new_level: float}` (wire из перехвата).

## 7. NO_SPEECH (wire реального сервера, по захвату)

Клиент закрыл микрофон `CloseReason=NO_SPEECH` (шёпот: локальный VAD колонки) → сервер: финальный пустой AsrResult → закрытие канала **OK** → реплика «Извините, я не расслышала…» (карточка + голос/TTS). Серверный no-speech — тот же wire.

## 8. Не поддержано (честно)

- Исполнение `executor.kind = mcp_stdio|script|app` — поле декларативное, привязка хранится, но не запускается (будущая работа, см. plan-index).
- Запуск приложений физически (TSendAndroidAppIntentDirective не восстановлен).
- TTS-озвучка срабатываний таймеров/будильников при выключенном aapi_tts.
- Дни недели будильника голосом; snooze голосом (API планировщика есть).
- Прогноз погоды на дни (только current); локация по GPS устройства.

## 9. Медиа: музыка, сказки, радио, плеер

Сценарии раздела `media` включают файлы из библиотеки (`state/media/music|tales`) и радио из
настроек, управляют плеером; директива `audio_play` (Struct) и очередь с колбэками плеера —
в отдельном контракте `docs/contracts/player.md`. SDK для py-сценариев — `ctx.media`,
`ctx.library`, `ctx.media_base` (scripts-sdk.md, 1.3.1).
