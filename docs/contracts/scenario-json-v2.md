---
title: "Сценарий JSON v2 — контракт"
status: stable
date: "2026-10-31"
module: "server_emulator/scenario_engine/parsers.py"
---

# Сценарий JSON v2 — контракт

Принцип: **сценарий целиком описан JSON** — триггеры, колбэки, словари,
условия, данные, ответ, директивы. **Код — только примитивы**: матчер,
движок условий, шаблонизатор, кодировщик protobuf по дескриптору, HTTP-источник,
хранилище, эффекты (таймер, push, свет). В коде нет пользовательских текстов,
словарей фраз и выбора веток по тексту.

## Расположение и разделы

`server_emulator/scenarios/<category>/<name>.json` — один сценарий на файл.
Общие словари — `server_emulator/scenarios/lexicons.json`.

| category | раздел |
|---|---|
| `system` | Системные (служебные колбэки, синхронизация времени, сброс диалога) |
| `informers` | Информеры (погода: голос/виджет/тап, время) |
| `device` | Управление устройством (громкость, плеер, Bluetooth, LED, приложения) |
| `smart_home` | Умный дом (свет) |
| `timers` | Таймеры и будильники |

Пользовательские сценарии (UI «Сценарии») — `state/scenarios/*.json`, тот же
формат, `category` любая из списка (по умолчанию `custom`).

## Верхний уровень

```json
{
  "format_version": 2,
  "id": "informers.weather.widget",
  "version": "2.0.0",
  "category": "informers",
  "title": {"ru": "…", "en": "…"},
  "description": {"ru": "…"},
  "priority": 100,
  "fallback": false,
  "triggers": [ … ],
  "guard": { … },
  "lexicon": {"rooms": {"кухне": "кухня"}},
  "tables": {"season": {"1": "winter"}},
  "flow": [ … ],
  "provenance": { … }
}
```

- `priority` — меньше = раньше (по умолчанию 100). Равные — по id.
- `fallback: true` — сценарий рассматривается **после** всех остальных
  (catch-all, например тихое подтверждение служебных колбэков).
- `lexicon`/`tables` — данные сценария; `lexicon` можно ссылать по имени из
  общего `lexicons.json` (в py — `ctx.lexicon`, `ctx.parse_*`).

## Триггеры

```json
{"kind": "phrase", "phrases": ["какая погода", "погода"], "match": "contains|exact|mask"}
{"kind": "event", "name": "@@mm_typed_callback",
 "callback": "NAlice.TGetInformersTypedCallback",
 "purpose": "get_informers", "frame": "weather_semantic_frame"}
```
У `event` фильтры `callback`/`purpose`/`frame` необязательны; сравниваются с
полями служебного запроса (`input.callback`, `input.purpose`, `input.frame`).

`provide` — сценарий-поставщик данных внутреннего события: колбэк-обработчик
принимает событие колонки и публикует «нужны данные» (`informers.requested`, `timesync.requested`,
…), поставщик собирает ответ колонке.

```json
{"kind": "provide", "event": "informers.requested"}
```

Пользовательский поставщик с тем же `event` перекрывает встроенный (приоритет — как у
сценариев); поставщика нет — колбэк отвечает своим умолчанием (тихое подтверждение).
Триггер `kind: event` остаётся для расширений и сценариев, созданных из промахов;
встроенные колбэки переведены на `provide`.

## Триггер template (hassil)

Полное совпадение фразы по шаблону hassil (Home Assistant) — вместо `contains`
плюс разбор сущностей: сущности шаблона сразу становятся slots. Шаблонное совпадение
приоритетнее phrase-триггеров внутри своего слоя (полная фраза — самый строгий сигнал),
phrase остаются fallback'ом; порядок слоёв — ниже; reason код — `template`.

```json
{"kind": "template",
 "sentences": ["[поставь] [таймер] (на|в) {minutes} [минут]", "таймер на {minutes} минут"],
 "lists": {"power": {"values": [{"in": "включи", "out": "on"}, {"in": "потуши", "out": "off"}]}}}
```

