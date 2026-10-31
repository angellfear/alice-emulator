"""JSON-сценарий оперирует максимумом: триггер-формы (слово/предложение/маска/
список), guard с правом отказа (реплика уходит дальше/в fallback), compose.reply
(карточка/реплика/любые директивы с bindings), контекст user/device (гео для
погоды, комната колонки для света)."""
import asyncio
import datetime

from aiohttp import web

from server_emulator.runtime import Runtime
from server_emulator.scenario_engine.conditions import check
from server_emulator.scenario_engine.reply_compose import compose_reply
from server_emulator.scenario_packages.validate import PackageError, validate_scenario
from server_emulator.schema import load_type


def _ctx(text, user=None, device=None):
    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg",
                     "sn": "SN-T", "text": text},
           "clock": {"now": datetime.datetime.now()}}
    if user is not None:
        ctx["user"] = user
    if device is not None:
        ctx["device"] = device
    return ctx


async def _say(runtime, phrase, ctx=None):
    sent = []

    async def send(data):
        sent.append(data)
    report = await runtime.dialog.handle_text(phrase, ctx or _ctx(phrase), "dg", send)
    return report, sent


def _reply_scenario(sid, phrases, match="contains", guard=None, reply=None):
    """Возвращает v2-сценарий для тестов пользовательских сценариев."""
    scenario = {
        "format_version": 2,
        "id": sid,
        "version": "1.0.0",
        "category": "custom",
        "triggers": [{"kind": "phrase", "phrases": phrases, "match": match}],
        "flow": [
            {"id": "say", "op": "compose", "reply": reply or {"text": "Готово."}},
            {"id": "done", "op": "finish"}
        ],
        "publication": {"enabled": False}
    }
    if guard is not None:
        scenario["guard"] = guard
    return scenario


# --- триггер-формы ------------------------------------------------------------


