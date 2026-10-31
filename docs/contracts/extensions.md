---
title: "Расширения эмулятора: extension.json и структура папок"
status: draft
date: "2026-10-30"
module: "server_emulator/extensions/schema.json"
---

# Расширения эмулятора: `extension.json` и структура папок

Код: менеджер `server_emulator/extensions/` (TASK-029). Схема манифеста:
`server_emulator/extensions/schema.json`. Тесты: `tests/test_extensions_manager.py` (TASK-029).

## Обзор

Расширение — git-репозиторий, zip или локальная папка с `extension.json` в корне; добавляет
эмулятору сценарии, эндпоинты, конфиги Quasar, голосовой стек, данные разделов, действия
сценариев и режимы «Интеллекта».

## Источники и установка

| Источник | Вид | Версия |
|---|---|---|
| GitHub-репозиторий | `https://github.com/<owner>/<repo>` — последний релиз (zipball); `…#v1.2.0` или `…#<sha>` — закреплённый тег/коммит | из `extension.json` |
| Прямая ссылка | zip с `extension.json` в корне | из `extension.json` |
| Каталог | JSON-индекс `{extensions: [{id, url, version, sha256?, description}]}` | из записи каталога |
| Локальная папка | режим разработки: подключается без копирования, правки подхватываются | из `extension.json` |

Установленное распаковывается в `state/extensions/<id>/<version>/`; `current` — активная
версия (атомарное переключение), предыдущая хранится для отката. Сетевые источники — только
https, лимит размера и таймаут, сверка sha256 если указан, распаковка с защитой от path
traversal (общий код `extensions/sources.py`, им же пользуется бывший реестр сценариев — `extensions/legacy_registry.py`, TASK-036). Включение/выключение — флаг на
расширение: выключение не удаляет файлы, но убирает всё, что расширение добавляет.

Приоритет при совпадении имён: **пользовательское > расширение > встроенное**. Каталоги
читают слои источников в этом порядке; запись каталога хранит `origin`:
`builtin | extension:<id> | user`. Записи расширения — только чтение: правка создаёт
пользовательскую копию (как сейчас со встроенными сценариями).

## Классы

| Класс | Когда | Что значит |
|---|---|---|
| `data` | только json/yaml/gif/wav | декларативные записи, исполняются существующими движками |
| `code` | есть `.py` | код исполняется в процессе сервера **без песочницы** |
| `install` | скачивает программы/модели | пакеты голосового стека (venv + модели) |

Класс расширения — наибольший из классов содержимого (`data` < `code` < `install`), виден в
карточке установки. Установка `code`/`install` — только с явным подтверждением: API
возвращает список `.py`, разрешений, размера и python-зависимостей, второй запрос
подтверждает. **Это не песочница** — классы и разрешения служат основой информированного
подтверждения при установке, границы доверия процедурные (как у py-сценариев,
docs/contracts/scripts-sdk.md).

## extension.json

| Поле | Тип | Что |
|---|---|---|
| `id` | string, обязателен | строчная латиница, цифры, точки, дефисы (`[a-z0-9]([a-z0-9.-]*[a-z0-9])?`); уникален среди установленных; префикс имён элементов |
| `name` | string, обязателен | название в UI |
| `version` | string, обязателен | semver |
| `description` | string, обязателен | что добавляет |
| `author` | string, обязателен | |
| `license` | string, обязателен | SPDX-идентификатор |
| `homepage` | string | |
| `requires` | object, обязателен | `emulator` — диапазон semver версий эмулятора; `sdk` — диапазон `SDK_VERSION` из `server_emulator/sdk.py` (сейчас `"1.2.0"`); `python_packages` — список pip-требований |
| `permissions` | object, обязателен | см. «Разрешения»; `{}` для data-расширения |
| `settings` | object | схема настроек расширения: `fields` + `defaults` — формат как `fields`/`defaults` в `voice_stack/packages/*.json` |
| `contributes` | object | переопределяет поиск по папкам (ниже) |

