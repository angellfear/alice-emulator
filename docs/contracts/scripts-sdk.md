---
title: "SDK py-сценариев и расширений"
status: draft
date: "2026-10-30"
module: "server_emulator/sdk.py"
---

# SDK py-сценариев и расширений

Код: `server_emulator/sdk.py`. Тесты: `tests/test_sdk.py`.

## Обзор

Публичный API py-скриптов (сценарии, py-эндпоинты и расширения):
контекст запроса `ScenarioContext`, построители ответа и сервисы — тонкие обёртки над
движком сценариев. Скрипт не лезет во внутренности Runtime; SDK ничего не дублирует:
ответ собирает существующий `reply_compose.compose_reply`, действия/данные — реестры
`Executor` (`runtime.scenario_executor`), новых сетевых клиентов нет.

## Версия

`SDK_VERSION = "2.3.0"` — semver. **Ломающие изменения API — только со сменой major**
(удаление/переименование/смена сигнатур, смена формы построенных dict'ов). Добавление
полей и новых функций — minor, исправления поведения — patch. Расширение объявляет, с
какой версией SDK оно совместимо (поле `requires.sdk`).

## Контракт

- `handle(ctx)` получает `ScenarioContext` и возвращает:
  - **построитель ответа** (`say`/`card`/`directive`/`silent`) или равнозначный dict —
    спецификацию reply в формате JSON-шага `compose` (docs/contracts/scenario-json-v2.md,
    раздел «Ответ»); каркас protobuf собирает `compose_reply`, доставку и каркас ответа
    (CardId, SequenceNumber, tts_play_placeholder, TTS-кеш) — диалоговый/голосовой слой,
    как для JSON-сценария;
  - **список** таких спецификаций — несколько сообщений по порядку;
  - **`ask(...)`** — вопрос с продолжением диалога;
  - **`None`** — сценарий отказался (как `guard=false`): реплика уходит следующему
    кандидату или в режим «Интеллект»;
  - **`[]`** (1.1.0) — сценарий отработал **без сообщений**: статус `finished`, `messages: []`
    (как JSON `finish` без `compose`). Не то же, что `None` (отказ — реплика уйдёт дальше) и
    `silent()` (отправляет service-ответ). Для служебных колбэков, на которые отвечать нечем.
- Построители возвращают ровно ту структуру dict'ов, которую принимает compose-шаг:
  `say("Включаю.")` ≡ `{"reply": {"text": "Включаю."}}` — колонке уходит одинаковый
  ответ (проверено тестом байт-в-байт).
- Сами построители `{{…}}` не подставляют, но возвращённая спецификация проходит compose,
  как у JSON-шага: шаблоны `{{state.*}}`, `{{slots.*}}`, фильтры и `$each` в ней раскрываются.
  Обычно значения формируются в коде (f-строки, `ctx.params`, `ctx.now`). Шаблоны оставляют
  для больших ресурсов (`ctx.asset`), заполняя `ctx.state` / `ctx.slots`. Значения пользователя
  передавайте через `state`, а не вставкой в шаблон: в тексте шаблона они раскрылись бы
  повторно.

## API

### Построители (модульные функции `server_emulator.sdk`)

| Функция | Возвращает | Назначение |
|---|---|---|
| `say(text, speech=None, speech_parts=None)` | `{"text", "speech"?, "speech_parts"?}` | реплика: карточка + озвучка (по умолчанию текст и есть речь; `speech_parts` — озвучка частями с кешем TTS) |
| `card(text)` | `{"text", "card": True}` | карточка с текстом (прогноз, список) |
| `directive(name, payload=None, payload_raw=None, analytics_type=None)` | `{"text": "", "card": False, "directives": [{…}]}` | ответ с директивой: `payload` — Struct (dict), `payload_raw` — base64 (отладка). Прочие поля элемента (`proto`+`fields`, `is_led_silent`) — достройка dict вручную |
| `silent()` | `{"envelope": "service", "text": "", "card": False}` | ответ без реплики — «bare» в JSON: тихий служебный ответ без голосовых добавок |
| `ask(question, then, expect=None)` | `Ask(question, then, expect)` | уточнение: продолжение — функция `then` того же модуля, `expect` — подсказка парсеру ответа |

### `ScenarioContext(runtime, ctx, dry_run=False)`

| Поле/свойство | Что |
|---|---|
| `ctx.text` | реплика, как её оставил диалоговый слой (нормализованный ASR-вариант, если он применён) |
| `ctx.raw_text` | исходная реплика до нормализации (если отличалась) |
| `ctx.slots` | слоты триггера; **живой dict** исполнения — правки переживают шаги |
| `ctx.event` | `Event(name, callback, purpose, frame)` — имя служебного события и поля запроса (для event/protocol_event-триггеров), иначе пустое |
| `ctx.sn` | SN колонки, от которой пришёл запрос |
| `ctx.device` | запись реестра о колонке: sn, name, room, model, display, light(s), apps, platform, fields |
| `ctx.owner` | владелец: id, name, location (гео); комната колонки — `ctx.device.room` |
| `ctx.now` | aware datetime в таймзоне устройства (params device → user → effective → input.timezone; без имени — таймзона сервера) |
| `ctx.params` | параметры владельца/устройства/платформы/сервера + `effective` (docs/contracts/params-db.md) |
| `ctx.timers` | таймеры колонки из состояния её запроса (`EnvironmentStateRaw`, AlarmCapability): `[{timer_id, start_ts, duration, paused, disabled, currently_playing, title}]`; пусто — если колонка ничего не сообщает (своего хранилища у сервера нет, TASK-079) |
| `ctx.alarms` | будильники колонки из того же состояния: `[{alarm_id, time{hours,minutes,seconds}, dates, weekdays, disabled, currently_playing, snooze_time, timezone}]` |
| `ctx.screens` | открытые экраны «Часов» колонки (ClockAppCapability): `timer_active/timer_info/timer_list/alarm_active/alarm_list/alarm_edit` — идентификатор или `true` |
| `ctx.state` | **живой dict** состояния диалога — переживает шаги |
| `ctx.extension` | `ExtensionContext` — настройки расширения: режимы расширений читают свои значения (`id`, `settings`, `get(key, default)`; строки `env:ИМЯ` раскрыты); у обычных py-сценариев `None`. Подробно — docs/contracts/extensions.md, «Настройки в коде расширения» |
| `ctx.dry_run` | True в проверке сценария: действия не выполняются |
| `ctx.effects` | `[{kind, name, arguments}]` — что dry_run сделал бы; показывает проверка сценария |

### Сервисы (обёртки над реестрами `Executor`)

| Вызов | Назначение |
|---|---|
| `await ctx.action(name, **arguments)` | действие реестра Executor (`timer.start`, `alarm.start`, `timer.cancel`, `device.push`, `light.set`). С TASK-079 `timer.start`/`alarm.start`/`timer.cancel` отправляют колонке штатные директивы (`open_screen_*` + `timer_set`/`alarm_add`/`timer_cancel`), своего расписания на сервере нет. В dry_run не выполняется: запись `{kind, name, arguments}` в `ctx.effects`, вернётся `None` |
| `await ctx.fetch_json(url, ttl=0)` | GET JSON через общий fetcher движка (кеш TTL, лимит 512 КиБ, честная ошибка) |
| `await ctx.dataset(name, key, max_age=0, args=None)` | данные плагина: свежая запись из БД (age ≤ max_age) — сразу; иначе пинок плагина, запись в БД делает executor. Нет плагина — устаревшая запись, без записи — `SDKError` |
| `ctx.lexicon(name)` | общий словарь (`scenarios/lexicons.json`) с приоритетом словаря сценария; неизвестное имя → `{}` |

Ошибки сервисов — `SDKError` (нет действия/плагина/сервиса); сетевые и HTTP-ошибки
проходят как есть — сценарий решает сам, отвечать отказом (`None`) или фразой сбоя.

## Пример py-сценария

`state/scenarios/custom.timer.py` (файл рядом с JSON):

```python
"""Таймер: эффект через SDK, ответ построителями SDK."""
from server_emulator.sdk import card, say


async def handle(ctx):
    seconds = 300  # слоты триггера; здесь для примера фиксировано
    await ctx.action("timer.start", seconds=seconds, label="Таймер")  # dry_run сам запишет effect
    geo = ctx.owner.get("location") or {}
    weather = await ctx.dataset("weather", f"{geo.get('latitude')},{geo.get('longitude')}",
                                max_age=3600, args={"url": ctx.params["effective"]["weather"]["url"]})
    reply = say(f"Таймер на {seconds // 60} минут.",
                speech_parts=["Таймер на", f"{seconds // 60} минут."])
    temp = (weather or {}).get("current", {}).get("temperature_2m")
    return [reply, card(f"За окном {temp}°.")] if temp is not None else reply
```

Скрипты с `def handle(ctx)` тоже допустимы (sync/async решает `ScriptRunner`).
Проверка сценария (dry-run) исполняет тот же код с `ctx.dry_run=True`: `ctx.action`
не выполняет действие, а пишет `ctx.effects` —
`[{"kind": "action", "name": "timer.start", "arguments": {"seconds": 300}}]` — и их
показывает проверка.

## 2.1.0 — данные сценария: `ctx.data` (TASK-080)

Хранилище данных py-сценария по владельцу-пользователю: `ctx.data.get(key, default=None)` и
`ctx.data.set(key, value)` (синхронные). Записи живут в `state_store.scenario_data`
(scope `data`, `scenario_id` = id сценария, `owner` = пользователь устройства, без него SN),
переживают перезапуск сервера. В dry-run `set` не пишет — «Проверка» данные не меняет.
Своих файлов в `state/` сценарии не создают: это единственный контрактный путь сохранения
произвольных данных сценария (датасеты — только плагины и чтение, параметры — только чтение).

## 2.2.0 — режим «Интеллект» и «нет источника данных» (TASK-090)

- `ctx.intelligence` (только чтение): `scenarios | model | agent` — режим «Интеллект».
  Сценарий может уступить фразу модели, вернув `None`.
- `ctx.no_data_source()` — ответ сценария данных без плагина-источника: в «Простом» —
  стандартный ответ `no_data_source` («Для этого нет источника данных.», правка текста в
  настройках действует); в «Модели/Агент» — `None` (фраза уходит модели). С `SDKError` от
  `dataset_record`:
  ```python
  try:
      record = await ctx.dataset_record("news", "top")
  except SDKError:
      return ctx.no_data_source()
  ```

## 2.3.0 — маршрут и владелец плеера (TASK-091)

- `ctx.route_mode` (только чтение): `emulate | hybrid | proxy` — маршрут устройства. В «Вместе»
  сценарии данных (`ctx.no_data_source()`) и `system.unavailable` отказываются (ответ `None`):
  промах эмулятора уходит настоящему Яндексу (параллельная сессия, `upstream/hybrid.py`).
- `ctx.player_owner` (только чтение): «yandex», пока в колонке играет контент Яндекса (флаг
  ветки; снимается своими плеерными директивами эмулятора). Сценарии плеера при
  `player_owner == "yandex"` отказываются (`None`): колбэки и ходы очереди идут Яндексу.
  Пауза/стоп (`device.player.pause`) — остаётся локальной штатной функцией колонки.

## 1.1.0 — устройства, парсеры, форматтеры, датасеты, ресурсы

Minor: только добавления, форма построенных dict'ов и сигнатуры 1.0 не менялись. Всё — тонкие
обёртки над движком: парсеры `scenario_engine/parsers.py`, фильтры `bindings.py`, обновление
датасета `Executor.refresh_dataset`; новой логики нет, строки и
значения те же, что у фильтров `{{…|duration}}` и т.п.

### Устройства дома и поиск

| Вызов | Что |
|---|---|
| `ctx.devices` | устройства дома из контекста запроса — `home.devices`, как `{{home.devices}}` (name/aliases/sn/current…); список dict, копия: только чтение |
| `ctx.find_device(text=None, field="name", fields=None, contains_only=False, lexicon=None, strip=None)` | устройство, названное в тексте; парсер `list`. Частный случай `find_in(ctx.devices, …)` |
| `ctx.find_in(items, text=None, field="name", fields=None, contains_only=False, lexicon=None, strip=None)` | то же по любому списку, например `ctx.device["apps"]`; побеждает самое длинное имя |

`lexicon` — слова-команды, снимаемые с начала текста («открой»): имя раздела `lexicon` сценария /
общего словаря либо готовый список. `strip` — фраза или список фраз, вырезаемых из текста до разбора
(алиасы адресата).

### Парсеры

`text` по умолчанию — `ctx.text`. Словарь — имя раздела `lexicon` сценария (приоритет) или общего
словаря (`scenarios/lexicons.json`) либо готовый dict/list. Не найдено — `None`. Все принимают `strip`.

| Вызов | Что |
|---|---|
| `ctx.parse_duration(text=None, lexicon="duration", strip=None)` | секунды: «на пять минут» → 300, «полчаса» → 1800 |
| `ctx.parse_time(text=None, lexicon="time", strip=None)` | время в формате движка «ЧЧ:ММ»: «на семь тридцать» → `"07:30"`; для вывода — `format_hhmm` |
| `ctx.parse_number(text=None, minimum=None, maximum=None, lexicon="numbers", strip=None)` | число цифрами или словами; вне границ — `None` |
| `ctx.parse_lexicon(lexicon_or_name, text=None, strip=None)` | значение по словарю `{фраза: значение}` (`power`, `rooms`, `modes`, `emotions`…): самая длинная фраза |
| `ctx.parse_rest(words, text=None, strip=None)` | остаток фразы после слова-команды (`words` — список или имя словаря) |

### Форматтеры (функции модуля `server_emulator.sdk`)

| Функция | Эквивалент в шаблоне | Пример |
|---|---|---|
| `format_duration(seconds)` | `{{x\|duration}}` | `5400` → «1 час 30 минут» |
| `format_hhmm(value)` | `{{x\|hhmm}}` | `"07:30"` → «7:30» |
| `plural(n, forms)` | `{{x\|plural:a,b,c}}` | `plural(5, ("минуту", "минуты", "минут"))` → «5 минут» (формы: 1, 2-4, 5-0) |
| `time_ru(value)` | `{{clock\|time_ru}}` | `time_ru(ctx.now)` → «12 часов 5 минут» |

Некорректное значение (`format_duration(0)`, `format_hhmm("полвосьмого")`) — `ValueError`, как ошибка
шаблона у JSON.

### Таймзона запроса

`ctx.input_timezone` — `input.timezone` запроса (имя IANA) или `None`. Порядок выбора таймзоны в
`ctx.now` — device → user → effective → input; у сервисного колбэка синхронизации времени он другой
(device → user → input, при пустом input — effective), поэтому сценарий выбирает сам, как в JSON.

### Датасеты с возрастом: `ctx.dataset_record`

`await ctx.dataset_record(name, key, max_age=0, args=None, timeout=None)` →
`{"value", "updated_at", "age_s", "fresh", "stale"}` или `None`. Логика та же, что у шагов
`load` → `refresh` → `load`:

- запись свежа (`age_s ≤ max_age`; `max_age=0` — любая запись свежа) — возвращается сразу, плагин не зовётся;
- иначе плагин обновляет запись через `Executor.refresh_dataset`: запись в БД делает executor,
  параллельные вызовы на ту же запись ждут одно обновление (`_refreshing`), `timeout` секунд
  (по умолчанию 10) — по таймауту плагин дорабатывает в фоне, запись появится к следующему запросу;
- сбой или таймаут плагина — откат на старую запись (`stale=True`, `fresh=False`); записи нет — `None`;
- плагина нет вовсе: старая запись (`stale`) либо `SDKError`.

Как отвечать на устаревшие данные («обновлены 3 часа назад») или на `None` — решает сценарий.
`ctx.dataset(...)` остался как в 1.0 (только значение).

### Ресурсы сценария: `ctx.asset`

`ctx.asset(name)` — JSON-файл `<name>.json` (или с указанным расширением) из каталога
`<раздел>/<id>.assets/` рядом со скриптом — большие шаблоны вроде Div2-карточки погоды (~700 строк).
Каталог лежит на уровень глубже раздела и не попадает в глоб библиотеки `*/*.json`. Имя — без пути
(`..`, `/`, `\`, `:` запрещены), иначе `SDKError`; нет каталога/файла или битый JSON — тоже `SDKError`.
Файл читается при каждом вызове; шаблоны `{{…}}` внутри ресурса SDK не раскрывает (ресурс — данные,
подстановку делает скрипт).

```python
from server_emulator.sdk import card, format_duration, say


async def handle(ctx):
    target = ctx.find_device(fields=["name", "aliases"], contains_only=True)
    aliases = (target or {}).get("aliases")
    seconds = ctx.parse_duration(strip=aliases)
    if seconds is None:
        return None
    await ctx.action("timer.start", seconds=seconds, sn=(target or {}).get("sn"))
    return say(f"Таймер на {format_duration(seconds)}.")


async def weather(ctx):  # виджет: данные с возрастом, шаблон — ресурс
    geo = ctx.owner.get("location") or {}
    record = await ctx.dataset_record("weather", f"{geo.get('latitude')},{geo.get('longitude')}",
                                      max_age=3600, timeout=5)
    if record is None:
        return []  # сервисный колбэк: ответить нечем — тихое завершение
    layout = ctx.asset("weather_card")
    ...
```

## 1.2.0 — настройки расширения в коде

Minor: только добавления, форма построенных dict'ов и сигнатуры 1.0/1.1 не менялись.

- Тип `ExtensionContext` (`server_emulator/extensions/context.py`, экспортируется из
  `server_emulator.sdk` для аннотаций) — значения настроек расширения, только чтение:
  `id`, `settings` (`MappingProxyType`, строки `env:ИМЯ` раскрыты через `auth.secret`),
  `get(key, default=None)`; `repr`/`str` маскируют строковые значения. Новый объект
  создаётся на каждый вызов кода расширения — правка настроек действует без рестарта.
- Режимы расширений (`modes/*.py`) получают контекст как `ctx.extension`; у обычных
  py-сценариев `ctx.extension is None`.
- Действия расширения (`actions/*.py`) читают настройки из `execution.extension`
  (у встроенных действий `None`); адаптеры голосового стека из расширений — из аргумента
  `extension=`, если функция адаптера его принимает (проверка сигнатуры).
- `secret(value)` — раскрытие ссылки `env:ИМЯ` (переменная окружения процесса; нет — пусто),
  те же правила, что у провайдеров эмулятора. Нужен адаптеру расширения для `api_key` своего
  пресета: настройки расширения `ExtensionContext` раскрывает сам.
- Владелец определяется по включённым расширениям (префикс имён / `permissions.actions`),
  чужие настройки недоступны; приватные атрибуты эмулятора, поиск объектов через `gc` и
  правка `sys.path` в коде расширения не поддерживаются. Примеры и ограничения —
  docs/contracts/extensions.md, «Настройки в коде расширения».

## 1.3.0 — обработчики AAPI и отправка на устройство

- `sdk.aapi_handler(*triggers, enabled=None)` — декоратор обработчика события колонки
  (`aapi/*.py` расширения): `async handler(event, ctx) -> bool`; событие — разобранный dict,
  `ctx.reply/send/push`, `ctx.extension`. Триггеры — из `permissions.aapi`; источник
  `extension:<id>`, порядок — после встроенных.
- `sdk.on_event(name)` — наблюдатель: вызывается для каждого события фоном, ответ не меняет.
- `sdk.send_push(sn, action, params)` / `sdk.send_directive(sn, name, payload)` — модульные
  отправки; у сценария — `ctx.send_push(...)` / `ctx.send_directive(...)`. Устройство не в
  сети или действие не из каталога — `SDKError`.

## Что разрешено

- **Код исполняется в процессе сервера без песочницы** — SDK даёт полный Python:
  импорты, файлы, сеть. Границы доверия процедурные, не технические:
  **py-сценарии пишет только администратор** (UI-редактор сценариев и файлы
  `state/scenarios/*.py` доступны администратору); встроенные сценарии —
  часть кодовой базы; расширения проходят установку администратором.
- SDK — единственный публичный вход во внутренности: `voice_stack`, `audio_buffer`,
  `voice` и прочие модули Runtime скриптам не адресуются; runtime-объект, переданный
  в контекст, используется только самим SDK.
- Действия — только через реестр Executor (эффекты без текстов и веток); на вопросы
  без ответа сценарий возвращает `None`, не выдумывает заглушек.

## Не реализовано

- Подключение к исполнению сценариев и dry-run проверка — через движок сценариев.
- Многошаговые диалоги (`ask` → продолжение, `ShouldListen`, TTL) — поддержаны.
- Поле `requires.sdk` у расширений; сейчас совместимость фиксирует
  только `SDK_VERSION` и правило major.
- Слоты из шаблонных триггеров (`ctx.slots`) наполняются диалоговым слоем.

## 1.3.1 — медиа: плеер, библиотека, настройки (удалено в 2.0.0)

> **Удалено в 2.0.0 (TASK-087)**: своей медиатеки и очереди у эмулятора нет, музыка —
> на штатном плеере колонки (см. `docs/contracts/player.md`). `ctx.media`, `ctx.library`,
> `ctx.media_base` и раздача `/media` убраны.

- `ctx.media` — плеер колонки (`server_emulator.media.Player`): очередь в памяти runtime
  (`media_state[sn]`), директивы `audio_play`. Методы: `play_library(library, kind, query, text)`
  (файлы вида — очередь и ответ с директивой; пусто → None), `play_radio(name, url, text)`,
  `start(queue, index, text)`, `step(+1|-1|0)` (следующий/предыдущий/продолжи; None — очередь
  кончилась), `pause()`, `resume()`, `current()`, `now_playing()`. Все методы возвращают
  compose-ответ (или None).
- `ctx.library` — библиотека (`MediaLibrary`): `list/folders/search/resolve/art_for` над
  `state/media/<kind>`; `kind` — `music` или `tales`.
- `ctx.settings` — настройки эмулятора (только чтение); список радио — `settings["media"]["radio"]`.
- `ctx.media_base` — база раздачи файлов для колонки: `http://<хост WS-соединения>/media`
  (файлы и обложки — по пути `/media/<kind>/<путь>`, Range поддержан).

## 1.4.0 — состояние колонки: таймеры, будильники, экраны «Часов»

- `ctx.timers` / `ctx.alarms` / `ctx.screens` — состояние колонки из **её запроса**
  (`TextInput.Request.EnvironmentStateRaw`, capability-типы 21 `NAlice.TAlarmCapability`
  и 144 `NAlice.TClockAppCapability` в `Endpoints[].AnyCapabilities[]`). Разбор —
  `server_emulator/station_state.py`; своего хранилища и планировщика таймеров на
  сервере больше нет (TASK-079): что колонка не сообщает — того нет.
- Остаток таймера — арифметика состояния, как настоящий сервер:
  `remaining = duration − (now − start_ts)` (сверено с ответом «Осталось 47 минут 48
  секунд»). Сигнал (`ringing`) — `currently_playing` у будильника/таймера или истёкший
  активный таймер (`disabled=false`).
- Ответы сценариев раздела `timers` — те же директивы, что слал сервер (имена, payload,
  `is_led_silent: true`, `type: client_action`; `payload_raw` по схеме станции):
  `open_screen_timer_info_directive`/`open_screen_timer_list_directive` +
  `timer_set_directive`, `timer_cancel_directive`, `timer_stop_directive`,
  `open_screen_alarm_list_directive` + `alarm_add_directive` (таймзона колонки,
  `SnoozeTime: 10`), `alarm_stop_directive` + `close_screen_alarm_active_directive`,
  `close_screen_timer_active_directive`. Идентификаторы — 32 hex в четырёх группах
  (`clock_directives.new_clock_id`). Обезличенные образцы —
  `tests/fixtures/directives/` (плейсхолдеры `<…>` = «поле присутствует и непустое»).
- «хватит» при сигнале таймера — ответ **без речи** (образец: `tts_play_placeholder`
  первым кадром, пустой `OutputSpeech.Text`); при сигнале будильника — реплика +
  `alarm_stop_directive{StopMedia: true}`. Если у колонки не звенит — сценарий
  возвращает `None` (реплику забирает `device.player.pause`).
- `ctx.action("timer.start"/"alarm.start"/"timer.cancel")` — отправка тех же директив
  **Push'ем** в живую сессию (`clock_directives.push_message`); результат содержит
  `delivery: "sent" | "no_session"`. Адресация директив другой колонке в образцах не
  встречалась — сценарии отвечают отказом.

## 2.0.0 — медиа на штатном плеере колонки, состояние из запроса (TASK-087, TASK-083)

Ломающее: удалены `ctx.media`, `ctx.library`, `ctx.media_base` (своя медиатека, очередь
и раздача `/media` убраны вместе с `server_emulator/media.py`; расширения, их использующие,
требуют переработки).

- `ctx.player` — состояние штатного плеера колонки из **её запроса** (capability 46
  `NAlice.TAudioPlayerCapability`, есть в каждом `TextInput`):
  `{state: idle|playing|paused|buffering|stopped|finished|unknown, playing, title,
  subtitle, played_ms, duration_ms}`; пустой dict, если колонка ничего не сообщает.
  Своей копии состояния у эмулятора нет.
- Директивы паузы — `server_emulator/media_directives.py` (`pause_items()`), форма
  настоящего сервера без речи: `tts_play_placeholder` первой + `divui_hide_view` +
  `draw_animation_directive` (S3 `animations/player_pause`) + `player_pause{smooth:false}`
  + `clear_queue` (образцы x20, `docs/contracts/player.md`, фикстура
  `tests/fixtures/directives/media-pause.json`).
- Колбэки `music_thin_client_on_started` / `on_stopped` / `@@mm_stack_engine_get_next`
  маршрутизируются в `callbacks.ack` — тихий ответ, как в образцах (x112/x134/x4).
  `audio_play` эмулятор больше не отправляет.
- `ctx.volume` — громкость колонки из **её запроса** (capability 23
  `NAlice.TVolumeCapability`, есть в каждом `TextInput`, TASK-083):
  `{muted, level, max_level, percent_level}`; пустой dict, если колонка ничего не
  сообщает — своего учёта громкости на сервере нет. Сценарий `device.volume` ставит
  уровень тихим ответом по образцу: `sound_set_level{new_level, new_percent_level}`
  (процент = level·100/MaxLevel, фикстура `tests/fixtures/directives/volume-level.json`).
