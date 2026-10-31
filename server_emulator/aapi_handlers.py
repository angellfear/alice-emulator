"""Реестр обработчиков AAPI-событий колонки (WS).

Обработчик объявляется рядом со своим кодом декоратором @aapi_handler(trigger, …, enabled=…).
Conversation.receive перебирает обработчики события в порядке объявления, пока один не вернёт
True. has_handler отвечает, есть ли у события обработчик при данных настройках, ничего не
выполняя: опция «Запись» → «Нет обработчиков» (TASK-054). Новый обработчик виден сразу,
отдельного списка событий нет.

enabled(settings) — условие по настройкам устройства (флаги профиля, стандартные реплики).
То, что зависит от состояния соединения (открыт ли канал и т. п.), обработчик решает сам,
вернув False; для has_handler такое событие считается обработанным.

TASK-058: у записи есть источник (builtin | callback — колбэк-обработчик; расширения
добавят extension:<id> через SDK), имя/описание и счётчики в памяти: сколько раз событие
пришло, сколько обработано, время последнего. Колбэки (@@mm_* и пр.) не исполняют сценарий
сами: route() отображает колбэк с его фильтрами во внутреннее событие-запрос данных
(например informers.requested), ответ собирает сценарий-поставщик (триггер kind: provide),
без поставщика — умолчание колбэка (тихое подтверждение).
"""
from __future__ import annotations

import time

_HANDLERS: dict[str, list] = {}  # trigger → [(handler, enabled, meta)] в порядке объявления
_COUNTERS: dict[str, dict] = {}  # trigger → {seen, handled, last}


def aapi_handler(*triggers, enabled=None, source="builtin", name="", description=""):
    """Зарегистрировать async handler(conversation, incoming, trigger) -> bool для событий triggers."""
    def register(handler):
        for trigger in triggers:
            meta = {"source": source, "name": name or handler.__name__.lstrip("_"),
                    "description": (description or (handler.__doc__ or "").strip().splitlines()[0]
                                    if (description or handler.__doc__) else "")}
            _HANDLERS.setdefault(trigger, []).append((handler, enabled, meta))
        return handler
    return register


def handlers(trigger, settings):
    """Обработчики события, включённые при этих настройках, в порядке объявления."""
    return [handler for handler, enabled, _ in _HANDLERS.get(trigger, ())
            if enabled is None or enabled(settings)]


def has_handler(trigger, settings):
    """Есть ли у события обработчик при этих настройках (без выполнения)."""
    return bool(handlers(trigger, settings))


def count(trigger, handled=False):
    """Счётчик события (в памяти процесса). count(trigger) — событие пришло (вызывается
    один раз при входе); count(trigger, handled=True) — обработчик его обработал."""
    entry = _COUNTERS.setdefault(trigger, {"seen": 0, "handled": 0, "last": 0.0})
    if handled:
        entry["handled"] += 1
    else:
        entry["seen"] += 1
        entry["last"] = time.time()
    return entry


def counters(trigger):
    return dict(_COUNTERS.get(trigger) or {"seen": 0, "handled": 0, "last": 0.0})


def registry(settings):
    """Все записи реестра для вкладки «WS»: событие, источник, имя, описание, условие
    включения текстом, включённость при текущих настройках, счётчики."""
    entries = []
    for trigger, items in _HANDLERS.items():
        for handler, enabled, meta in items:
            entries.append({
                "trigger": trigger,
                "source": meta["source"],
                "name": meta["name"],
                "description": meta["description"],
                "enabled_text": "" if enabled is None else (enabled.__doc__ or "").strip(),
                "enabled": enabled is None or bool(enabled(settings)),
                "counters": counters(trigger),
            })
    return sorted(entries, key=lambda item: (item["trigger"], item["source"], item["name"]))


# --- колбэки → внутренние события (TASK-058) -------------------------------------

# (event_name, match(fields) → bool, provide_event, описание) в порядке проверки;
# fields — {callback, purpose, frame} запроса (service_event_fields).
_ROUTES: list[tuple] = []


def callback_route(event_name, provide_event, match=None, description="", as_reason=None):
    """Задать маршрут колбэка: событие колонки (+ фильтры) → внутреннее событие-запрос.
    match None — любой колбэк этого имени. Порядок объявления значим. as_reason — причина
    выбора для отчётов и эталонов в форме прежнего event-триггера (строка или callable(fields))."""
    _ROUTES.append((event_name, match, provide_event, description, as_reason))


def route(event_name, fields):
    """Внутреннее событие-запрос данных для колбэка или None — маршрута нет."""
    for entry in _ROUTES:
        if _matched(entry, event_name, fields):
            return entry[2]
    return None


def route_reason(event_name, fields):
    """Причина выбора в форме прежнего event-триггера (эталоны/отчёты) или None."""
    for entry in _ROUTES:
        if _matched(entry, event_name, fields):
            reason = entry[4]
            if callable(reason):
                reason = reason(fields or {})
            return reason
    return None


def _matched(entry, event_name, fields):
    name, match = entry[0], entry[1]
    return name.lower() == (event_name or "").lower() and (match is None or match(fields or {}))


def routes():
    """Маршруты колбэков для вкладки «WS» (поставщика дописывает API)."""
    return [{"event": name, "provide": provide, "description": description,
             "filters": None if match is None else (match.__doc__ or "").strip()}
            for name, match, provide, description, _ in _ROUTES]