- Диапазон версий — строка вида `>=1.0.0 <2.0.0` или `^1.2.0` (совместимо в рамках major);
  сверку выполняет менеджер при установке: `requires.sdk` — с `SDK_VERSION` (ломающие
  изменения SDK — только major), `requires.emulator` — с версией эмулятора.
- `requires.python_packages` менеджер не ставит молча: список показывается в карточке,
  установка — отдельной кнопкой с подтверждением (pip в текущее окружение, TASK-029).
- `settings` — типы полей `text | password | number | select | textarea | file`;
  `options` — `{value, label}`; `when: {ключ: значение}` — поле видно при значении другого
  поля; значения хранит менеджер, рендерит вкладка «Расширений» тем же кодом, что поля
  пакетов голосового стека (TASK-035).
- `contributes` — ключи-имена папок (`"scenarios"`, `"endpoints"`, …) со значением: путь или
  список путей внутри расширения вместо поиска по папке; `"actions": {"prefix": "ha"}` —
  префикс имён действий вместо `<id>.`.
- Неизвестные поля манифеста — ошибка валидации: опечатка в `permissions` не должна
  молча превратить расширение в «без разрешений».

### Разрешения

| Ключ | Значение | Что объявляет |
|---|---|---|
| `network` | список хостов | к каким хостам обращается код и датасеты; `*.example.com` — поддомены |
| `actions` | список ref | какие действия реестра Executor использует и регистрирует |
| `device_push` | bool | шлёт server_action на колонки (push-шаги, `device.push`) |
| `install` | bool | скачивает программы/модели |

Разрешение не ограничивает исполнение — нарушение объявления видит администратор: карточка
установки показывает объявления, расхождение с фактом — повод не включать расширение.

## Папки

Каждая папка необязательна и есть только при содержимом.

| Папка | Что подключает | Класс |
|---|---|---|
| `scenarios/<раздел>/*.json` (+ `.py`) | сценарии JSON v2 в библиотеку (слой поверх встроенных); раздел — папка, `category` ей равна; `.py` — исполнитель `handle(ctx)` рядом с JSON (TASK-019) | data / code |
| `endpoints/*.json` (+ `.py`) | эндпоинты HTTP-ответов (слой поверх встроенных); `.py` — script-обработчик рядом с JSON | data / code |
| `quasar/` | шаблоны get_config_* и переопределения платформ (+ `.py`) | data / code |
| `voice/packages/*.json` | пакеты-установщики речевых серверов (venv + модели) | install |
| `voice/adapters/*.py` | адаптеры ASR/TTS/LLM | code |
| `voice/presets/*.json` | пресеты «Голосового стека» и «Интеллект → Модель» | data |
| `lists/*.yaml\|json` | списки hassil и словари для template-триггеров и ctx.parse_* (слои: встроенный → расширения → пользовательский) | data |
| `modes/*.py` | режимы «Интеллекта»: `MODE` + `async answer(ctx, text, history)` (TASK-034) | code |
| `mcp/*.json` | описания MCP-серверов: stdio-команда или URL (TASK-032) | code |
| `actions/*.py` | действия сценариев: `ACTIONS = {"<префикс>.имя": fn}` (TASK-033) | code |
| `datasets/*.py` | источники данных шагов load/refresh: `DATASETS = {"имя": fn}` (TASK-033) | code |
| `animations/` | анимации (json + gif) | data |
| `params/` | определения параметров устройств | data |
| `replies/` | стандартные ответы (новые `case`, только известные) | data |
| `catalog/` | записи каталога приложений | data |
| `updates/` | правила OTA | data |
| `tests/` | фразы и эталоны самопроверки после установки (прогон через dry-run, TASK-023/029) | data |
| `hooks.py` | хуки `on_install / on_enable / on_disable / migrate(from_version)` | code |
| `sounds/<голос>/` | **предусмотрено, не реализовано**: готовый звуковой кеш (`index.json` + wav), подключается как кеш только для чтения | data |
| `ui/*.js` | **предусмотрено, не реализовано**: своя вкладка внутри «Расширений» | code |

