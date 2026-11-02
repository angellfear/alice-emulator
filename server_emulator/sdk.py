"""SDK py-сценариев (TASK-018): публичный API — и для встроенных сценариев, и для
расширений. Скрипт не лезет во внутренности Runtime: контекст запроса, построители
ответа и сервисы — отсюда.

Принцип: SDK НЕ дублирует логику — только обёртки и dataclass'ы над тем, что уже
есть в движке (1.1.0: парсеры scenario_engine/parsers.py, форматтеры фильтров шаблонов, обновление
датасета Executor; 1.2.0: ExtensionContext — настройки расширения для его кода, TASK-053; secret — раскрытие
ссылок env:ИМЯ, как у провайдеров).
Построители (say/card/directive/silent) возвращают ровно ту структуру
dict'ов, которую принимает compose-шаг (scenario_engine/reply_compose.compose_reply) —
никакой новой сборки ответа. Сервисы контекста (action/fetch_json/dataset/lexicon) —
тонкие обёртки над реестром Executor (runtime.scenario_executor): действия, fetch_json
с кешем, плагины датасетов, общие словари. Новых сетевых клиентов нет.

Версия SDK_VERSION — semver: ломающие изменения API только со сменой major;
расширение объявляет, с какой версией SDK оно совместимо.

SDK не импортирует voice_stack/audio_buffer/voice — доступ к runtime только через
объект, переданный в конструктор ScenarioContext (как executor/dialog получают своих
коллабораторов конструктором).
"""
from __future__ import annotations

import copy
import datetime
import inspect
import json
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from .auth import secret as _secret
from .extensions.context import ExtensionContext, action_context  # ExtensionContext — для аннотаций кода расширений
from .scenario_engine import bindings as _bindings
from .scenario_engine import parsers as _parsers

SDK_VERSION = "2.0.0"  # TASK-087: удалены ctx.media/library/media_base, добавлен ctx.player; TASK-083: ctx.volume
DATASET_REFRESH_TIMEOUT_S = 10.0  # как у шага refresh без timeout


class SDKError(Exception):
    """Честная ошибка SDK (нет действия/плагина/сервиса) — сценарий видит её как исключение."""


def secret(value):
    """Значение секрета: ссылка `env:ИМЯ` → переменная окружения процесса (нет — пусто),
    иначе строка как есть, не строка — пусто. Те же правила, что у провайдеров эмулятора:
    адаптер расширения раскрывает так `api_key` своего пресета (настройки расширения
    ExtensionContext раскрывает сам)."""
    return _secret(value)


# --- 1.3.0: обработчики AAPI расширений и отправка на устройство (TASK-059) ----------

_RUNTIME = None      # привязывает Runtime.__init__ (bind_runtime) — модульные отправки
_LOADING = None      # id расширения, чьи aapi/*.py импортирует загрузчик (rebuild_extension_registries)
_PENDING: list = []  # регистрации за импорт модулей расширения; забирает загрузчик (_pending)


def bind_runtime(runtime):
    """Привязать runtime для модульных send_push/send_directive — как bind_manager
    у расширений; последний вызов побеждает (один самостоятельный сервер)."""
    global _RUNTIME
    _RUNTIME = runtime


class _LoadingExt:
    """Контекст загрузки aapi-модулей расширения: декораторам известно имя владельца."""

    def __init__(self, ext_id):
        self.ext_id = str(ext_id)
        self.previous = None

    def __enter__(self):
        global _LOADING
        self.previous, _LOADING = _LOADING, self.ext_id
        return self

    def __exit__(self, *exc):
        global _LOADING
        _LOADING = self.previous
        return False


def loading_extension(ext_id):
    """Контекст для загрузчика расширений: внутри импортируются aapi/*.py одного
    расширения, декораторы aapi_handler/on_event записывают регистрации ему."""
    return _LoadingExt(ext_id)


def _pending():
    """Регистрации, накопленные за импорт модулей расширения."""
    taken, _PENDING[:] = _PENDING[:], []
    return taken