- синтаксис hassil: `(а|б)` — альтернативы, `[…]` — необязательное, `{имя}` — ссылка
  на список, `{имя}` с `"wildcard": true` — свободный хвост. Ошибка синтаксиса и
  ссылка на несуществующий список отсекаются валидатором при сохранении (с номером фразы).
- `lists` триггера перекрывают общие списки источников по имени; значения совпавших
  списков и fixed-`slots` данных интента → `slots` сценария (числа range-списков
  приводятся к int: hassil отдаёт цифры float «5.0», слова — int «5»).
- компиляция: шаблоны всех сценариев собираются один раз при загрузке каталога
  (`scenario_engine/templates.py`, `Intents.from_dict`), перекомпиляция при сохранении
  сценария; матчинг — `TemplateIndex.match(text) → (scenario_id, slots)`, диалоговый
  слой кладёт слоты в `ctx.slots` (JSON-сценарий видит их как `slots.*`).
- списки и `skip_words` собираются из нескольких источников-словарей
  (`merge_lexicons`): встроенный `lexicons.json` (раздел `lists`; `skip_words` —
  `asr.wake_words` + `asr.trailing_fillers` + `asr.skip_words`), расширения,
  пользовательские. Конфликт имён списков — поздний источник перекрывает ранний:
  пользовательский > расширение > встроенный.
- порядок кандидатов послойный: свой слой (пользовательские сценарии и расширения)
  целиком раньше встроенного — как в каталоге (пользовательская запись перекрывает
  встроенную); внутри слоя шаблонное совпадение приоритетнее phrase-триггеров.
  Сценарий с несколькими совпавшими предложениями входит в кандидаты один раз,
  с слотами первого (самого частного) предложения.
- шаблоны пишутся без «ё» (hassil сворачивает «ё» только в реплике) и с запасным
  хвостом `[{query}]` там, где у фразы бывает продолжение, — иначе общий шаблон
  другого сценария перехватит реплику раньше phrase-fallback'а.

### Общие списки (`scenarios/lexicons.json` → `lists`)

| список | значения | слот |
|---|---|---|
| `number` | 0…9999 цифрами и словами | число |
| `minutes`, `hour`, `minute` | range для времени/минут | число |
| `unit`, `duration_unit`, `duration_unit2` | секунд/минут/часов → множитель в секундах | число |
| `duration_fixed` | «полминуты», «полчаса», «полторы минуты», «полтора часа», «час», «минуту», «секунду» | секунды |
| `duration_number`, `duration_number2` | 1…999 (композит «2 часа 15 минут») | число |
| `ordinal` | «первый…десятый» → 1…10, «последний» → -1 | индекс |
| `day_period` | «утра/дня/вечера/ночи» | слово |
| `time_word` | «полночь» → «00:00», «полдень» → «12:00» | «ЧЧ:ММ» |
| `date_word` | «сегодня», «завтра», «послезавтра» | слово |
| `weekday` | дни недели → 1…7 (пн=1) | число |
| `day`, `month` | «8 марта»: день 1…31, месяц родительным падежом → 1…12 | числа |
| `room` | кухня/спальня/гостиная/ванная/коридор/детская (падежи) | нормализованное имя |
| `media_object` | музыку/песню/трек/композицию/альбом/плейлист/подборку/сборник/дискотеку | слово |
| `volume_mode`, `volume_fixed` | громче/тише…; «выключи звук», «на максимум»… | up/down/mute/max |
| `player_command` | следующий/предыдущий/продолжи/что играет… (синонимы справки) | next/prev/resume/what |
| `led_emotion` | «глазки», «глаза» | joy |
| `app`, `query` | wildcard — свободный хвост (приложение, исполнитель, текст) | строка |

Длительность одним списком hassil не выражается — в шаблонах она пара
`{duration_number} {duration_unit}` (с добавкой `…2`, «2 часа 15 минут») плюс
`{duration_fixed}`; секунды считает py-сценарий. Время — `{hour} [{minute}]
[{day_period}]` или `{time_word}`, дата — `{date_word}` / `[в] {weekday}` /
`{day} {month}`. Список триггера с тем же именем перекрывает общий.

Примеры (таблица проверки):