async def test_trigger_word_list_exact_and_mask(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings["builtin_scenarios"] = False  # только наши определения
    runtime.settings["scenarios"] = [
        _reply_scenario("user.exact", ["время"], match="exact"),
        _reply_scenario("user.mask", ["*погод*"], match="mask"),
        _reply_scenario("user.list", ["скажи время", "который час"], match="contains"),
    ]
    try:
        report, _ = await _say(runtime, "время")
        assert report["scenario"] == "user.exact" and report["reason"] == "phrase:exact"
        report, _ = await _say(runtime, "а какая погодка на улице")
        assert report["scenario"] == "user.mask" and report["reason"] == "phrase:mask"
        report, _ = await _say(runtime, "алиса скажи время")  # wake снят нормализатором
        assert report["scenario"] == "user.list"
        report, _ = await _say(runtime, "не время")  # exact не матчится, contains нет
        assert report["handled"] is False
    finally:
        await runtime.close()


def test_validator_trigger_forms():
    ok = _reply_scenario("user.ok", ["слово", "предложение из слов"], match="mask")
    assert validate_scenario(ok)["id"] == "user.ok"
    empty = _reply_scenario("user.bad", [])
    try:
        validate_scenario(empty)
        raise AssertionError("пустой список фраз должен отклоняться")
    except PackageError:
        pass


# --- guard: право отказа ------------------------------------------------------


async def test_guard_declines_to_next_scenario_or_fallback(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings["builtin_scenarios"] = False
    runtime.settings["scenarios"] = [
        _reply_scenario("user.geo", ["погода"], guard={"exists": [{"source": "user", "field": "location"}]},
                        reply={"text": "Погода по гео."}),
        _reply_scenario("user.fallback", ["погод"], reply={"text": "Погода без гео."}),
    ]
    try:
        # гео нет → первый сценарий отказался, реплика ушла следующему
        report, sent = await _say(runtime, "погода")
        assert report["scenario"] == "user.fallback"
        # гео есть → первый берёт запрос
        report, _ = await _say(runtime, "погода", _ctx("погода", user={"location": {"latitude": 1, "longitude": 2}}))
        assert report["scenario"] == "user.geo"
        # все отказались → fallback вызывающего (LLM)
        runtime.settings["scenarios"] = [_reply_scenario("user.only", ["глаза"],
                                                         guard={"exists": [{"source": "device", "field": "room"}]})]
        report, _ = await _say(runtime, "глаза")
        assert report["handled"] is False and report["reason"].startswith("guard_declined")
    finally:
        await runtime.close()


def test_conditions_check():
    ctx = _ctx("x", user={"location": {"latitude": 5}}, device={"room": "кухня"})
    assert check({"exists": [{"source": "user", "field": "location"}]}, ctx)
    assert not check({"exists": [{"source": "user", "field": "location"}]}, _ctx("x"))
    assert check({"eq": [{"source": "device", "field": "room"}, "кухня"]}, ctx)
    without_user = _ctx("x", device={"room": "кухня"})
    assert check({"all": [{"not": {"exists": [{"source": "user", "field": "location"}]}},
                          {"in": [{"source": "device", "field": "room"}, ["кухня", "зал"]]}]}, without_user)
    assert not check({"any": [{"eq": [{"source": "device", "field": "room"}, "спальня"]},
                              {"exists": [{"source": "user", "field": "нет"}]}]}, ctx)
    assert check({"bad": "leaf"}, ctx) is False  # битое условие = отказ, не исключение


# --- compose.reply: директивы/TTS-реплика из JSON ------------------------------


def test_compose_reply_builds_directives_and_speech():
    ctx = _ctx("глазки")
    ctx["slots"] = {"gif": "https://x/joy.gif"}
    spec = {"text": "А вот и глазки!",
            "speech_prefix": "<speaker voice=\"x\"> ",
            "card": True,
            "directives": [
                {"name": "draw_led_screen", "is_led_silent": True,
                 "payload": {"till_end_of_speech": False,
                             "animation_sequence": [{"frontal_led_image": {"source": "slot", "name": "gif"}}]}},
                {"name": "replay.one", "payload_raw": "AQID", "debug": True}]}
    message = compose_reply(spec, ctx, "server_emulator/schemas")
    response = message.Event.AliceResponse
    assert response.Response.Cards[0].Text == "А вот и глазки!"
    assert response.VoiceResponse.OutputSpeech.Text.startswith("<speaker voice=\"x\"> А вот")
    led, replay = response.Response.Directives
    assert led.Name == "draw_led_screen" and led.IsLedSilent is True
    assert led.Payload["animation_sequence"][0]["frontal_led_image"] == "https://x/joy.gif"
    assert replay.PayloadRaw == b"\x01\x02\x03"


def test_compose_reply_speech_only_without_card():
    message = compose_reply({"speech": "Только голос.", "card": False}, _ctx("x"), "server_emulator/schemas")
    response = message.Event.AliceResponse
    assert not response.Response.Cards and response.VoiceResponse.OutputSpeech.Text == "Только голос."


async def test_user_scenario_with_json_reply_via_dialog(tmp_path):
    """Пользовательский JSON-сценарий с директивой LED — без единой строки кода."""
    runtime = Runtime(tmp_path)
    runtime.settings["builtin_scenarios"] = False
    runtime.settings["scenarios"] = [
        {
            "format_version": 2,
            "id": "user.eyes",
            "version": "1.0.0",
            "category": "custom",
            "triggers": [{"kind": "phrase", "phrases": ["глазки", "покажи глазки"]}],
            "flow": [
                {"id": "say", "op": "compose", "reply": {
                    "text": "Глазки!",
                    "directives": [{"name": "draw_led_screen", "is_led_silent": True,
                                    "payload": {"till_end_of_speech": False,
                                                "animation_sequence": [{"frontal_led_image":
                                                                       {"source": "constant", "value": "https://s/joy.gif"}}]}}]}},
                {"id": "done", "op": "finish"}
            ],
            "publication": {"enabled": False}
        }
    ]
    try:
        report, sent = await _say(runtime, "покажи глазки")
        assert report["handled"] and report["scenario"] == "user.eyes"
        cls = load_type("server_emulator/schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
        message = cls.FromString(sent[0][4:])
        directive = message.Event.AliceResponse.Response.Directives[0]
        assert directive.Name == "draw_led_screen"
        assert directive.Payload["animation_sequence"][0]["frontal_led_image"] == "https://s/joy.gif"
    finally:
        await runtime.close()


# --- контекст: гео пользователя и комната колонки ------------------------------


async def test_weather_uses_user_location(tmp_path):
    """Гео профиля приоритетнее настроек: провайдер видит координаты пользователя.

    v2: weather читает датасет из БД. Создаём пользователя/устройство,
    задаём geo через quasar_params, сидируем датасет, подменяем плагин на
    ConnectionError, и проверяем ответ из заранее заготовленного FORECAST.
    """
    LAT, LON = 55.755826, 37.617326
    KEY = f"{LAT},{LON}"
    FORECAST = {
        "current": {"temperature_2m": 12.4, "apparent_temperature": 10.1, "weather_code": 3, "is_day": 1},
        "hourly": {"time": [f"2026-10-27T{h % 24:02d}:00" for h in range(12, 36)],
                   "temperature_2m": [10 + h % 5 for h in range(24)], "weather_code": [3] * 24, "is_day": [1] * 24},
        "daily": {"temperature_2m_max": [14.2], "temperature_2m_min": [8.1], "precipitation_probability_max": [10]},
    }

    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-W', 'user_id': 'owner', 'device_ids': []})
    runtime.quasar_params.set_value("user:owner", "user.location.latitude", LAT)
    runtime.quasar_params.set_value("user:owner", "user.location.longitude", LON)
    runtime.store.scenario_data_set("dataset:weather", "system", "", KEY, FORECAST)

    # подменяем плагин погоды — он должен упасть с ConnectionError
    async def failing_plugin(key, args):
        raise ConnectionError("провайдер недоступен")
    runtime.scenario_plugins["weather"] = failing_plugin

    try:
        report, sent = await _say(runtime, "погода",
                                  ctx={"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg",
                                                 "sn": "SN-W", "text": "погода"},
                                       "clock": {"now": datetime.datetime.now()}})
        # контекст рантайма (user/device/params/store) должен быть в ctx
        ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg",
                         "sn": "SN-W", "text": "погода"},
               "clock": {"now": datetime.datetime.now()}}
        ctx.update(runtime._scenario_context(sn="SN-W"))
        report, sent = await _say(runtime, "погода", ctx)

        assert report["scenario"] == "informers.weather"
        response = None
        for raw in sent:
            message = load_type("server_emulator/schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage").FromString(raw[4:])
            if message.Event.WhichOneof("Event") == "AliceResponse":
                response = message.Event.AliceResponse
                break
        assert response is not None
        assert response.Response.Cards[0].Text.startswith("Сейчас +12.")
    finally:
        await runtime.close()