def aapi_handler(*triggers, enabled=None):
    """Обработчик AAPI-события колонки из расширения: async handler(event, ctx) -> bool.

    event — разобранное сообщение {name, type, message_id, request_id, dialog_id, payload,
    text}; ctx — отправка и устройство (ctx.sn/device/extension, ctx.reply(say(...)),
    ctx.send(байты), ctx.push(действие, параметры)). triggers — имена сообщений и
    служебных событий ('TextInput', '@@mm_typed_callback', ...); каждое обязано быть
    объявлено в permissions.aapi манифеста, иначе не регистрируется. Источник —
    extension:<id>; вызываются после встроенных. Выключение/удаление расширения снимает
    обработчики (пересборка реестра)."""

    def register(handler):
        if _LOADING is None:
            raise SDKError("sdk.aapi_handler — только в aapi/*.py расширения")
        _PENDING.append({"triggers": triggers, "enabled": enabled, "handler": handler, "observer": False})
        return handler

    return register


def on_event(name):
    """Наблюдатель события (1.3.0): async fn(event, ctx) вызывается для каждого события
    фоном — в выборе обработчика не участвует и ответ колонке не меняет. Тоже требует
    permissions.aapi."""

    def register(fn):
        if _LOADING is None:
            raise SDKError("sdk.on_event — только в aapi/*.py расширения")
        _PENDING.append({"triggers": (str(name),), "enabled": None, "handler": fn, "observer": True})
        return fn

    return register


def _aapi_event(conversation, incoming, trigger):
    """Разобранное сообщение для обработчика расширения — публичный dict без
    protobuf-классов: имя, тип, заголовки, Payload как dict, текст TextInput."""
    from google.protobuf import json_format

    message = getattr(incoming.Event, str(trigger), None)
    request = getattr(message, "Request", None)
    event_obj = getattr(request, "Event", None) if request is not None else getattr(message, "Event", None)
    header = getattr(message, "Header", None)  # заголовок сообщения (TEvent своего Header не имеет)
    event = {"name": str(getattr(event_obj, "Name", "") or trigger),
             "type": int(getattr(event_obj, "Type", 0) or 0),
             "message_id": str(getattr(incoming.Event.Header, "MessageId", "") or ""),
             "request_id": str(getattr(header, "RequestId", "") or ""),
             "dialog_id": str(getattr(header, "DialogId", "") or ""),
             "payload": {},
             "text": str(getattr(event_obj, "Text", "") or "")}
    if event_obj is not None:
        payload = getattr(event_obj, "Payload", None)
        if payload is not None and len(payload.fields):
            try:
                event["payload"] = json_format.MessageToDict(payload, preserving_proto_field_name=True)
            except Exception:  # noqa: BLE001 — битый Payload отдаётся пустым
                event["payload"] = {}
    return event


def _aapi_compose_ctx(conversation, event):
    """Контекст compose для ctx.reply: те же поля запроса, что собирает voice.py."""
    return {"input": {"message_id": event["message_id"], "request_id": event["request_id"],
                      "dialog_id": event["dialog_id"], "sn": event["sn"], "text": ""},
            "clock": {"now": datetime.datetime.now()},
            "sequence": next(conversation.runtime.sequence)}


class _AapiContext:
    """Контекст обработчика AAPI расширения (1.3.0): ExtensionContext, устройство,
    ответ колонке и push. reply(spec) — сборка тем же compose_reply, что у сценариев
    (say/card/directive/silent); send(data) — сырой AAPI-фрейм в сокет этой колонки;
    push(action, params) — действие каталога (actions.py)."""

    __slots__ = ("conversation", "event", "extension", "_compose_ctx")

    def __init__(self, conversation, event, extension, compose_ctx):
        self.conversation = conversation
        self.event = event
        self.extension = extension
        self._compose_ctx = compose_ctx

    @property
    def sn(self):
        """SN колонки, от которой пришло событие."""
        return self._compose_ctx["input"]["sn"]

    @property
    def device(self):
        """Запись реестра о колонке (как ctx.device у сценариев)."""
        return (self.conversation.runtime._scenario_context(sn=self.sn) or {}).get("device") or {}

    async def reply(self, spec):
        """Ответ колонке из построителя SDK (say/card/directive/silent): сборка и каркас —
        как у шага compose сценария."""
        from .scenario_engine.dialog import finalize_reply
        from .scenario_engine.reply_compose import compose_reply
        runtime = self.conversation.runtime
        message = compose_reply(dict(spec or {}), self._compose_ctx, runtime.scenario_executor.schemas_root)
        finalize_reply(message, self._compose_ctx)
        await self.conversation.send(b"AAPI" + message.SerializeToString())

    async def send(self, data):
        """Сырой фрейм в сокет колонки (байты; AAPI-префикс не добавляется)."""
        await self.conversation.send(data)

    async def push(self, action, params=None):
        """Push-действие каталога этой колонке (как send_push)."""
        return await _push(self.conversation.runtime, self.sn, action, params)