| Фраза | Совпадение |
|---|---|
| «поставь таймер на пять минут» / «на 5 минут» | `minutes=5` |
| «таймер на двадцать пять минут» | `minutes=25` |
| «потуши свет в спальне» | `power=off, room=спальня` |
| «поставь будильник на семь часов тридцать минут» | `hour=7, minute=30` |
| «открой ютуб» | `app="ютуб"` (wildcard) |
| «алиса включи пожалуйста свет» | `power=on` — шум снимают `skip_words` |
| «свет включи на кухне» | `power=on, room=кухня` — перестановка `({power}; свет)` |
| «поставь таймер на полчаса» | `duration_fixed=1800` |
| «поставь таймер на 2 часа 15 минут» | `duration_number=2, duration_unit=3600, duration_number2=15, duration_unit2=60` |
| «громкость на семь» | `number=7` |
| «включи майкла джексона» | `query="майкла джексона"` (wildcard) |
| «будильник на 7:30 завтра» | `hour=7, minute=30, date_word="завтра"` |
| «выключи второй таймер» | `ordinal=2` |

## Контекст (корни шаблонов и bindings)

`input` (text, sn, purpose, callback, frame, timezone, request_id…), `slots`,
`state`, `user` (id, name, location, fields), `device` (sn, name, model,
room, display, lights, apps, platform, fields), `clock` (now, hour, minute,
month, weekday, epoch), `settings` (weather…), `store` (`store.user.*`,
`store.device.*`, `store.system.*` — данные сценария), `lexicon`, `tables`,
`animations`, а также:
- `params` — параметры из БД (docs/contracts/params-db.md):
  `params.user.*` (о пользователе: name, location.*, timezone),
  `params.device.*` (о колонке: name, aliases, room, location.*, timezone),
  `params.platform.*`, `params.server.*` (weather.url, weather.max_age_hours,
  weather.refresh_timeout, timezone) и `params.effective.*` — итог цепочки
  колонка → пользователь → платформа → сервер → default для location/timezone
  и прочих ключей без префикса (`params.effective.weather.max_age_hours`).
- `home.devices` — колонки владельца: `{sn, name, aliases, room, platform,
  model, online, current}` — адресация команд между колонками
  («поставь будильник на дуо 32»: `ctx.find_device` / `parse_list` по `name/aliases`,
  `contains_only`, затем `sn` в аргументах эффекта).

Шаблон: `"{{slots.weather.current.temperature_2m | signed}}"`. Фильтры:
`plural`, `time_ru`, `hhmm`, `duration`, `signed`, `round`, `lookup:<table>`,
`default:<v>`, `lower`, `json`, `hour_label`, `div:<n>`, `mul:<n>` (один
фильтр на подстановку, цепочек нет).
Binding: `{"source": "path", "path": "slots.weather.current.temperature_2m"}`
возвращает значение без приведения к строке.

## Шаги flow

Политика: **JSON — для простого ответа и одного действия**. Сложное (данные, разбор
фразы, ветвление, состояние) — только py (раздел «Исполнитель script», SDK).

Актуальные шаги — их можно использовать в новых сценариях:

| op | назначение |
|---|---|
| `action` | эффект-примитив: `{"ref","arguments","slot","on_failure"}` |
| `compose` | ответ: `{"reply":{…}}` или отладочный `{"raw":{…},"debug":true}` |
| `ask` | уточнение: `{"await","question","next"}` |
| `finish` / `error` | терминалы |
| `resolve` | служебный: значение из резолвера в слот (`{"ref","slot"}`) |

### Удалённые шаги

`extract`, `load`, `refresh`, `fetch`, `branch`, `set`, `store`, `wait_state` **удалены**: код исполнителя и контракты валидатора убраны. Сценарий с таким шагом не проходит `validate_scenario`
(при сохранении и при загрузке) с ошибкой «шаг <op> больше не поддерживается — перенесите в py».
Имена шагов остаются в `validate.LEGACY_OPS` только ради этого сообщения. Параметры `strict` и
`previous` у `validate_scenario` (`existing` у `validate_package`) приняты ради совместимости вызовов
и ничего не меняют.