async def test_light_defaults_to_device_room(tmp_path):
    """«включи свет» без комнаты: лампа комнаты колонки (device.room).

    Стиль tests/test_light.py: создаём устройство с light-комнатами,
    указываем device.room, проверяем реплику с этой комнатой.
    """
    runtime = Runtime(tmp_path)
    runtime.settings.update(builtin_scenarios=True)

    # создаём пользователя и устройство с лампами (как в test_light.py)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-T', 'user_id': 'owner',
                               'device_ids': ['dev-t'], 'profile': {'aapi_model': True}})
    device = next(d for d in runtime.identities.data['devices'] if d['sn'] == 'SN-T')
    runtime.identities.mutate({'operation': 'state', 'sn': 'SN-T',
                               'state': dict(device['state'],
                                             light={'кухня': {'on': False, 'brightness': 100},
                                                    'зал': {'on': False, 'brightness': 100}})})
    runtime.quasar_params.set_value("device:SN-T", "device.room", "кухня")  # комната колонки (params)

    try:
        ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg",
                         "sn": "SN-T", "text": "включи свет"},
               "clock": {"now": datetime.datetime.now()}}
        ctx.update(runtime._scenario_context(sn="SN-T"))
        report, sent = await _say(runtime, "включи свет", ctx)

        assert report["scenario"] == "smart_home.light"
        # проверяем реплику
        response = None
        for raw in sent:
            message = load_type("server_emulator/schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage").FromString(raw[4:])
            if message.Event.WhichOneof("Event") == "AliceResponse":
                response = message.Event.AliceResponse
                break
        assert response is not None
        text = response.Response.Cards[0].Text
        assert "кухня" in text and "включён" in text
    finally:
        await runtime.close()


async def test_identity_user_location_validation(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U',
                                   'location': {'latitude': 55.75, 'longitude': 37.61, 'city': 'Москва'}})
        user = next(u for u in runtime.identities.data['users'] if u['id'] == 'u1')
        assert user['location']['city'] == 'Москва'
        try:
            runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U',
                                       'location': {'latitude': 999}})
            raise AssertionError('невалидная локация должна отклоняться')
        except ValueError:
            pass
        runtime.identities.mutate({'operation': 'device', 'sn': 'S1', 'name': 'Колонка', 'room': 'Спальня'})
        device = next(d for d in runtime.identities.data['devices'] if d['sn'] == 'S1')
        assert device['room'] == 'Спальня'
    finally:
        await runtime.close()