- `hooks.py` — один модуль в корне расширения; все функции необязательны; менеджер вызывает
  их через `ScriptRunner` с таймаутом (TASK-029), ошибка хука откатывает установку;
  `migrate(from_version)` — при обновлении, перенос настроек с прошлой версии.
- `mcp/*.json` — класс `code`: MCP-сервер это внешний процесс или сеть; `actions/*.py` и
  `datasets/*.py` — код в процессе сервера, сигнатуры — контракт реестров Executor
  (TASK-033; url/token для сети — из настроек расширения).
- Имена элементов (сценарии, эндпоинты, списки, действия, датасеты, пакеты, режимы)
  получают префикс `<ext-id>.`; принадлежность записи расширению определяется по `origin`,
  не по разбору имени (id может содержать точки).

## Настройки в коде расширения

Значения `settings` (вкладка «Расширений») код расширения получает объектом `ExtensionContext`
(`server_emulator/extensions/context.py`; тип также экспортирует `server_emulator.sdk`) —
**только чтение**: `id`, `settings` (все значения, `MappingProxyType`) и `get(key, default=None)`.
Строки вида `env:ИМЯ` уже раскрыты через `auth.secret`, как секреты провайдеров: в настройке
храните ссылку (`env:COFFEE_TOKEN`), само значение живёт в переменной окружения процесса и не
попадает в настройки и экспорты. Контекст создаётся заново на каждый вызов кода расширения —
правка настроек действует без рестарта сервера. `repr`/`str` контекста маскируют строковые
значения (`***`), чтобы секреты не утекали в журналы и трассы.

| Код расширения | Откуда контекст |
|---|---|
| `modes/*.py` — `answer(ctx, text, history)` | `ctx.extension` |
| `actions/*.py` — `fn(arguments, execution)` | `execution.extension` — контекст расширения, объявившего действие (имя под префиксом `contributes.actions.prefix`/`id` или в `permissions.actions`); у встроенных действий `None` |
| `voice/adapters/*.py` — `complete/transcribe/speech/…` | аргумент `extension=ExtensionContext` — если функция адаптера его принимает (проверка сигнатуры); встроенные и прежние адаптеры расширений вызываются как раньше |

Контекст всегда относится к тому расширению, из которого загружен код: чужие настройки код
расширения получить не может.

Примеры (расширение `coffee` с полями `url` и `token`):

```python
# modes/answer.py — режим
async def answer(ctx, text, history):
    ext = ctx.extension                       # ExtensionContext расширения coffee
    url = ext.get("url")                      # значение или None (get(key, default=None))
    token = ext.get("token", "")              # env:COFFEE_TOKEN уже раскрыт
    ...
```

```python
# actions/brew.py — действие
async def brew(arguments, execution):
    ext = execution.extension                 # контекст расширения-владельца
    if ext is None:                           # у встроенных действий контекста нет
        raise RuntimeError("действию coffee нужны его настройки")
    ...  # POST {ext.get("url")} с Authorization: Bearer {ext.get("token")}
```

```python
# voice/adapters/speak.py — адаптер: extension добавлен к прежней сигнатуре
from server_emulator.sdk import secret

async def speech(runtime, config, payload, extension=None):
    # api_key пресета может быть ссылкой env:ИМЯ — раскрывает sdk.secret; пусто — токен расширения
    token = secret(config.get("api_key")) or (extension.get("token", "") if extension else "")
    ...  # прежний адаптер (без параметра extension) работает как раньше
```

Секреты — только через SDK: `server_emulator.sdk.secret(value)` раскрывает ссылку `env:ИМЯ`
(значения настроек расширения `ExtensionContext` уже раскрыл). Импорт внутренних модулей
эмулятора (`server_emulator.auth` и др.) в коде расширения не поддерживается.

Общий код нескольких файлов расширения подключайте обычным модулём в корне расширения,
загружаемым относительно `__file__` (пути внутри расширения не зависят от того, откуда оно
запущено):

