---
title: "SDK py-сценариев и расширений"
status: draft
date: "2026-10-30"
module: "server_emulator/sdk.py"
---

# SDK py-сценариев и расширений

Код: `server_emulator/sdk.py`. Тесты: `tests/test_sdk.py`.

## Обзор

Публичный API py-скриптов (сценарии — TASK-019/020, со временем py-эндпоинты и расширения):
контекст запроса `ScenarioContext`, построители ответа и сервисы — тонкие обёртки над
движком сценариев. Скрипт не лезет во внутренности Runtime; SDK ничего не дублирует:
ответ собирает существующий `reply_compose.compose_reply`, действия/данные — реестры
`Executor` (`runtime.scenario_executor`), новых сетевых клиентов нет.

## Версия

`SDK_VERSION = "1.1.0"` — semver. **Ломающие изменения API — только со сменой major**
(удаление/переименование/смена сигнатур, смена формы построенных dict'ов). Добавление
полей и новых функций — minor, исправления поведения — patch. Расширение объявляет, с
какой версией SDK оно совместимо (поле `requires.sdk` появится вместе с расширениями,
TASK-028/029).

## Контракт

- `handle(ctx)` получает `ScenarioContext` (TASK-019) и возвращает:
  - **построитель ответа** (`say`/`card`/`directive`/`silent`) или равнозначный dict —
    спецификацию reply в формате JSON-шага `compose` (docs/contracts/scenario-json-v2.md,
    раздел «Ответ»); каркас protobuf собирает `compose_reply`, доставку и каркас ответа
    (CardId, SequenceNumber, tts_play_placeholder, TTS-кеш) — диалоговый/голосовой слой,
    как для JSON-сценария;
  - **список** таких спецификаций — несколько сообщений по порядку;
  - **`ask(...)`** (TASK-020) — вопрос с продолжением диалога;
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
| `ask(question, then, expect=None)` | `Ask(question, then, expect)` | уточнение (TASK-020): продолжение — функция `then` того же модуля, `expect` — подсказка парсеру ответа |

### `ScenarioContext(runtime, ctx, dry_run=False)`

| Поле/свойство | Что |
|---|---|
| `ctx.text` | реплика, как её оставил диалоговый слой (нормализованный ASR-вариант, если он применён) |
| `ctx.raw_text` | исходная реплика до нормализации (если отличалась) |
| `ctx.slots` | слоты триггера (TASK-022); **живой dict** исполнения — правки переживают шаги |
| `ctx.event` | `Event(name, callback, purpose, frame)` — имя служебного события и поля запроса (для event/protocol_event-триггеров), иначе пустое |
| `ctx.sn` | SN колонки, от которой пришёл запрос |
| `ctx.device` | запись реестра о колонке: sn, name, room, model, display, light(s), apps, platform, fields |
| `ctx.owner` | владелец: id, name, location (гео); комната колонки — `ctx.device.room` |
| `ctx.now` | aware datetime в таймзоне устройства (params device → user → effective → input.timezone; без имени — таймзона сервера) |
| `ctx.params` | параметры владельца/устройства/платформы/сервера + `effective` (docs/contracts/params-db.md) |
| `ctx.state` | **живой dict** состояния диалога — переживает шаги (TASK-020) |
| `ctx.dry_run` | True в проверке сценария (TASK-019): действия не выполняются |
| `ctx.effects` | `[{kind, name, arguments}]` — что dry_run сделал бы; показывает проверка сценария |

### Сервисы (обёртки над реестрами `Executor`)

| Вызов | Назначение |
|---|---|
| `await ctx.action(name, **arguments)` | действие реестра Executor (`timer.start`, `alarm.start`, `timer.cancel`, `device.push`, `light.set`). В dry_run не выполняется: запись `{kind, name, arguments}` в `ctx.effects`, вернётся `None` |
| `await ctx.fetch_json(url, ttl=0)` | GET JSON через общий fetcher движка (кеш TTL, лимит 512 КиБ, честная ошибка) |
| `await ctx.dataset(name, key, max_age=0, args=None)` | данные плагина: свежая запись из БД (age ≤ max_age) — сразу; иначе пинок плагина, запись в БД делает executor. Нет плагина — устаревшая запись, без записи — `SDKError` |
| `ctx.lexicon(name)` | общий словарь (`scenarios/lexicons.json`) с приоритетом словаря сценария; неизвестное имя → `{}` |

Ошибки сервисов — `SDKError` (нет действия/плагина/сервиса); сетевые и HTTP-ошибки
проходят как есть — сценарий решает сам, отвечать отказом (`None`) или фразой сбоя.

## Пример py-сценария

`state/scenarios/custom.timer.py` (файл рядом с JSON, TASK-019):

```python
"""Таймер: эффект через SDK, ответ построителями SDK."""
from server_emulator.sdk import card, say


async def handle(ctx):
    seconds = 300  # слоты триггера — TASK-022; здесь для примера фиксировано
    await ctx.action("timer.start", seconds=seconds, label="Таймер")  # dry_run сам запишет effect
    geo = ctx.owner.get("location") or {}
    weather = await ctx.dataset("weather", f"{geo.get('latitude')},{geo.get('longitude')}",
                                max_age=3600, args={"url": ctx.params["effective"]["weather"]["url"]})
    reply = say(f"Таймер на {seconds // 60} минут.",
                speech_parts=["Таймер на", f"{seconds // 60} минут."])
    temp = (weather or {}).get("current", {}).get("temperature_2m")
    return [reply, card(f"За окном {temp}°.")] if temp is not None else reply
```

Скрипты с `def handle(ctx)` тоже допустимы (sync/async решает `ScriptRunner`, TASK-017).
Проверка сценария (dry-run) исполняет тот же код с `ctx.dry_run=True`: `ctx.action`
не выполняет действие, а пишет `ctx.effects` —
`[{"kind": "action", "name": "timer.start", "arguments": {"seconds": 300}}]` — и их
показывает проверка (TASK-019).

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

## Что разрешено

- **Код исполняется в процессе сервера без песочницы** — SDK даёт полный Python:
  импорты, файлы, сеть. Границы доверия процедурные, не технические:
  **py-сценарии пишет только администратор** (UI-редактор сценариев и файлы
  `state/scenarios/*.py` доступны администратору; TASK-021); встроенные сценарии —
  часть кодовой базы; расширения проходят установку администратором (TASK-029).
- SDK — единственный публичный вход во внутренности: `voice_stack`, `audio_buffer`,
  `voice` и прочие модули Runtime скриптам не адресуются; runtime-объект, переданный
  в контекст, используется только самим SDK.
- Действия — только через реестр Executor (эффекты без текстов и веток); на вопросы
  без ответа сценарий возвращает `None`, не выдумывает заглушек.

## Не реализовано

- Подключение к исполнению сценариев и dry-run проверка — TASK-019.
- Многошаговые диалоги (`ask` → продолжение, `ShouldListen`, TTL) — TASK-020.
- Поле `requires.sdk` у расширений — TASK-028/029; сейчас совместимость фиксирует
  только `SDK_VERSION` и правило major.
- Слоты из шаблонных триггеров (`ctx.slots`) наполняются в TASK-022.