Агент (`scenario_draft`/`scenario_patch`) предлагает только простые JSON — `compose / action / ask /
finish / error`; py-сценарии пишет администратор.

Чем заменить шаг в py:

| было | в py |
|---|---|
| `extract` | `ctx.parse_lexicon / parse_list / parse_number / parse_duration / parse_time / parse_rest`, `ctx.find_in` |
| `load`, `refresh` | `ctx.dataset`, `ctx.dataset_record` |
| `fetch` | `await ctx.fetch_json` |
| `branch` | обычный `if` |
| `set`, `wait_state` | `ctx.state`, `ask` |
| `store` | сервисы хранилища SDK |

Размеры кода до/после вывода — `docs/architecture.md` (раздел «Исполнитель сценариев»). `bindings.py` и
`parsers.py` остались: `bindings` раскрывает `{{state.*}}`/фильтры/`$each` в ответах py-сценариев, `parsers` —
это `ctx.parse_*` SDK.

Эффекты (`action.ref`): `timer.start {seconds,label,sn?}` → `{id,seconds,label,delivery}`,
`alarm.start {time,label?,timezone?,sn?}` → `{id,time,delivery}`,
`timer.cancel {kind,sn?}` → число отменённых (0|1),
`device.push {action,arguments,sn?}`, `light.set {room,on,brightness?}`
→ `{on,brightness}`. `sn` — колонка-адресат (по умолчанию — та, что спросила).
С TASK-079 `timer.*`/`alarm.*` — не серверное расписание, а отправка колонке штатных
директив (`timer_set_directive`, `alarm_add_directive`, `timer_cancel_directive`):
`delivery: "sent"` в живую WS-сессию, `"no_session"` без неё; состояние таймеров
и будильников сервер не хранит (оно в запросе колонки).

## Исполнитель script (py-сценарии)

Сценарий без `flow` с полем `"executor": {"kind": "script"}`: триггеры, guard и
слоты описываются как обычно, логика — кодом `handle(ctx)` в файле `.py` рядом
с JSON:

- пользовательский сценарий — `state/scenarios/<id>.py` (рядом с `<id>.json`);
- встроенный — `server_emulator/scenarios/<раздел>/<id>.py`.

Допустимые значения `executor` — `{"kind": "flow"}` (эквивалент отсутствия поля)
или `{"kind": "script"}`; для `script` поле `flow` не допускается (валидатор:
«flow не нужен для executor script»). Код никогда не кладётся внутрь JSON —
`"script"` остаётся в `FORBIDDEN_KEYS` валидатора; наличие файла валидатор не
проверяет (это делают API при сохранении и `probe` при выборе кандидата:
нет файла — `script_missing`, сценарий роутером не выбирается).