```python
# actions/brew.py — общий модуль core.py в корне расширения
import importlib.util
from pathlib import Path

_core = Path(__file__).resolve().parent.parent / "core.py"
_spec = importlib.util.spec_from_file_location("coffee_core", _core)
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
```

**Запрещённые обходы** — не поддерживаются, код расширения так не пишет: приватные атрибуты
эмулятора (`ctx._runtime`, `manager._state` и подобные), поиск объектов через `gc`, правка
`sys.path`. Штатный доступ к настройкам — только `ExtensionContext`; поведение эмулятора при
обходах не гарантируется.

## Пример 1: data-расширение — один JSON-сценарий

```
coffee/
├── extension.json
└── scenarios/
    └── custom/
        └── coffee.make.json
```

`extension.json`:

```json
{
  "id": "coffee",
  "name": "Кофемашина",
  "version": "1.0.0",
  "description": "Сценарий «сделай кофе» — реплика и карточка.",
  "author": "Ivan Petrov <ivan@example.com>",
  "license": "MIT",
  "homepage": "https://github.com/example/alice-coffee",
  "requires": {
    "emulator": ">=0.1.0",
    "sdk": ">=1.0.0 <2.0.0",
    "python_packages": []
  },
  "permissions": {}
}
```

`scenarios/custom/coffee.make.json` (формат — docs/contracts/scenario-json-v2.md):

```json
{
  "format_version": 2,
  "id": "coffee.make",
  "version": "1.0.0",
  "category": "custom",
  "title": {"ru": "Кофе", "en": "Coffee"},
  "description": {"ru": "«сделай кофе» — подтверждение и карточка"},
  "priority": 100,
  "triggers": [
    {"kind": "phrase", "phrases": ["сделай кофе", "принеси кофе"], "match": "contains"}
  ],
  "flow": [
    {"id": "say", "op": "compose", "reply": {"text": "Варю кофе."}},
    {"id": "done", "op": "finish"}
  ]
}
```

## Пример 2: расширение с кодом — сценарий + .py + действие

```
ha/
├── extension.json
├── hooks.py
├── actions/
│   └── lights.py
└── scenarios/
    └── smart_home/
        ├── ha.light.scene.json
        └── ha.light.scene.py
```

`extension.json`:

```json
{
  "id": "ha",
  "name": "Home Assistant",
  "version": "0.3.1",
  "description": "Свет через Home Assistant: сценарий с кодом, действие ha.light.turn, хук миграции.",
  "author": "Ivan Petrov <ivan@example.com>",
  "license": "MIT",
  "requires": {
    "emulator": ">=0.1.0",
    "sdk": ">=1.0.0 <2.0.0",
    "python_packages": ["aiohttp>=3.11,<4"]
  },
  "permissions": {
    "network": ["homeassistant.local"],
    "actions": ["light.set", "ha.light.turn"],
    "device_push": false,
    "install": false
  },
  "settings": {
    "fields": [
      {"key": "url", "label": "Адрес Home Assistant", "type": "text"},
      {"key": "token", "label": "Долгосрочный токен", "type": "password"}
    ],
    "defaults": {"url": "http://homeassistant.local:8123", "token": ""}
  }
}
```

`actions/lights.py` (реестр Executor рядом с `light.set`, TASK-033):

```python
"""Действие ha.light.turn; префикс — id расширения (или contributes.actions.prefix)."""


async def turn(arguments, execution):
    """Сигнатура — как у встроенных действий (runtime.py: light_set, device_push).

    Аргументы — шаг action {room, on}; возврат — значение в slot шага ({on, brightness}).
    Сеть — общий клиент runtime (lifecycle.client_session), url/token — настройки расширения.
    """
    ...  # POST {url}/api/services/light/turn_on|turn_off


ACTIONS = {"ha.light.turn": turn}
```

`scenarios/smart_home/ha.light.scene.json` (триггеры и guard — в JSON, выбор ветки — код):

