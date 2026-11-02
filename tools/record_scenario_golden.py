"""Эталонные ответы сценариев: dry-run прогон и запись (TASK-023).

Для каждого сценария каталога прогоняется набор фраз и служебных колбэков из
триггеров плюс вариации со значениями («таймер на 5 минут», «свет в спальне»,
«громкость 5») — через стандартный диалоговый путь dialog.handle_text /
dialog.handle_event, тот же, что в voice.py и tests/test_timesync_scenario.py.
ask-уточнения продолжаются ответом из кейса (continuation диалога).

Детерминизм: время (ctx.clock.now), гео владельца (params) и данные погоды
(заглушка плагина weather) фиксированы; из ответов берётся проекция без
изменчивых полей — CardId/ResponseId/MessageId (uuid4), SequenceNumber,
Timings не записываются; трасса шагов (trace) тоже не записывается — деталь
исполнителя (у JSON-flow и py-сценария она разная). Результат: сценарий, исход (status), текст карточек,
speech, директивы (payload/payload_raw) и effects (вызовы action-примитивов).

Использование:
    python tools/record_scenario_golden.py            # встроенная библиотека
                                                      # server_emulator/scenarios/*/*.json
    python tools/record_scenario_golden.py DIR        # каталог сценариев <раздел>/*.json
    python tools/record_scenario_golden.py DIR --output golden.json

Программная точка входа — record(catalog_dir=None, workdir=None, cases=None):
каталог можно подменить (самопроверка пакета расширения при установке прогоняет
его сценарии тем же механизмом), cases — свои фразы. Реестра расширений здесь
нет: только прогон и запись.

Эталон по умолчанию: tests/golden/scenarios.json; тест tests/test_scenario_golden.py
повторяет прогон и сверяет байт-в-байт (ключи отсортированы, два запуска подряд
дают одинаковый текст).
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import datetime
import inspect
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.protobuf import json_format  # noqa: E402

from server_emulator.runtime import Runtime  # noqa: E402
from server_emulator.schema import load_type  # noqa: E402
from server_emulator.scenario_engine.router import trigger_phrases  # noqa: E402
from server_emulator.voice import service_event_fields  # noqa: E402

CLIENT = load_type(ROOT / "server_emulator" / "schemas", "alice-api.pb", "NAlice.NAliceApi.TClientMessage")
SERVER = load_type(ROOT / "server_emulator" / "schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")

DEFAULT_GOLDEN = ROOT / "tests" / "golden" / "scenarios.json"

# --- фиксированный мир прогона: владелец, колонка, гео, время, погода --------
OWNER = "owner"
SN = "SN-GOLDEN"          # «текущая» колонка — адресат всех фраз
SN_OTHER = "SN-DUO32"     # вторая колонка владельца — адресация по имени
OTHER_NAME = "Дуо 32"
LAT, LON = 55.755826, 37.617326
TIMEZONE = "Asia/Tashkent"
NOW = datetime.datetime(2026, 10, 27, 21, 7)  # вторник, вечер, осень (сезон погоды)
NOW_TS = int(NOW.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5)))
             .timestamp())  # Asia/Tashkent (+5, DST нет) — start_ts таймеров колонки
STATION = {"timers": [], "alarms": [], "screens": {}}  # применяет apply_setup
CLOCK_ID = "0b000000-00004000-80000000-00000000"  # id нового таймера/будильника в прогоне
FORECAST = {
    "current": {"temperature_2m": 12.4, "apparent_temperature": 10.1, "weather_code": 3, "is_day": 1},
    "hourly": {"time": [f"2026-10-27T{h % 24:02d}:00" for h in range(12, 36)],
               "temperature_2m": [10 + h % 5 for h in range(24)], "weather_code": [3] * 24,
               "is_day": [1] * 24},
    "daily": {"temperature_2m_max": [14.2], "temperature_2m_min": [8.1],
              "precipitation_probability_max": [10]},
}
WEATHER_KEY = f"{LAT},{LON}"


async def weather_stub(key, args):
    """Заглушка плагина weather: фиксированный прогноз без сети."""
    return copy.deepcopy(FORECAST)


async def weather_offline(key, args):
    """Заглушка «провайдер недоступен»: ветка no_data/тихий ответ."""
    raise ConnectionError("провайдер погоды недоступен")


def service_request(purpose="", timezone=TIMEZONE, name="@@mm_typed_callback"):
    """TextInput с server_action — как на wire (tests/test_timesync_scenario.py)."""
    payload = {"request_params": {"disable_output_speech": True, "is_parallel": True},
               "analytics": {"purpose": purpose, "origin": "SmartSpeaker"}}
    return json_format.ParseDict({"Event": {"TextInput": {
        "Header": {"RequestId": "golden-req"},
        "Application": {"Timezone": timezone, "DeviceId": SN},
        "Request": {"Event": {"Type": "server_action", "Name": name, "Payload": payload}}}}}, CLIENT())


# --- подготовка runtime и запись effects --------------------------------------

def attach_effects_recorder(runtime):
    """Оборачивает action-примитивы executor'а: каждый вызов → (ref, arguments,
    result). id-поля результата (uuid таймера/будильника) стираются."""
    effects = []

    def scrub(value):
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items() if key != "id"}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    def make_wrapper(ref, handler):
        def wrapper(arguments, execution):
            entry = {"ref": ref, "arguments": scrub(copy.deepcopy(arguments))}
            effects.append(entry)
            result = handler(arguments, execution)
            if inspect.isawaitable(result):
                async def chain():
                    value = await result
                    if value is not None:
                        entry["result"] = scrub(copy.deepcopy(value))
                    return value
                return chain()
            if result is not None:
                entry["result"] = scrub(copy.deepcopy(result))
            return result
        return wrapper

    for ref, handler in list(runtime.dialog.executor.actions.items()):
        runtime.dialog.executor.actions[ref] = make_wrapper(ref, handler)
    return effects


def make_runtime(workdir):
    """Владелец + две колонки + гео дома (params) + заглушка погоды."""
    runtime = Runtime(Path(workdir))
    runtime.identities.mutate({"operation": "user", "id": OWNER, "name": "Alex"})
    runtime.identities.mutate({"operation": "device", "sn": SN, "user_id": OWNER, "name": "Станция"})
    runtime.identities.mutate({"operation": "state", "sn": SN,
                               "state": {"apps": [{"name": "ютуб"}, {"name": "спотифай"}]}})
    runtime.identities.mutate({"operation": "device", "sn": SN_OTHER, "user_id": OWNER,
                               "name": OTHER_NAME})
    runtime.quasar_params.set_value(f"user:{OWNER}", "user.location.latitude", LAT)
    runtime.quasar_params.set_value(f"user:{OWNER}", "user.location.longitude", LON)
    runtime.scenario_plugins["weather"] = weather_stub
    effects = attach_effects_recorder(runtime)
    return runtime, effects


def apply_setup(runtime, name):
    """Состояние под кейс (порядок кейсов внутри сценария значим)."""
    if not name:
        STATION.update({"timers": [], "alarms": [], "screens": {}})
        return
    if name == "no_lights":
        runtime.identities.mutate({"operation": "state", "sn": SN, "state": {}})
    elif name == "lights_kitchen_bedroom":
        runtime.identities.mutate({"operation": "state", "sn": SN, "state": {
            "light": {"кухня": {"on": False, "brightness": 70}, "спальня": {"on": True}}}})
    elif name == "station_timers":  # TASK-079: колонка сообщает два активных таймера
        STATION.update({
            "timers": [{"timer_id": "11111111-22222222-33333333-44444444",
                        "start_ts": NOW_TS - (75 * 60 - 47 * 60 - 12), "duration": 75 * 60,
                        "paused": False, "disabled": False, "currently_playing": False,
                        "title": ""},
                       {"timer_id": "55555555-66666666-77777777-88888888",
                        "start_ts": NOW_TS - 60, "duration": 90 * 60,
                        "paused": False, "disabled": False, "currently_playing": False,
                        "title": ""}],
            "alarms": [], "screens": {}})
    elif name == "station_ringing_alarm":  # «хватит» по сигналу будильника
        STATION.update({"timers": [], "screens": {},
                        "alarms": [{"alarm_id": "aaaaaaaa-bbbbbbbb-cccccccc-dddddddd",
                                    "time": {"hours": 7, "minutes": 30, "seconds": 0},
                                    "dates": [], "weekdays": [], "disabled": False,
                                    "currently_playing": True, "snooze_time": 10,
                                    "timezone": TIMEZONE}]})
    elif name == "station_ringing_timer":  # истёкший активный таймер — сигнал звенит
        STATION.update({"alarms": [], "screens": {},
                        "timers": [{"timer_id": "99999999-aaaaaaaa-bbbbbbbb-cccccccc",
                                    "start_ts": NOW_TS - 70, "duration": 60,
                                    "paused": False, "disabled": False,
                                    "currently_playing": False, "title": ""}]})
    elif name == "screen_timer_list":  # у колонки откры экран списка таймеров
        STATION.update({"timers": [], "alarms": [], "screens": {"timer_list": True}})
    elif name == "station_idle":  # колонка ничего не сообщает, id для директив фиксирован
        STATION.update({"timers": [], "alarms": [], "screens": {}, "clock_id": CLOCK_ID})
    elif name == "station_alarm_evening":  # будильник на вечер: id фиксирован, экранов нет
        STATION.update({"timers": [], "alarms": [], "screens": {}, "clock_id": CLOCK_ID})
    elif name == "weather_stub":
        runtime.scenario_plugins["weather"] = weather_stub
        runtime.store.scenario_data_set("dataset:weather", "system", "", WEATHER_KEY,
                                        copy.deepcopy(FORECAST))
    elif name == "weather_absent":
        runtime.scenario_plugins["weather"] = weather_offline
        runtime.store.scenario_data_delete("dataset:weather", "system", "", WEATHER_KEY)
    else:
        raise ValueError(f"неизвестный setup {name!r}")


# --- контексты запроса (повторяют voice.py / test_timesync_scenario.py) -------

def base_ctx(runtime, dialog_id, sequence=1, text=""):
    station = copy.deepcopy(STATION)
    state = {}
    if station.pop("clock_id", None):  # id нового таймера/будильника — из прогона, не uuid
        state["clock_id"] = STATION["clock_id"]
    ctx = {"input": {"message_id": "golden-msg", "request_id": "golden-req",
                     "dialog_id": dialog_id, "sn": SN, "text": text},
           "clock": {"now": NOW}, "sequence": sequence,
           "state": state,
           "station": station}  # TASK-079: таймеры/будильники колонки
    ctx.update(runtime._scenario_context(sn=SN))
    animations = getattr(runtime, "animations", None)
    if animations is not None:
        ctx["animations"] = {a["id"]: a["url"] for a in animations.list() if a.get("url")}
    return ctx


def callback_ctx(runtime, dialog_id, purpose="", timezone=TIMEZONE, name="@@mm_typed_callback"):
    """Контекст служебного колбэка: поля wire-запроса + контекст сценариев."""
    request = service_request(purpose=purpose, timezone=timezone, name=name)
    fields = service_event_fields(request.Event.TextInput, request.Event.TextInput.Request.Event)
    ctx = base_ctx(runtime, dialog_id)
    ctx["input"].update(fields)
    return ctx


def event_ctx(runtime, dialog_id, event_name, fields=None):
    """Контекст служебного события без TextInput (@@agent_say)."""
    ctx = base_ctx(runtime, dialog_id)
    ctx["input"].update({"event_name": event_name, "purpose": "", "callback": "", "frame": ""})
    ctx["input"].update(fields or {})
    return ctx


# --- проекция ответов: без uuid/timestamps -------------------------------------

def directive_view(directive):
    view = {"name": directive.Name, "type": directive.Type,
            "is_led_silent": directive.IsLedSilent}
    if directive.AnalyticsType:
        view["analytics"] = directive.AnalyticsType
    payload = json_format.MessageToDict(directive.Payload, preserving_proto_field_name=True) \
        if len(directive.Payload.fields) else {}
    if payload:
        view["payload"] = payload
    if directive.PayloadRaw:
        import base64
        view["payload_raw"] = base64.b64encode(directive.PayloadRaw).decode()
    return view


def message_view(message):
    """Детерминированная проекция TServerMessage: карточки, speech, директивы."""
    if message.WhichOneof("Message") != "Event":
        return {"event": None}
    event = message.Event
    kind = event.WhichOneof("Event")
    if kind != "AliceResponse":
        return {"event": kind}
    response = event.AliceResponse
    return {"event": kind,
            "cards": [{"type": card.Type, "text": card.Text} for card in response.Response.Cards],
            "speech": response.VoiceResponse.OutputSpeech.Text,
            "has_voice": response.VoiceResponse.HasVoiceResponse,
            "directives": [directive_view(d) for d in response.Response.Directives]}


def report_view(report, sent=None):
    """Отчёт прогона: исход, текст, speech, директивы. Трасса шагов (trace) не пишется:
    это деталь исполнителя (у JSON-flow и py-сценария она разная), эталон фиксирует
    наблюдаемое поведение. sent — доставленные байты (путь handle_event)."""
    view = {"handled": bool(report.get("handled")), "reason": report.get("reason"),
            "status": report.get("status"), "scenario": report.get("scenario"),
            "speech": report.get("speech") or "", "speech_parts": report.get("speech_parts") or []}
    if sent is not None:
        view["messages"] = [message_view(SERVER.FromString(packet[4:])) for packet in sent]
    elif "messages" in report:
        view["messages"] = [message_view(message) for message in report["messages"]]
    return view


# --- прогон кейсов ---------------------------------------------------------------

async def run_case(runtime, effects, case):
    """Один кейс: фраза через handle_text (deliver=False, как голосовой тракт)
    или колбэк/событие через handle_event (доставка в сборщик байтов).
    status awaiting + answer → продолжение диалога той же репликой."""
    dialog_id = "golden-" + case["name"]
    effects.clear()
    runs = []
    kind = case.get("kind") or "text"
    if kind == "text":
        phrase = case["phrase"]
        report = await runtime.dialog.handle_text(
            phrase, base_ctx(runtime, dialog_id, text=phrase), dialog_id, None, deliver=False)
        view = report_view(report)
        view["text"] = phrase
        runs.append(view)
        if report.get("status") == "awaiting" and case.get("answer"):
            answer = case["answer"]
            report = await runtime.dialog.handle_text(
                answer, base_ctx(runtime, dialog_id, sequence=2, text=answer), dialog_id,
                None, deliver=False)
            view = report_view(report)
            view["text"] = answer
            runs.append(view)
    else:
        sent = []

        async def send(data):
            sent.append(data)

        if kind == "callback":
            event_name = case.get("event") or "@@mm_typed_callback"
            ctx = callback_ctx(runtime, dialog_id, purpose=case.get("purpose") or "",
                               timezone=case.get("timezone") or TIMEZONE, name=event_name)
        else:  # событие без TextInput: @@timer_fired, @@agent_say
            event_name = case["event"]
            ctx = event_ctx(runtime, dialog_id, event_name, case.get("fields"))
        # TASK-058: колбэк/событие → внутреннее событие-запрос (та же таблица маршрутов,
        # что у voice.py); provider-сценарий отвечает как раньше — эталоны не меняются.
        # reason в эталоне — событие, каким его видела реплика сценария: для маршрутизированного
        # колбэка восстанавливается форма исходного event-причины.
        from server_emulator.aapi_handlers import route as callback_route, route_reason
        fields = {key: (ctx.get("input") or {}).get(key) for key in ("callback", "purpose", "frame")}
        routed = callback_route(event_name, fields)
        report = await runtime.dialog.handle_event(routed or event_name, ctx, dialog_id, send)
        if routed and str(report.get("reason") or "").startswith("provide:"):
            report = {**report, "reason": route_reason(event_name, fields)}
        runs.append(report_view(report, sent))
    return {"name": case["name"],
            "input": {key: value for key, value in case.items()
                      if key in ("kind", "phrase", "answer", "event", "purpose", "fields")},
            "runs": runs, "effects": copy.deepcopy(effects)}


# --- кейсы встроенной библиотеки ------------------------------------------------
# Фразы — из триггеров; вариации со значениями покрывают ветки extract/branch.

CASES = {
    "device.app.open": [
        {"name": "app.open-found", "kind": "text", "phrase": "открой ютуб"},
        {"name": "app.open-found-second", "kind": "text", "phrase": "запусти спотифай"},
        {"name": "app.open-not-found", "kind": "text", "phrase": "открой телеграм"},
    ],
    "device.bluetooth.off": [
        {"name": "bluetooth.off-ru", "kind": "text", "phrase": "выключи блютуз"},
        {"name": "bluetooth.off-latin", "kind": "text", "phrase": "выключи bluetooth"},
    ],
    "device.bluetooth.on": [
        {"name": "bluetooth.on-ru", "kind": "text", "phrase": "включи блютуз"},
        {"name": "bluetooth.on-latin", "kind": "text", "phrase": "включи bluetooth"},
    ],
    "device.led.emotion": [
        {"name": "led.emotion-joy", "kind": "text", "phrase": "покажи глазки"},
        {"name": "led.emotion-wink", "kind": "text", "phrase": "подмигни"},
    ],
    "device.player.pause": [
        {"name": "player.pause-pause", "kind": "text", "phrase": "пауза"},
        {"name": "player.pause-stop-music", "kind": "text", "phrase": "останови музыку"},
    ],
    "device.volume": [
        {"name": "volume-level", "kind": "text", "phrase": "громкость 5"},
        {"name": "volume-percent", "kind": "text", "phrase": "громкость 50"},
        {"name": "volume-mute", "kind": "text", "phrase": "выключи звук"},
        {"name": "volume-max", "kind": "text", "phrase": "максимальная громкость"},
        {"name": "volume-up", "kind": "text", "phrase": "громче"},
        {"name": "volume-down", "kind": "text", "phrase": "тише"},
    ],
    "informers.clock": [
        {"name": "clock-how-much", "kind": "text", "phrase": "сколько времени"},
        {"name": "clock-which-hour", "kind": "text", "phrase": "который час"},
        {"name": "clock-tell", "kind": "text", "phrase": "скажи время"},
    ],
    "informers.weather": [
        {"name": "weather-phrase", "kind": "text", "phrase": "какая погода", "setup": "weather_stub"},
        {"name": "weather-degrees", "kind": "text", "phrase": "сколько градусов", "setup": "weather_stub"},
        {"name": "weather-tap", "kind": "callback", "event": "@@mm_semantic_frame",
         "purpose": "weather_informer_tap_action", "setup": "weather_stub"},
    ],
    "informers.weather.widget": [
        {"name": "weather.widget-no-data", "kind": "callback", "purpose": "get_informers",
         "setup": "weather_absent"},
        {"name": "weather.widget-with-data", "kind": "callback", "purpose": "get_informers",
         "setup": "weather_stub"},
    ],
    "smart_home.light": [
        {"name": "light-no-lights", "kind": "text", "phrase": "включи свет", "setup": "no_lights"},
        {"name": "light-on-room", "kind": "text", "phrase": "включи свет на кухне",
         "setup": "lights_kitchen_bedroom"},
        {"name": "light-off-room", "kind": "text", "phrase": "выключи свет в спальне"},
        {"name": "light-brightness", "kind": "text", "phrase": "включи свет на кухне на 50"},
        {"name": "light-ambiguous", "kind": "text", "phrase": "зажги свет"},
        {"name": "light-unknown-room", "kind": "text", "phrase": "выключи свет в ванной"},
    ],
    "system.agent_say": [
        {"name": "agent_say-text", "kind": "event", "event": "@@agent_say",
         "fields": {"text": "Напоминание: полить цветы."}},
    ],
    "system.dialog.reset": [
        {"name": "dialog.reset-forget", "kind": "text", "phrase": "забудь всё"},
        {"name": "dialog.reset-restart", "kind": "text", "phrase": "начни заново"},
    ],
    "system.service_ack": [
        {"name": "service_ack-shutter", "kind": "callback", "purpose": "collect_upper_shutter"},
        {"name": "service_ack-screensaver", "kind": "callback", "purpose": "screensaver_callback_get_config"},
        {"name": "service_ack-stack-engine", "kind": "callback", "event": "@@mm_stack_engine_get_next"},
        {"name": "service_ack-music-started", "kind": "callback", "event": "music_thin_client_on_started"},
    ],
    "system.timesync": [
        {"name": "timesync-sync", "kind": "callback", "purpose": "sync_timezone"},
    ],
    "timers.alarm.set": [
        {"name": "alarm.set-time", "kind": "text", "phrase": "будильник на 7:30",
         "setup": "station_alarm_evening"},
        {"name": "alarm.set-ask", "kind": "text", "phrase": "разбуди меня", "answer": "в 7 утра",
         "setup": "station_alarm_evening"},
        {"name": "alarm.set-remote", "kind": "text", "phrase": "заведи будильник на дуо 32 на 6:30"},
    ],
    "timers.ringing.stop": [
        {"name": "ringing.stop-alarm", "kind": "text", "phrase": "хватит",
         "setup": "station_ringing_alarm"},
        {"name": "ringing.stop-timer", "kind": "text", "phrase": "хватит",
         "setup": "station_ringing_timer"},
    ],
    "timers.timer.cancel": [
        {"name": "timer.cancel-none", "kind": "text", "phrase": "отмени таймер"},
        {"name": "timer.cancel-here", "kind": "text", "phrase": "отмени таймер",
         "setup": "station_timers"},
        {"name": "timer.cancel-second", "kind": "text", "phrase": "выключи второй таймер",
         "setup": "station_timers"},
        {"name": "timer.cancel-remote", "kind": "text", "phrase": "отмени таймер на дуо 32",
         "setup": "station_timers"},
    ],
    "timers.timer.list": [
        {"name": "timer.list-remaining", "kind": "text", "phrase": "сколько осталось до таймера",
         "setup": "station_timers"},
        {"name": "timer.list-none", "kind": "text", "phrase": "какие таймеры установлены"},
    ],
    "timers.timer.set": [
        {"name": "timer.set-duration", "kind": "text", "phrase": "таймер на 5 минут",
         "setup": "station_idle"},
        {"name": "timer.set-ask", "kind": "text", "phrase": "поставь таймер", "answer": "на 90 секунд",
         "setup": "station_idle"},
        {"name": "timer.set-remote", "kind": "text", "phrase": "поставь таймер на дуо 32 на 5 минут"},
        {"name": "timer.set-screen-open", "kind": "text", "phrase": "таймер на 1 минуту",
         "setup": "screen_timer_list"},
    ],
}


# --- каталоги -------------------------------------------------------------------

def load_catalog(root):
    """Каталог сценариев из каталога пакета (<раздел>/*.json) — тот же валидатор,
    что у встроенной библиотеки. Битый сценарий поднимает ошибку: самопроверка
    пакета расширения должна падать громко, а не молча пропускать файл."""
    from server_emulator.scenario_engine.library import catalog_order
    from server_emulator.scenario_packages.validate import PackageError, validate_scenario
    root = Path(root)
    categories_path = root / "categories.json"
    categories = ({item["id"] for item in json.loads(categories_path.read_text(encoding="utf-8"))}
                  if categories_path.exists() else None)
    catalog = []
    for path in sorted(root.glob("*/*.json")):
        if path.name in ("categories.json", "lexicons.json"):
            continue
        item = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(item, dict):
            raise ValueError(f"{path}: не JSON-объект")
        try:
            validate_scenario(item)
        except PackageError as exc:
            raise ValueError(f"{path}: {exc}") from exc
        if categories is not None and (item.get("category") != path.parent.name
                                       or path.parent.name not in categories):
            raise ValueError(f"{path}: category {item.get('category')!r} ≠ папке {path.parent.name!r}")
        catalog.append({**item, "_origin": "extension", "_file": f"{path.parent.name}/{path.name}"})
    if not catalog:
        raise ValueError(f"{root}: сценариев не найдено (ожидается <раздел>/*.json)")
    return sorted(catalog, key=catalog_order)


def derive_cases(catalog):
    """Кейсы прямо из триггеров (для каталогов без ручных сценариев): каждая
    фраза phrase-триггера и каждый event-триггер с его фильтрами."""
    entries = []
    for scenario in sorted(catalog, key=lambda item: str(item.get("id"))):
        cases = []
        for index, trigger in enumerate(scenario.get("triggers", []) or []):
            kind = trigger.get("kind")
            if kind == "phrase":
                for phrase in trigger_phrases(trigger):
                    cases.append({"name": f"{scenario['id']}-trigger{index}-"
                                          f"{len(cases)}", "kind": "text", "phrase": phrase})
            elif kind in ("event", "protocol_event"):
                cases.append({"name": f"{scenario['id']}-trigger{index}", "kind": "callback",
                              "event": trigger.get("name"),
                              "purpose": trigger.get("purpose") or "",
                              "fields": {key: trigger[key]
                                         for key in ("callback", "frame") if trigger.get(key)}})
        entries.append((str(scenario.get("id")), cases))
    return entries


def cases_for(catalog):
    """Ручные кейсы встроенной библиотеки + авто-вывод для сценариев без них."""
    by_id = {str(item.get("id")): item for item in catalog}
    entries = []
    for scenario_id in sorted(by_id):
        entries.append((scenario_id, CASES.get(scenario_id) or _derived(by_id[scenario_id])))
    return entries


def _derived(scenario):
    return dict(derive_cases([scenario]))[str(scenario.get("id"))]


# --- запись -----------------------------------------------------------------------

def meta_view(catalog_dir, scenario_ids):
    return {"catalog": "builtin" if catalog_dir is None else str(catalog_dir),
            "location": [LAT, LON], "now": NOW.isoformat(), "owner": OWNER, "sn": SN,
            "timezone": TIMEZONE, "scenarios": scenario_ids}


async def record(catalog_dir=None, workdir=None, cases=None):
    """Прогон каталога сценариев → структура эталона (dict).

    catalog_dir=None — встроенная библиотека; путь — каталог <раздел>/*.json
    (пакет расширения). workdir — каталог состояния runtime (по умолчанию
    временный). cases — [(scenario_id, [case, …])]; по умолчанию ручные кейсы
    CASES + авто-вывод из триггеров для остальных сценариев каталога.
    """
    temp = None
    if workdir is None:
        temp = tempfile.mkdtemp(prefix="scenario-golden-")
        workdir = temp
    try:
        runtime, effects = make_runtime(workdir)
        try:
            catalog = runtime.dialog.scenarios()
            if catalog_dir is not None:
                catalog = load_catalog(catalog_dir)
                runtime.dialog.scenarios = lambda: catalog
            plan = cases if cases is not None else cases_for(catalog)
            entries = []
            for scenario_id, case_list in plan:
                entry_cases = []
                for case in case_list:
                    apply_setup(runtime, case.get("setup"))
                    entry_cases.append(await run_case(runtime, effects, case))
                entries.append({"id": scenario_id, "cases": entry_cases})
            return {"meta": meta_view(catalog_dir, [entry["id"] for entry in entries]),
                    "scenarios": entries}
        finally:
            await runtime.close()
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)


def dump(data):
    """Канонический текст эталона: сортировка ключей, отступ 2, перевод строки."""
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Запись эталонных ответов сценариев (dry-run диалога)")
    parser.add_argument("catalog", nargs="?", default=None,
                        help="каталог сценариев <раздел>/*.json "
                             "(по умолчанию — встроенная библиотека server_emulator/scenarios)")
    parser.add_argument("--output", default=str(DEFAULT_GOLDEN), help="файл эталона")
    args = parser.parse_args(argv)
    data = asyncio.run(record(catalog_dir=args.catalog))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(dump(data), encoding="utf-8")
    cases = sum(len(entry["cases"]) for entry in data["scenarios"])
    print(f"{output}: {len(data['scenarios'])} scenarios, {cases} cases")


if __name__ == "__main__":
    main()