def _is_informers(fields):
    """callback=NAlice.TGetInformersTypedCallback или purpose=get_informers"""
    return fields.get("callback") == "NAlice.TGetInformersTypedCallback" or fields.get("purpose") == "get_informers"


def _is_weather_tap(fields):
    """purpose=weather_informer_tap_action (тап по виджету погоды на экране станции)"""
    return fields.get("purpose") == "weather_informer_tap_action"


def _is_timesync(fields):
    """callback=NAlice.TDeviceControlTimesyncTypedCallback или purpose=sync_timezone"""
    return fields.get("callback") == "NAlice.TDeviceControlTimesyncTypedCallback" or fields.get("purpose") == "sync_timezone"


# Маршруты колбэков (TASK-058): порядок проверки значим — именованные раньше общего ack.
# as_reason — форма причины прежнего event-триггера: эталон scenarios.json и отчёты не меняются
callback_route('@@mm_typed_callback', 'informers.requested', _is_informers, 'информеры/виджет погоды',
               lambda fields: "event:@@mm_typed_callback[" +
                              ("callback=%s" % fields["callback"] if fields.get("callback") else "purpose=get_informers") + "]")
callback_route('@@mm_typed_callback', 'timesync.requested', _is_timesync, 'синхронизация времени',
               lambda fields: "event:@@mm_typed_callback[" +
                              ("callback=%s" % fields["callback"] if fields.get("callback") else "purpose=sync_timezone") + "]")
callback_route('@@mm_typed_callback', 'callbacks.ack', None, 'прочие колбэки приложения — тихое подтверждение',
               'event:@@mm_typed_callback')
callback_route('@@mm_semantic_frame', 'weather.opened', _is_weather_tap, 'тап по виджету погоды',
               'event:@@mm_semantic_frame[purpose=weather_informer_tap_action]')
callback_route('@@mm_semantic_frame', 'callbacks.ack', None, 'семантический фрейм — тихое подтверждение',
               'event:@@mm_semantic_frame')
callback_route('@@mm_stack_engine_get_next', 'callbacks.ack', None, 'стек плеера — тихое подтверждение',
               'event:@@mm_stack_engine_get_next')
callback_route('music_thin_client_on_started', 'callbacks.ack', None, 'плеер колонки — тихое подтверждение',
               'event:music_thin_client_on_started')
callback_route('music_thin_client_on_stopped', 'callbacks.ack', None, 'плеер колонки — тихое подтверждение',
               'event:music_thin_client_on_stopped')
callback_route('@@agent_say', 'agent_say.requested', None, 'сообщение агента (задания по расписанию)',
               'event:@@agent_say')
callback_route('@@timer_fired', 'timer_fired.requested', None, 'срабатывание таймера/будильника',
               'event:@@timer_fired')


# --- обработчики и наблюдатели расширений (TASK-059, sdk.aapi_handler / sdk.on_event) ---

_OBSERVERS: dict[str, list] = {}  # trigger → [(handler, meta)] — наблюдатели, выбор не участвуют


def drop_extensions():
    """Убрать обработчики и наблюдателей расширений (пересборка реестра при
    включении/выключении/удалении): встроенные и колбэк-маршруты остаются."""
    for table in (_HANDLERS, _OBSERVERS):
        for trigger in list(table):
            table[trigger] = [item for item in table[trigger]
                              if not item[-1]["source"].startswith("extension:")]
            if not table[trigger]:
                del table[trigger]


def register_extension(trigger, handler, enabled, ext_id, name="", description="", observer=False):
    """Обработчик/наблюдатель расширения: источник extension:<id>. Регистрируется ПОСЛЕ
    встроенных — расширение не перекрывает встроенные ответы протокола без явного намерения.
    В реестр идёт адаптер (aapi_handlers-сигнатура → sdk.adapt_aapi_call): наружу публичная
    сигнатура handler(event, ctx)."""
    meta = {"source": f"extension:{ext_id}",
            "name": name or getattr(handler, "__name__", "handler"),
            "description": description}

    async def call(conversation, incoming, trigger):
        from .sdk import adapt_aapi_call
        return bool(await adapt_aapi_call(handler, conversation, incoming, trigger, meta))

    call.__name__ = meta["name"]
    table = _OBSERVERS if observer else _HANDLERS
    table.setdefault(str(trigger), []).append((call, enabled, meta))


def observers(trigger):
    """Наблюдатели события (sdk.on_event): вызываются для каждого события, в выборе
    обработчика не участвуют."""
    return [handler for handler, _enabled, _meta in _OBSERVERS.get(str(trigger), ())]


def notify_observers(conversation, incoming, trigger):
    """Запустить наблюдателей события фоном: сбой или зависание наблюдателя не тормозит
    протокол и не мешает ответу. Наблюдателей нет — ничего не происходит."""
    items = list(_OBSERVERS.get(str(trigger), ()))
    if not items:
        return
    import asyncio

    async def run(call):
        try:
            await call(conversation, incoming, str(trigger))
        except Exception:  # noqa: BLE001 — наблюдатель не ломает обработку события
            pass

    for call, _enabled, _meta in items:
        try:
            asyncio.get_running_loop().create_task(run(call))
        except RuntimeError:
            pass  # вне цикла событий (тесты реестра) — наблюдатели не вызываются