```json
{
  "format_version": 2,
  "id": "ha.light.scene",
  "version": "0.3.1",
  "category": "smart_home",
  "title": {"ru": "Свет через Home Assistant"},
  "priority": 90,
  "triggers": [
    {"kind": "phrase", "phrases": ["включи свет в доме", "выключи свет в доме"], "match": "contains"}
  ],
  "executor": {"kind": "script"}
}
```

`scenarios/smart_home/ha.light.scene.py` (SDK — docs/contracts/scripts-sdk.md):

```python
"""Сценарий-скрипт (TASK-019): handle(ctx) рядом с JSON."""
from server_emulator.sdk import say


async def handle(ctx):
    room = ctx.slots.get("room") or ctx.device.get("room") or "дом"
    on = "включи" in (ctx.text or "").lower()
    result = await ctx.action("ha.light.turn", room=room, on=on)  # dry_run запишет ctx.effects
    if result is None:  # Home Assistant недоступен
        return None     # отказ: реплика уходит следующему кандидату или в режим «Интеллект»
    return say(f"Свет в «{room}» {'включён' if on else 'выключен'}.")
```

`hooks.py`:

```python
"""Хуки жизненного цикла: менеджер вызывает через ScriptRunner с таймаутом;
ошибка хука откатывает операцию (TASK-029). Все функции необязательны."""


def on_install(settings):
    """После распаковки и проверки, до включения."""


def on_enable(settings):
    """При включении расширения."""


def on_disable(settings):
    """При выключении; файлы не удаляются."""


def migrate(from_version, settings):
    """При обновлении: from_version — версия, с которой переносим настройки."""
```

## Обработчики WS (TASK-059, SDK 1.3.0)

Папка `aapi/*.py`: обработчики сообщений и событий колонки регистрируются публичным
декоратором SDK — сигнатура `async handler(event, ctx) -> bool`, без внутренних классов:

```python
from server_emulator import sdk

@sdk.aapi_handler('@@mm_custom')            # или 'TextInput', 'VoiceInput', ...
async def greet(event, ctx):
    await ctx.reply(sdk.say('привет от расширения'))
    return True

@sdk.on_event('@@mm_custom')                # наблюдатель: видит каждое событие,
async def watch(event, ctx): ...            # в выборе обработчика не участвует
```

- `event` — разобранное сообщение: `{name, type, message_id, request_id, dialog_id, payload, text}`
  (`payload` — dict из protobuf Struct); `ctx` — `sn`, `device`, `extension` (ExtensionContext),
  `reply(say(...)/card(...)/directive(...)/silent())`, `send(байты)`, `push(действие, параметры)`.
- Каждый триггер обязан быть объявлен в `permissions.aapi: ["<событие>", …]` — без объявления
  обработчик не регистрируется (запись в warnings, как у других разрешений).
- Источник — `extension:<id>`, виден во вкладке «Эмулятор → WS → Входящие». Вызываются ПОСЛЕ
  встроенных: расширение не перекрывает встроенные ответы протокола без явного намерения.
- Выключение/удаление расширения снимает его обработчики (пересборка реестра); битый модуль
  отключает расширение целиком — как у actions/datasets.
- Отправка на устройство из кода расширения: `sdk.send_push(sn, action, params)` (каталог
  actions.py) и `sdk.send_directive(sn, name, payload)`; у сценария — то же методами
  `ctx.send_push(...)` / `ctx.send_directive(...)`. Устройство не в сети — `SDKError`.

## Не реализовано

- Менеджер: источники, установка, включение, обновление, откат, хуки, python-зависимости,
  API — TASK-029.
- Точки подключения папок: сценарии/эндпоинты/Quasar/списки — TASK-030; голосовой стек —
  TASK-031; данные разделов и `mcp/` — TASK-032; `actions/` и `datasets/` — TASK-033;
  режимы «Интеллекта» — TASK-034; вкладка UI — TASK-035, вкладки `ui/*.js` расширений —
  после неё.
- `sounds/<голос>/` — предусмотрено контрактом, не реализовано.
- Автотест схемы против примеров этого контракта — вместе с менеджером (TASK-029);
  `jsonschema` в зависимостях проекта сейчас нет, проверка выполнена разово.