async def adapt_aapi_call(handler, conversation, incoming, trigger, meta=None):
    """Мост реестра → публичная сигнатура (TASK-059): event (dict) + ctx (ExtensionContext
    расширения-владельца, устройство, reply/send). Возвращённое приводится к bool."""
    event = _aapi_event(conversation, incoming, trigger)
    event["sn"] = conversation.trace_context.get("sn") or ""
    ext_id = (meta or {}).get("source", "").removeprefix("extension:")
    manager = getattr(conversation.runtime, "extensions", None)
    extension = manager.context(ext_id) if ext_id and manager is not None else None
    ctx = _AapiContext(conversation, event, extension, _aapi_compose_ctx(conversation, event))
    result = handler(event, ctx)
    if inspect.isawaitable(result):
        result = await result
    return bool(result)


async def _push(runtime, sn, action, params):
    """Общая отправка Push-действия каталога (actions.py): неизвестное действие и
    устройство не в сети — понятное исключение SDK."""
    try:
        result = await runtime.push_action(str(sn), str(action), dict(params or {}))
    except ValueError as exc:  # неизвестное действие / параметры
        raise SDKError(str(exc)) from exc
    if not result or result.get("delivery") == "no_session":
        raise SDKError(f"устройство {sn} не в сети — push не отправлен")
    return result


async def send_push(sn, action, params=None):
    """Push-действие каталога (actions.py CATALOG) выбранному устройству (1.3.0):
    для сценариев (вне ctx) и расширений. Устройство не в сети — SDKError."""
    if _RUNTIME is None:
        raise SDKError("SDK не привязан к runtime")
    return await _push(_RUNTIME, sn, action, params)


async def send_directive(sn, name, payload=None):
    """Директива Push произвольного вида выбранному устройству (1.3.0). Устройство
    не в сети — SDKError."""
    if _RUNTIME is None:
        raise SDKError("SDK не привязан к runtime")
    from . import actions as _actions
    from .runtime import ROOT
    from .schema import load_type
    message = _actions.push_directive(load_type(ROOT / "schemas", "alice-api.pb",
                                                "NAlice.NAliceApi.TServerMessage"), name, payload)
    if not await _RUNTIME.send_message(str(sn), message):
        raise SDKError(f"устройство {sn} не в сети — директива не отправлена")


# --- построители ответа: return-значение handle() -------------------------------
#
# Каждый построитель отдаёт спецификацию reply — ровно тот dict, который несёт
# JSON-шаг {"op": "compose", "reply": {…}} в compose_reply (docs/contracts/scenario-json-v2.md,
# раздел «Ответ»). Сборка сообщения protobuf — забота существующего compose_reply,
# доставка и каркас (CardId/SequenceNumber/tts_play_placeholder) — диалогового слоя.


def say(text, speech=None, speech_parts=None):
    """Реплика: карточка с текстом + озвучка (по умолчанию текст и есть речь).

    Эквивалент JSON-шага compose {"reply": {"text": …}}. speech — другой текст
    озвучки; speech_parts — озвучка частями (файловый кеш TTS)."""
    spec = {"text": "" if text is None else str(text)}
    if speech is not None:
        spec["speech"] = str(speech)
    if speech_parts:
        spec["speech_parts"] = [str(part) for part in speech_parts]
    return spec


def card(text):
    """Карточка с текстом (прогноз, список): эквивалент {"reply": {"text": …, "card": true}}."""
    return {"text": "" if text is None else str(text), "card": True}


def directive(name, payload=None, payload_raw=None, analytics_type=None):
    """Ответ с директивой (set_timezone, draw_animation…): эквивалент
    {"reply": {"text": "", "card": false, "directives": [{…}]}}.

    payload — google.protobuf.Struct (dict); payload_raw — base64 (отладка);
    analytics_type — метка аналитики. Прочие поля элемента (proto+fields,
    is_led_silent) — достройка возвращённого dict вручную: это тот же формат JSON."""
    item = {"name": str(name)}
    if analytics_type:
        item["analytics_type"] = str(analytics_type)
    if payload is not None:
        item["payload"] = payload
    if payload_raw:
        item["payload_raw"] = payload_raw
    return {"text": "", "card": False, "directives": [item]}