`handle(ctx)` получает контекст SDK — `ScenarioContext` (полный API:
docs/contracts/scripts-sdk.md: построители `say`/`card`/`directive`/
`silent`, сервисы `ctx.action`/`ctx.fetch_json`/`ctx.dataset`/`ctx.lexicon`,
`ctx.slots`/`ctx.state` — живые dict'ы исполнения) — и возвращает:

| Вернул | Исход |
|---|---|
| построитель ответа / равнозначный dict — спецификация reply из раздела «Ответ» | ответ через тот же compose, что JSON-шаг compose (карточка, директивы, `speech_parts`) |
| список таких спецификаций | несколько сообщений по порядку |
| `None` | **отказ** (новый статус `declined`): реплика уходит следующему кандидату, при отсутствии кандидатов — режим «Интеллект» (как `guard=false`) |
| `ask(...)` | уточнение: ожидание как у JSON ask (StateStore.executions), продолжение — функция `then(ctx, answer)` того же модуля; отмена («отмена», «стоп») и TTL завершают; ShouldListen |
| иное | `errored: script_result:<тип>` |

Ошибки (исход `errored`, запись в trace `{"step": "script", "op": "script",
"status": "error"}`): нет файла — `script_missing:<id>`; исключение скрипта,
`ScriptError`, таймаут — `script_failed:<текст>` (последние строки трассировки).
Каждый исход `errored` попадает в журнал `recent` (`{"kind": "scenario", "id",
"error"}`), кроме прогона с dry-run — проверка сценария журнал не засоряет.

Dry-run (проверка фразы, POST `/api/scenarios`): `ctx.dry_run = True` —
`ctx.action` действие не выполняет, а пишет `{kind, name, arguments}` в
`ctx.effects`; `effects` видны в отчёте проверки. JSON-сценарии ведут себя
как раньше.

Таймаут (10 с, `ScriptRunner`) действует **только на async `handle`**:
синхронный код выполняется в цикле событий, как у эндпоинтов, и блокирует
сервер — поэтому сетевой код писать async (`await ctx.fetch_json(...)`).

## Данные плагинов: датасеты, свежесть, обновление

Внешние данные (погода и т.п.) сценарий не качает сам — он читает их из БД
(датасет = `scenario_data`, `scenario_id = dataset:<имя>`, scope `system`,
`updated` — время обновления) и при необходимости пинает плагин. Это делает py-сценарий:
`ctx.dataset(name, key, max_age, args, timeout)` и `ctx.dataset_record(...)` (запись с `updated`/`age`).
JSON-шагов `load`/`refresh` больше нет.

- Плагин `plugins[name](key, args)` возвращает значение, `Executor.refresh_dataset` пишет его в датасет.
  Таймаут — исключение `asyncio.TimeoutError`, плагин дорабатывает в фоне (данные появятся к следующему
  запросу); параллельные запросы одного ключа ждут одну задачу плагина.
- Плагины — код (`runtime.scenario_plugins`). `weather` пока ЗАГЛУШКА: один
  GET Open-Meteo по `args.url/args.query`.

## Ответ (compose.reply)

```json
{"envelope": "dialog|service",
 "text": "…", "speech": "…", "card": true,
 "directives": [
   {"name": "update_all_informers_directive", "analytics_type": "update_all_informers",
    "is_led_silent": true,
    "proto": "NAlice.TInformersCapability.TUpdateAllInformersDirective",
    "fields": {"WeatherInformerState": {"Temperature": "{{…}}"}}},
   {"name": "…", "payload": {"Struct": "…"}},
   {"name": "…", "payload_raw": "base64", "debug": true}
 ],
 "speech_parts": ["Сейчас", "{{…|signed}}", "Сегодня от", "…"]}
```
- `speech_parts` — озвучка частями с файловым кешем (`state/tts_cache`, ключ —
  движок/голос + текст части): постоянные слова и числа синтезируются один раз,
  части склеиваются паузой 60 мс. Без `speech_parts` вся реплика сценария — одна
  кешируемая часть («Включаю.», «Выключаю.»). Текст речи для протокола —
  `speech_prefix` + части через пробел.
- `envelope: dialog` — каркас диалогового ответа облака (tts_play_placeholder,
  SequenceNumber, CardId); `service` — тихий служебный ответ (ResponseId,
  пустые EffectiveSettings, Timings.SendingTime), без голосовых добавок.
- `proto` + `fields` — payload собирается по дескриптору (поля по именам proto,
  значения — литералы/шаблоны/bindings, типы приводятся по дескриптору).
  `{"$json": {...}}` внутри fields — сериализуется в строку JSON (div2-карточки:
  `Div2Card.StringBody`).
- `payload_raw` — только с `"debug": true` (отладка, реплей перехвата).

## Сырой protobuf (отладка)

```json
{"op": "compose", "debug": true,
 "raw": {"schema": "alice-api", "proto": "NAlice.NAliceApi.TServerMessage", "fields": {…}}}
```
Сообщение целиком собирается из fields и уходит как есть (диалоговый слой
проставляет только RefMessageId). Без `debug` валидатор отклоняет.

## Хранилище данных сценариев

Таблица `scenario_data(scenario_id, scope, owner, key, value, updated)`:
`scope=user` — owner = владелец колонки; `device` — SN колонки;
`system` — общие данные (owner пустой). Чтение — `store.<scope>.<key>` в
контексте (загружается перед прогоном), запись — сервисы SDK.
API: `GET/DELETE api/scenario-data?scenario=…`.