def silent():
    """Ответ без реплики — «bare» в JSON: тихий служебный ответ (envelope service):
    без голосовых добавок, с пустыми EffectiveSettings и серверной меткой времени."""
    return {"envelope": "service", "text": "", "card": False}


@dataclass(frozen=True)
class Ask:
    """Уточнение (TASK-020): вопрос репликой, продолжение — функция `then` того же
    модуля (получает (ctx, answer); арность решает ScriptRunner); expect — подсказка
    парсеру значения ответа (TASK-022), движок хранит её в данных ожидания и отдаёт
    продолжению через ctx.ask. Ожидание сохраняется так же, как для JSON-шага ask."""
    question: str
    then: str
    expect: str | None = None


def ask(question, then, expect=None):
    """Вопрос с ожиданием ответа: return ask("На сколько?", then="on_duration", expect="duration")."""
    return Ask(question=str(question), then=str(then), expect=str(expect) if expect else None)


# --- форматтеры (1.1.0): те же строки, что у фильтров шаблонов JSON -------------------
#
# Обёртки над закрытым реестром фильтров scenario_engine/bindings.py — логика не
# копируется: {{x|duration}} ≡ format_duration(x), {{x|hhmm}} ≡ format_hhmm(x),
# {{x|plural:a,b,c}} ≡ plural(x, (a, b, c)), {{clock|time_ru}} ≡ time_ru(clock).
# Некорректное значение — ValueError (BindingError), как ошибка шаблона у JSON.


def format_duration(seconds):
    """Секунды → «1 час 30 минут» / «5 минут» / «1 минуту 30 секунд» (> 0)."""
    return _bindings._filter_duration(seconds, None, {})


def format_hhmm(value):
    """«07:30» / «7:30» → «7:30» (без ведущего нуля часа; минуты — две цифры)."""
    return _bindings._filter_hhmm(value, None, {})


def plural(n, forms):
    """Число с формой слова: plural(5, ("минуту", "минуты", "минут")) → «5 минут».
    forms — три формы (для 1, для 2-4, для 5-0) кортежем или строкой «минуту,минуты,минут»."""
    args = forms if isinstance(forms, str) else ",".join(str(form) for form in forms)
    return _bindings._filter_plural(n, args, {})


def time_ru(value):
    """Время словами без «Сейчас» и точки: «12 часов» / «12 часов 5 минут».
    value — datetime (например ctx.now) или dict с ключом now."""
    return _bindings._filter_time_ru(value, None, {})


# --- контекст сценария -----------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """Служебное событие (event/protocol_event-триггеры): имя колбэка и поля
    service-запроса, по которым фильтруются триггеры (callback/purpose/frame)."""
    name: str = ""
    callback: str = ""
    purpose: str = ""
    frame: str = ""


class _ActionExecution:
    """Минимальный объект execution, который видит обработчик действия: контекст
    запроса (input/clock/user/device…), как у шага action в Executor, и extension —
    ExtensionContext расширения-владельца действия (TASK-053), встроенному — None."""

    __slots__ = ("ctx", "extension")

    def __init__(self, ctx, extension=None):
        self.ctx = ctx
        self.extension = extension


class ScenarioContext:
    """Контекст py-сценария — единственный аргумент handle(ctx) (TASK-019).

    runtime — живой Runtime (или совместимый объект с scenario_executor); сервисы
    берут реестры из runtime.scenario_executor, ничего не дублируя. ctx — контекст
    запроса в формате шагов движка (input/clock/user/device/params/slots/state…);
    slots и state — ТЕ ЖЕ dict'ы, что у исполнения: правки переживают шаги диалога.
    dry_run=True (проверка сценария) — действия не выполняются, а пишутся в effects.
    extension — ExtensionContext настроек расширения (TASK-053): его ставит
    intelligence.answer для режимов расширений (ctx.extension); у обычных
    py-сценариев и вне режимов он None.
    """

    def __init__(self, runtime, ctx=None, dry_run=False, assets_dir=None):
        self._runtime = runtime
        self._ctx = ctx if isinstance(ctx, dict) else {}
        self._assets_dir = Path(assets_dir) if assets_dir else None  # <id>.assets рядом со скриптом
        self.dry_run = bool(dry_run)
        self.extension = None  # ExtensionContext режима расширения (TASK-053), ставит intelligence.answer
        self.effects = []  # [{kind, name, arguments}] — что dry_run сделал бы (показывает проверка)
        self.slots = self._ctx.get("slots") if isinstance(self._ctx.get("slots"), dict) else {}
        self.state = self._ctx.get("state") if isinstance(self._ctx.get("state"), dict) else {}

    # --- запрос -------------------------------------------------------------------
    @property
    def text(self):
        """Реплика, как её оставил диалоговый слой (нормализованный ASR-вариант, если он применён)."""
        return str((self._ctx.get("input") or {}).get("text") or "")

    @property
    def raw_text(self):
        """Исходная реплика (до нормализации), если она отличалась."""
        source = self._ctx.get("input") or {}
        return str(source.get("text_raw") or source.get("text") or "")

    @property
    def sn(self):
        """SN колонки, от которой пришёл запрос."""
        source = self._ctx.get("input") or {}
        return str(source.get("sn") or (self._ctx.get("device") or {}).get("sn") or "")

    @property
    def event(self):
        """Служебное событие (для event/protocol_event-триггеров), иначе пустое."""
        source = self._ctx.get("input") or {}
        return Event(name=str(source.get("event_name") or ""),
                     callback=str(source.get("callback") or ""),
                     purpose=str(source.get("purpose") or ""),
                     frame=str(source.get("frame") or ""))

    @property
    def ask(self):
        """Данные уточнения, на которое пришло продолжение (TASK-020): dict
        {question, expect} — что было спрослено и какая подсказка парсеру обещана
        (разбор ответа по expect — TASK-022); вне продолжения — None."""
        value = self._ctx.get("ask")
        return dict(value) if isinstance(value, dict) else None

    @property
    def device(self):
        """Запись реестра о колонке: sn, name, room, model, display, light(s), apps, platform, fields."""
        return self._ctx.get("device") or {}

    @property
    def user(self):
        """Профиль владельца колонки: id, name, location, fields (корень user контекста)."""
        return self._ctx.get("user") or {}

    def _services_runtime(self):
        """Живой runtime для сервисов сценария: у объекта из исполнителя (SimpleNamespace)
        нет каталогов состояния и настроек — берётся модульная привязка bind_runtime."""
        runtime = self._runtime
        if hasattr(runtime, "directory") and hasattr(runtime, "settings"):
            return runtime
        return _RUNTIME if _RUNTIME is not None else runtime

    @property
    def settings(self):
        """Настройки эмулятора (только чтение)."""
        return self._services_runtime().settings

    @property
    def owner(self):
        """Владелец: пользователь (id, name), гео (location) — комната колонки в ctx.device.room."""
        return self._ctx.get("user") or {}

    @property
    def devices(self):
        """Устройства дома (1.1.0): колонки владельца из контекста запроса — `home.devices`,
        как их видит JSON `{{home.devices}}` (name/aliases/sn/…). Копия: только чтение."""
        home = self._ctx.get("home")
        devices = home.get("devices") if isinstance(home, dict) else None
        return copy.deepcopy(devices) if isinstance(devices, list) else []

    @property
    def input_timezone(self):
        """Таймзона из `input.timezone` запроса (1.1.0) или None. Порядок выбора таймзоны в
        сценарии (timesync) отличается от ctx.now — сценарий выбирает сам, как в JSON."""
        name = str((self._ctx.get("input") or {}).get("timezone") or "").strip()
        return name or None

    @property
    def params(self):
        """Параметры владельца/устройства/платформы/сервера + effective (docs/contracts/params-db.md)."""
        return self._ctx.get("params") or {}

    # --- 1.4.0: состояние колонки из её запроса (TASK-079) ---------------------------

    @property
    def timers(self):
        """Таймеры колонки из состояния запроса (EnvironmentStateRaw): [{timer_id, start_ts,
        duration, paused, disabled, currently_playing, title}]. Пусто, если колонка ничего
        не сообщает (сухая проверка, запрос без состояния) — своего хранилища нет."""
        return list((self._ctx.get("station") or {}).get("timers") or [])

    @property
    def alarms(self):
        """Будильники колонки из состояния запроса: [{alarm_id, time{hours,minutes,seconds},
        dates, weekdays, disabled, currently_playing, snooze_time, timezone}]."""
        return list((self._ctx.get("station") or {}).get("alarms") or [])

    @property
    def screens(self):
        """Открытые экраны приложения «Часы» у колонки: timer_active/timer_info/timer_list,
        alarm_active/alarm_list/alarm_edit (значения — идентификатор или true)."""
        return dict((self._ctx.get("station") or {}).get("screens") or {})

    @property
    def player(self):
        """Состояние штатного плеера колонки из её запроса (capability 46, TASK-087):
        {state: idle/playing/paused/…, playing, title, subtitle, played_ms, duration_ms}.
        Пустой dict, если колонка ничего не сообщает — своей очереди у эмулятора нет."""
        return dict((self._ctx.get("station") or {}).get("player") or {})

    @property
    def volume(self):
        """Громкость колонки из её запроса (capability 23 TVolumeCapability, TASK-083):
        {muted, level, max_level, percent_level}. Пустой dict, если колонка ничего не
        сообщает — своего учёта громкости на сервере нет."""
        return dict((self._ctx.get("station") or {}).get("volume") or {})

    @property
    def now(self):
        """Текущий момент — aware datetime в таймзоне устройства (params.device/user/effective
        .timezone, затем input.timezone; без имени — таймзона сервера)."""
        now = (self._ctx.get("clock") or {}).get("now")
        if not isinstance(now, datetime.datetime):
            now = datetime.datetime.now()
        params = self._ctx.get("params") or {}
        name = ""
        for scope in ("device", "user", "effective"):
            name = str((params.get(scope) or {}).get("timezone") or "").strip()
            if name:
                break
        if not name:
            name = str((self._ctx.get("input") or {}).get("timezone") or "").strip()
        try:
            tz = ZoneInfo(name) if name else None
        except Exception:  # неизвестное имя — таймзона сервера
            tz = None
        if now.tzinfo is None:  # наивное время контекста — локальное время сервера
            return now.astimezone(tz) if tz else now.astimezone()
        return now.astimezone(tz) if tz else now

    # --- сервисы: обёртки над реестрами Executor -----------------------------------
    def _executor(self):
        executor = getattr(self._runtime, "scenario_executor", None)
        if executor is None:
            raise SDKError("в runtime нет scenario_executor — сервисы SDK недоступны")
        return executor

    async def action(self, name, **arguments):
        """Действие из реестра Executor (timer.start, alarm.start, timer.cancel,
        device.push, light.set — см. docs/contracts/scenario-json-v2.md). В dry_run
        действие НЕ выполняется: запись {kind, name, arguments} в ctx.effects, вернётся None.
        Обработчик получает execution.extension — ExtensionContext расширения-владельца
        действия (TASK-053), встроенному — None."""
        if self.dry_run:
            self.effects.append({"kind": "action", "name": str(name), "arguments": dict(arguments)})
            return None
        handler = (getattr(self._executor(), "actions", None) or {}).get(str(name))
        if handler is None:
            raise SDKError(f"нет действия {name!r}")
        result = handler(dict(arguments),
                         _ActionExecution(self._ctx, extension=action_context(str(name))))
        if inspect.isawaitable(result):
            result = await result
        return result

    async def send_push(self, sn, action, params=None):
        """Push-действие каталога (actions.py CATALOG) выбранному устройству (1.3.0):
        громкость, bluetooth, LED-анимация... Устройство не в сети — SDKError."""
        return await _push(self._runtime, sn, action, params)

    async def send_directive(self, sn, name, payload=None):
        """Директива Push произвольного вида выбранному устройству (1.3.0): тот же
        конверт Push, без проверки каталога. Не в сети — SDKError."""
        from . import actions as _actions
        from .runtime import ROOT
        from .schema import load_type
        message = _actions.push_directive(load_type(ROOT / "schemas", "alice-api.pb",
                                                    "NAlice.NAliceApi.TServerMessage"), name, payload)
        if not await self._runtime.send_message(str(sn), message):
            raise SDKError(f"устройство {sn} не в сети — директива не отправлена")

    async def fetch_json(self, url, ttl=0):
        """GET JSON через общий fetcher движка (кеш TTL, лимит размера, честная ошибка)."""
        fetcher = getattr(self._executor(), "fetcher", None)
        if fetcher is None:
            raise SDKError("fetch недоступен: у Executor нет fetcher")
        return await fetcher(str(url), {}, int(ttl or 0))

    async def dataset(self, name, key, max_age=0, args=None):
        """Данные плагина (датасет): свежая запись из БД (age ≤ max_age секунд) — сразу;
        устаревшая/отсутствующая — пинок плагина (шаги load/refresh), который пишет
        executor. args — аргументы плагина (как шаг refresh). Нет плагина — вернётся
        устаревшая запись, а без записи — SDKError."""
        executor = self._executor()
        dataset_id = "dataset:" + str(name)
        record = None
        if executor.data_store is not None:
            record = executor.data_store.scenario_data_record(dataset_id, "system", "", str(key))
        if record is not None:
            value, updated = record
            if not max_age or max(0, int(time.time() - updated)) <= max_age:
                return value
        plugin = (getattr(executor, "plugins", None) or {}).get(str(name))
        if plugin is None:
            if record is not None:
                return record[0]  # устаревшие данные лучше сбоя сценария
            raise SDKError(f"нет данных и плагина для датасета {name!r}")
        value = await plugin(key=str(key), args=dict(args or {}))
        if executor.data_store is not None:  # запись в БД делает executor (как шаг refresh)
            executor.data_store.scenario_data_set(dataset_id, "system", "", str(key), value)
        return value

    def lexicon(self, name):
        """Общий словарь (server_emulator/scenarios/lexicons.json) с приоритетом
        словаря сценария; неизвестное имя → {}."""
        name = str(name)
        merged = self._ctx.get("lexicon")
        if isinstance(merged, dict) and name in merged:
            return merged[name]
        return (getattr(self._executor(), "lexicons", None) or {}).get(name, {})

    # --- 1.1.0: парсеры текста ------------------------------------------------
    # Обёртки над scenario_engine/parsers.py: разбор фраз для py-сценариев.
    # text по умолчанию — ctx.text; словарь — имя раздела lexicon сценария / общего
    # словаря либо готовый dict/list; strip — фразы (строка или список), вырезаемые из
    # текста до разбора (алиасы колонки-адресата).

    def _lexicon_data(self, value):
        """Словарь парсера: dict/list — как есть, строка — имя (сценарий → общие), иначе {}."""
        if isinstance(value, (dict, list, tuple)):
            return value
        if isinstance(value, str):
            merged = self._ctx.get("lexicon")
            if isinstance(merged, dict) and value in merged:
                return merged[value]
            executor = getattr(self._runtime, "scenario_executor", None)
            return (getattr(executor, "lexicons", None) or {}).get(value, {})
        return {}

    def _source_text(self, text, strip=None):
        text = self.text if text is None else str(text)
        for phrase in (strip if isinstance(strip, (list, tuple)) else [strip] if strip else []):
            if phrase:
                text = text.lower().replace(str(phrase).lower(), " ")
        return text

    def find_in(self, items, text=None, field="name", fields=None, contains_only=False,
                lexicon=None, strip=None):
        """Элемент списка, названный в тексте (парсер list): поле field (или
        любое из fields; значение-список — любой элемент) входит в текст целыми словами,
        а без contains_only — ещё и остаток текста входит в него; побеждает самое длинное
        имя. lexicon — слова-команды («открой»), снимаемые с начала (имя раздела или
        список). Пример: ctx.find_in(ctx.device["apps"], lexicon="commands"). Нет — None."""
        return _parsers.parse_list(self._source_text(text, strip), self._lexicon_data(lexicon),
                                   items=items, field=field, fields=fields,
                                   contains_only=contains_only)

    def find_device(self, text=None, field="name", fields=None, contains_only=False,
                    lexicon=None, strip=None):
        """Устройство дома (ctx.devices), названное в тексте: find_in(ctx.devices, …).
        Адресат по имени или алиасу — find_device(fields=["name", "aliases"], contains_only=True)."""
        return self.find_in(self.devices, text, field=field, fields=fields,
                            contains_only=contains_only, lexicon=lexicon, strip=strip)

    def parse_duration(self, text=None, lexicon="duration", strip=None):
        """Длительность в секундах («на пять минут» → 300, «полчаса» → 1800) или None;
        словарь — общий «duration» (numbers/units/fixed)."""
        return _parsers.parse_duration(self._source_text(text, strip), self._lexicon_data(lexicon))

    def parse_time(self, text=None, lexicon="time", strip=None):
        """Время «ЧЧ:ММ» в формате движка («на семь тридцать» → «07:30», «8:30» → «08:30»)
        или None; для вывода — format_hhmm. Словарь — общий «time» (numbers/minutes)."""
        return _parsers.parse_time(self._source_text(text, strip), self._lexicon_data(lexicon))

    def parse_number(self, text=None, minimum=None, maximum=None, lexicon="numbers", strip=None):
        """Число из цифр или слов («двадцать пять» → 25) или None; вне [minimum, maximum] — None."""
        return _parsers.parse_number(self._source_text(text, strip), self._lexicon_data(lexicon),
                                     minimum=minimum, maximum=maximum)

    def parse_lexicon(self, lexicon_or_name, text=None, strip=None):
        """Значение по словарю {фраза: значение} (или списку фраз): самая длинная фраза,
        найденная целыми словами (`слово*` — префикс). lexicon_or_name — dict/list или имя
        раздела lexicon сценария / общего словаря (power, rooms, modes, emotions…). Нет — None."""
        return _parsers.parse_lexicon(self._source_text(text, strip), self._lexicon_data(lexicon_or_name))

    def parse_rest(self, words, text=None, strip=None):
        """Остаток фразы после слова-команды (words — список или имя словаря: «открой
        Ютуб» → «ютуб»); без команды в начале — None."""
        return _parsers.parse_rest(self._source_text(text, strip), self._lexicon_data(words))

    # --- 1.1.0: датасеты с возрастом, ресурсы ---------------------------------------
    @staticmethod
    def _record_view(record, max_age):
        value, updated = record
        age = max(0, int(time.time() - updated))
        fresh = not max_age or age <= max_age
        return {"value": value, "updated_at": updated, "age_s": age, "fresh": fresh, "stale": not fresh}

    async def dataset_record(self, name, key, max_age=0, args=None, timeout=None):
        """Запись датасета с возрастом — та же логика, что у шагов load/refresh/load:
        {"value", "updated_at", "age_s", "fresh", "stale"} (fresh — возраст ≤ max_age,
        max_age=0 — любая запись свежа; stale = не fresh).

        Свежая запись — сразу. Иначе плагин обновляет её (Executor.refresh_dataset: запись
        в БД делает executor, параллельные вызовы ждут одно обновление; timeout секунд,
        по умолчанию 10; по таймауту плагин дорабатывает в фоне). Сбой/таймаут плагина —
        откат на старую запись (stale=True), без записи — None. Нет плагина: старая запись
        или SDKError. Как отвечать на устаревшие данные или None — решает сценарий."""
        executor = self._executor()
        store = getattr(executor, "data_store", None)
        dataset_id = "dataset:" + str(name)

        def read():
            return None if store is None else store.scenario_data_record(dataset_id, "system", "", str(key))

        record = read()
        if record is not None and (not max_age or max(0, int(time.time() - record[1])) <= max_age):
            return self._record_view(record, max_age)
        if str(name) not in (getattr(executor, "plugins", None) or {}):
            if record is not None:
                return self._record_view(record, max_age)  # устаревшие данные лучше сбоя сценария
            raise SDKError(f"нет данных и плагина для датасета {name!r}")
        seconds = DATASET_REFRESH_TIMEOUT_S if timeout is None else max(0.1, float(timeout))
        try:
            value = await executor.refresh_dataset(str(name), key, args, seconds)
        except Exception:  # сбой плагина или таймаут (TimeoutError): старая запись или None
            return None if record is None else self._record_view(record, max_age)
        fresh_record = read()
        if fresh_record is None:  # без БД: значение плагина «только что получено»
            fresh_record = (value, time.time())
        return self._record_view(fresh_record, max_age)

    def asset(self, name):
        """JSON-ресурс сценария (1.1.0): файл `<имя>.json` (или с указанным расширением)
        из каталога `<id>.assets/` рядом со скриптом — большие шаблоны (Div2-карточка).
        Имя без пути (`..`, `/`, `\\`, `:` запрещены) — иначе SDKError; нет каталога/файла
        или битый JSON — тоже SDKError. Каждый вызов читает файл заново."""
        raw = str(name or "").strip()
        if not raw or ".." in raw or "/" in raw or "\\" in raw or ":" in raw:
            raise SDKError(f"недопустимое имя ресурса {name!r}")
        if self._assets_dir is None:
            raise SDKError("у сценария нет каталога ресурсов")
        path = self._assets_dir / (raw if "." in raw else raw + ".json")
        if not path.is_file():
            raise SDKError(f"нет ресурса {raw!r} в {self._assets_dir.name}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SDKError(f"ресурс {raw!r} не читается: {exc}") from exc
