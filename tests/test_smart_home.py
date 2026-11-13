"""Реестр умного дома: сервис ядра, SDK расширения, действие с колонки.

Проверяется реестр (префикс origin, права, сохранение, комнаты, производный online),
построители директив колонке (set_iot_user_info / set_div_variables /
sync_iot_user_info / set_iot_account_settings — поля по образцам журнала), путь
«кнопка карточки → плагин → четыре Push по порядку» (TextInput с фреймом
iot_device_action_semantic_frame, запись 2026-11-12) и пуш
списка живым сессиям при изменении реестра.
"""
import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.protobuf import json_format

from server_emulator import aapi_handlers, sdk
from server_emulator import smart_home
from server_emulator.runtime import Runtime
from server_emulator.schema import load_type, schemas_dir, station_file
from server_emulator.sdk import SDK_VERSION
from server_emulator.voice import Conversation

CLIENT = "NAlice.NAliceApi.TClientMessage"
SERVER_TYPE = "NAlice.NAliceApi.TServerMessage"
SN = "1928374650"


def _type(name):
    return load_type(schemas_dir(), "alice-api.pb", name)


def _centaur(name):
    return load_type(schemas_dir(), station_file(), name)


SERVER = _type(SERVER_TYPE)


# --- реестр (ядро, без расширений) -----------------------------------------------------------

def _standalone_registry(tmp_path, loaded=None):
    """Реестр поверх хоста без WS: directory — папка состояния, extensions — менеджер
    (loaded=None — расширения не подключены, origin недоступен)."""
    host = SimpleNamespace(directory=tmp_path,
                          extensions=None if loaded is None else SimpleNamespace(loaded=loaded))
    return smart_home.SmartHomeRegistry(host)


def test_add_prefix_update_remove_and_ownership(tmp_path):
    reg = _standalone_registry(tmp_path, loaded={"plug": {}})
    item = reg.add_device({"id": "light-1", "name": "Свет", "room": "room-1",
                           "type": "LightDeviceType",
                           "capabilities": [{"type": "on_off", "value": False}]}, "plug")
    assert item["id"] == "plug:light-1" and item["origin"] == "plug" and item["online"]

    with pytest.raises(smart_home.SmartHomeError):
        reg.add_device({"id": "plug:light-1", "name": "Лампа"}, "other")  # чужой префикс
    with pytest.raises(smart_home.SmartHomeError):
        reg.add_device({"name": "без id"}, "plug")
    with pytest.raises(smart_home.SmartHomeError):
        reg.update_device("plug:light-1", {"online": False}, "other")  # чужое
    with pytest.raises(smart_home.SmartHomeError):
        reg.remove_device("plug:light-1", "other")
    with pytest.raises(smart_home.SmartHomeError):
        reg.update_device("plug:light-1", {"id": "x"}, "plug")  # id не меняют

    upd = reg.update_device("light-1", {"name": "Ночник", "online": False,
                                        "capabilities": [{"type": "on_off", "value": True}]}, "plug")
    assert upd["name"] == "Ночник" and upd["online"] is False
    assert upd["capabilities"][0]["value"] is True

    assert reg.remove_device("plug:light-1", "plug") is None
    assert reg.devices() == [] and reg.get("plug:light-1") is None


def test_persistence_across_registries(tmp_path):
    first = _standalone_registry(tmp_path, loaded={"plug": {}})
    first.add_device({"id": "d-1", "name": "Лампа", "room": "room-1"}, "plug")
    first.set_rooms({"room-1": "Кухня"}, "plug")

    second = _standalone_registry(tmp_path, loaded={"plug": {}})
    assert [d["id"] for d in second.devices()] == ["plug:d-1"]
    assert second.get("plug:d-1")["name"] == "Лампа"
    assert second.rooms() == {"room-1": "Кухня"}


def test_rooms_replaced_only_own_origin(tmp_path):
    reg = _standalone_registry(tmp_path, loaded={"a": {}, "b": {}})
    reg.set_rooms({"r1": "Кухня"}, "a")
    reg.set_rooms({"r2": "Спальня"}, "b")
    reg.set_rooms({"r3": "Ванная"}, "a")
    assert reg.rooms() == {"r2": "Спальня", "r3": "Ванная"}


def test_online_follows_extension_enabled(tmp_path):
    off = _standalone_registry(tmp_path, loaded={})
    off.add_device({"id": "d-1", "name": "Лампа"}, "plug")
    assert off.devices()[0]["online"] is False  # флаг True, но плагин выключен

    on = _standalone_registry(tmp_path, loaded={"plug": {}})
    assert on.devices()[0]["online"] is True
    on.update_device("plug:d-1", {"online": False}, "plug")
    assert on.devices()[0]["online"] is False  # флаг False — и при включённом плагине


def test_directives_per_sample_fields(tmp_path):
    reg = _standalone_registry(tmp_path, loaded={"plug": {}})
    reg.add_device({"id": "l-1", "name": "Свет", "room": "room-1", "type": "LightDeviceType",
                    "capabilities": [{"type": "on_off", "value": False},
                                     {"type": "level", "value": 40, "instance": "brightness"}]}, "plug")
    reg.add_device({"id": "x-1", "name": "Неизвестное", "type": "light",
                    "capabilities": [{"type": "temperature", "value": 21}], "online": False}, "plug")
    reg.set_rooms({"room-1": "Кухня"}, "plug")

    user_info = _centaur("NAlice.TLocalIoTCapability.TSetIoTUserInfoDirective")
    msg = user_info.FromString(smart_home.user_info_raw(reg))
    assert msg.Name == "set_iot_user_info"
    rooms = msg.IoTClientUserInfo.Rooms
    assert [(r.Id, r.Name) for r in rooms] == [("room-1", "Кухня")]
    assert rooms[0].HouseholdId == "emulator-home"  # комната в доме, как в образце
    assert [h.Name for h in msg.IoTClientUserInfo.Households] == ["Дом"]
    devices = {d.Id: d for d in msg.IoTClientUserInfo.Devices}
    assert devices["plug:l-1"].Type == 1  # LightDeviceType (enum схемы)
    # умения — RawCapability (типизированные сообщения, как в образце), не голый enum
    on_off, level = devices["plug:l-1"].Endpoint.AnyCapabilities
    assert len(bytes(on_off.RawCapability)) > 0 and len(bytes(level.RawCapability)) > 0
    assert on_off.CapabilityType == 0 and level.CapabilityType == 0  # enum не задан
    caps = _centaur("NAlice.TOnOffCapability")
    on_off_msg = caps.FromString(bytes(on_off.RawCapability))
    assert on_off_msg.State.On is False and on_off_msg.Meta.Retrievable
    assert list(on_off_msg.Meta.SupportedDirectives) == [1]  # OnOffDirectiveType
    levels = _centaur("NAlice.TLevelCapability")
    level_msg = levels.FromString(bytes(level.RawCapability))
    assert level_msg.State.Level == 40.0
    assert level_msg.Parameters.Instance == 4  # BrightnessInstance (короткое имя «brightness»)
    assert level_msg.Parameters.Unit == 1 and level_msg.Parameters.Range.Max == 100  # PercentUnit 1..100
    assert devices["plug:l-1"].StatusInfo.Status == 1  # OnlineDeviceState
    assert devices["plug:x-1"].Type == 0  # значение вне enum → UnknownDeviceType
    assert devices["plug:x-1"].StatusInfo.Status == 2  # OfflineDeviceState
    unknown_cap = devices["plug:x-1"].Endpoint.AnyCapabilities[0]
    assert unknown_cap.CapabilityType == 0  # неизвестный тип умения — CapabilityType без Raw

    div = _centaur("NAlice.TDivViewCapability.TSetDivVariablesDirective")
    variables = div.FromString(smart_home.div_variables_raw(
        {"plug:l-1": {"on_off": True, "level": False}}))
    assert [(v.VariableName, v.LongValue) for v in variables.SetVariable] == [
        ("device_plug_l_1_on_off_state", 1), ("device_plug_l_1_level_state", 0)]

    sync = _centaur("NAlice.TLocalIoTCapability.TSyncIoTUserInfoDirective")
    assert sync.FromString(smart_home.sync_iot_user_info_raw()).Name == "sync_iot_user_info"

    # set_iot_account_settings — Name + пустое TIoTAccountSettings, байт в байт как в записи
    account = _centaur("NAlice.TLocalIoTCapability.TSetIoTAccountSettingsDirective")
    raw = smart_home.account_settings_raw()
    assert raw == base64.b64decode("ChhzZXRfaW90X2FjY291bnRfc2V0dGluZ3MSAA==")
    msg = account.FromString(raw)
    assert msg.Name == "set_iot_account_settings" and msg.HasField("IoTAccountSettings")
    assert not msg.IoTAccountSettings.ListFields()


# --- расширение через SDK + действие с колонки ------------------------------------------------

MANIFEST = {
    "id": "lights", "name": "Lights", "version": "1.0.0",
    "description": "Устройства умного дома из расширения.",
    "author": "Ivan <ivan@example.com>", "license": "MIT",
    "requires": {"emulator": ">=0", "sdk": f">={SDK_VERSION} <3.0.0"},
    "permissions": {},
}

EXT_MODULE = """
import json
from pathlib import Path

from server_emulator import sdk

CALLS = Path(r"%(calls)s")

sdk.smart_home_set_rooms({"room-1": "Кухня"})
sdk.smart_home_add_device({"id": "light-1", "name": "Свет", "room": "room-1",
                           "type": "LightDeviceType",
                           "capabilities": [{"type": "on_off", "value": False}]})


async def device_action(device, action):
    value = (action.get("instance") or "on") != "off"
    with CALLS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": device["id"], "action": action}) + "\\n")
    return {"on_off": value}


sdk.smart_home_on_device_action(device_action)
"""


async def make_ext_runtime(tmp_path):
    """Runtime с включённым расширением «lights» (устройства и комнаты — на импорте)."""
    calls = tmp_path / "calls.jsonl"
    root = tmp_path / "lights-ext"
    (root / "aapi").mkdir(parents=True)
    (root / "extension.json").write_text(json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8")
    (root / "aapi" / "smart.py").write_text(EXT_MODULE % {"calls": str(calls)}, encoding="utf-8")
    runtime = Runtime(tmp_path)
    sdk.bind_runtime(runtime)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'Alex'})
    runtime.identities.mutate({'operation': 'device', 'sn': SN, 'user_id': 'owner', 'device_ids': []})
    await runtime.extensions.install({"url": str(root)}, confirm=True)
    await runtime.extensions.enable("lights")
    return runtime, calls


def iot_incoming(device_ids, instance):
    """TextInput с фреймом действия карточки (образец записей 120389–120425)."""
    client = _type(CLIENT)()
    client.Event.Header.MessageId = "msg-1"
    client.Event.TextInput.Header.RequestId = "req-7"
    event = client.Event.TextInput.Request.Event
    event.Name = "@@mm_semantic_frame"
    json_format.ParseDict({"typed_semantic_frame": {"iot_device_action_semantic_frame": {
        "request_value": {"device_ids": device_ids},
        "intent_parameters": {"capability_instance": instance}}}}, event.Payload)
    return client


async def dispatch_iot(runtime, incoming):
    """Обработчик TextInput фичи умного дома (тот же путь, что voice.receive)."""
    sent = []

    async def send(data):
        sent.append(data)

    conversation = Conversation(runtime, SERVER, send,
                                trace_context={"session": "s-1", "sn": SN, "device_id": ""})
    handled = False
    for handler in aapi_handlers.handlers("TextInput", runtime.settings):
        if await handler(conversation, incoming, "TextInput"):
            handled = True
            break
    return handled, sent


def push_directive(sent, index):
    return SERVER.FromString(sent[index][4:]).Event.Push.Directives[0]


async def test_extension_devices_action_updates_state_and_pushes(tmp_path):
    runtime, calls = await make_ext_runtime(tmp_path)
    try:
        devices = runtime.smart_home.devices()
        assert [d["id"] for d in devices] == ["lights:light-1"]
        assert devices[0]["online"] is True and devices[0]["capabilities"][0]["value"] is False
        assert runtime.smart_home.rooms() == {"room-1": "Кухня"}
        assert runtime.smart_home.has_actions()

        handled, sent = await dispatch_iot(runtime, iot_incoming(["lights:light-1"], "on"))
        assert handled and len(sent) == 6  # ack + ответ + четыре пуша по порядку образца
        ack = SERVER.FromString(sent[0][4:])
        assert ack.Event.HasField("InputStartAck")
        response = SERVER.FromString(sent[1][4:]).Event.AliceResponse
        assert response.Header.RequestId == "req-7"
        assert response.VoiceResponse.OutputSpeech.Text == ""
        assert len(response.Response.Directives) == 1  # ответ — одна tts_play_placeholder
        directive = response.Response.Directives[0]
        assert directive.Name == "tts_play_placeholder" and directive.IsLedSilent
        assert [push_directive(sent, i).Name for i in range(2, 6)] == [
            "set_div_variables", "sync_iot_user_info",
            "set_iot_account_settings", "set_iot_user_info"]
        div = push_directive(sent, 2)
        assert div.Name == "set_div_variables" and div.EndpointId.value == SN
        variables = _centaur("NAlice.TDivViewCapability.TSetDivVariablesDirective")
        assert [v.VariableName for v in variables.FromString(bytes(div.PayloadRaw)).SetVariable] == \
            ["device_lights_light_1_on_off_state"]
        sync = push_directive(sent, 3)
        assert sync.Name == "sync_iot_user_info"
        account = push_directive(sent, 4)
        settings = _centaur("NAlice.TLocalIoTCapability.TSetIoTAccountSettingsDirective") \
            .FromString(bytes(account.PayloadRaw))
        assert settings.Name == "set_iot_account_settings" and settings.HasField("IoTAccountSettings")
        assert list(settings.IoTAccountSettings.ListFields()) == []  # настроек нет, как в образце
        user_info = _centaur("NAlice.TLocalIoTCapability.TSetIoTUserInfoDirective") \
            .FromString(bytes(push_directive(sent, 5).PayloadRaw))
        assert [d.Id for d in user_info.IoTClientUserInfo.Devices] == ["lights:light-1"]
        assert user_info.IoTClientUserInfo.Devices[0].StatusInfo.Status == 1  # OnlineDeviceState

        assert runtime.smart_home.devices()[0]["capabilities"][0]["value"] is True
        entry = json.loads(calls.read_text(encoding="utf-8").splitlines()[0])
        assert entry["id"] == "lights:light-1"
        assert entry["action"] == {"instance": "on", "capability": "on_off", "value": True}

        await dispatch_iot(runtime, iot_incoming(["lights:light-1"], "off"))
        assert runtime.smart_home.devices()[0]["capabilities"][0]["value"] is False
    finally:
        await runtime.close()
        aapi_handlers.drop_builtin(smart_home.IOT_HANDLER_NAME)


async def test_action_foreign_frame_returns_false(tmp_path):
    runtime, _calls = await make_ext_runtime(tmp_path)
    try:
        foreign = _type(CLIENT)()
        foreign.Event.Header.MessageId = "msg-2"
        foreign.Event.TextInput.Header.RequestId = "req-2"
        event = foreign.Event.TextInput.Request.Event
        event.Name = "@@mm_semantic_frame"
        json_format.ParseDict({"typed_semantic_frame": {"other_semantic_frame": {}}}, event.Payload)
        handled, sent = await dispatch_iot(runtime, foreign)
        assert not handled and sent == []
    finally:
        await runtime.close()
        aapi_handlers.drop_builtin(smart_home.IOT_HANDLER_NAME)


async def test_action_unknown_device_still_answers_sync(tmp_path):
    runtime, calls = await make_ext_runtime(tmp_path)
    try:
        handled, sent = await dispatch_iot(runtime, iot_incoming(["lights:ghost"], "on"))
        assert handled and len(sent) == 3  # ack + ответ + sync, div-пуша нет
        assert push_directive(sent, 2).Name == "sync_iot_user_info"
        assert not calls.exists()
        assert runtime.smart_home.devices()[0]["capabilities"][0]["value"] is False
    finally:
        await runtime.close()
        aapi_handlers.drop_builtin(smart_home.IOT_HANDLER_NAME)


async def test_disable_marks_devices_offline(tmp_path):
    runtime, _calls = await make_ext_runtime(tmp_path)
    try:
        await runtime.extensions.disable("lights")
        assert runtime.smart_home.devices()[0]["online"] is False  # реестр хранит, сеть — нет
        assert runtime.smart_home.rooms() == {"room-1": "Кухня"}
    finally:
        await runtime.close()
        aapi_handlers.drop_builtin(smart_home.IOT_HANDLER_NAME)


# --- пуш списка колонкам ----------------------------------------------------------------------

def _session(runtime, send):
    runtime.sessions["conn-1"] = {"connection_id": "conn-1", "device_sn": SN,
                                  "user_id": "owner", "send": send}


async def test_registry_change_pushes_user_info_to_live_sessions(tmp_path):
    runtime = Runtime(tmp_path)
    sent = []

    async def send(data):
        sent.append(data)

    _session(runtime, send)
    try:
        runtime.smart_home.add_device({"id": "d-1", "name": "Лампа", "room": "room-1"}, "plug")
        runtime.smart_home.set_rooms({"room-1": "Кухня"}, "plug")
        await asyncio.sleep(0.05)
        assert sent, "изменение реестра — пуш живой сессии"
        directive = push_directive(sent, -1)
        assert directive.Name == "set_iot_user_info" and directive.EndpointId.value == SN
        user_info = _centaur("NAlice.TLocalIoTCapability.TSetIoTUserInfoDirective")
        msg = user_info.FromString(bytes(directive.PayloadRaw))
        assert [d.Id for d in msg.IoTClientUserInfo.Devices] == ["plug:d-1"]
        assert [(r.Id, r.Name) for r in msg.IoTClientUserInfo.Rooms] == [("room-1", "Кухня")]

        sent.clear()
        runtime.smart_home.remove_device("plug:d-1", "plug")
        await asyncio.sleep(0.05)
        directive = push_directive(sent, -1)  # пустой список уходит — карточки снимаются
        msg = _centaur("NAlice.TLocalIoTCapability.TSetIoTUserInfoDirective").FromString(
            bytes(directive.PayloadRaw))
        assert list(msg.IoTClientUserInfo.Devices) == []
    finally:
        await runtime.close()


async def test_action_pushes_user_info_once_to_live_session(tmp_path):
    """Действие карточки: список уходит колонке один раз и в порядке образца —
    слушатель реестра при действии молчит, иначе дубль опережал бы остальные пуши."""
    runtime, _calls = await make_ext_runtime(tmp_path)
    pushed = []

    async def send(data):
        pushed.append(data)

    _session(runtime, send)
    try:
        handled, sent = await dispatch_iot(runtime, iot_incoming(["lights:light-1"], "on"))
        await asyncio.sleep(0.05)
        assert handled
        assert [push_directive(sent, i).Name for i in range(2, 6)] == [
            "set_div_variables", "sync_iot_user_info",
            "set_iot_account_settings", "set_iot_user_info"]
        assert not pushed, "слушатель реестра не дублирует список при действии"
    finally:
        await runtime.close()
        aapi_handlers.drop_builtin(smart_home.IOT_HANDLER_NAME)


async def test_empty_registry_not_pushed_to_identified_column(tmp_path):
    runtime = Runtime(tmp_path)
    sent = []

    async def send(data):
        sent.append(data)

    _session(runtime, send)
    try:
        runtime._smart_home_push(SN)  # новой колонке показывать нечего
        assert not sent
        runtime.smart_home.add_device({"id": "d-1", "name": "Лампа"}, "plug")
        await asyncio.sleep(0.05)
        sent.clear()
        runtime._smart_home_push(SN)  # после опознания — текущий список
        await asyncio.sleep(0.05)  # пуш уходит задачей жизненного цикла
        assert push_directive(sent, 0).Name == "set_iot_user_info"
    finally:
        await runtime.close()


async def test_feature_inactive_without_extension(tmp_path):
    """Пока обработчиков действий нет — TextInput фичей не перехватывается
    (инвариант флагов профиля сохранён). Встроенный HA-плагин регистрирует
    обработчик всегда — инвариант проверяется на чистом реестре без колбэков
    (подменой store, как у модульных тестов ядра)."""
    runtime = Runtime(tmp_path)
    from server_emulator.smart_home import SmartHomeRegistry
    runtime.smart_home = SmartHomeRegistry(runtime)  # без колбэков плагинов
    sent = []

    async def send(data):
        sent.append(data)

    try:
        assert not runtime.smart_home.has_actions()
        conversation = Conversation(runtime, SERVER, send,
                                    trace_context={"session": "s-1", "sn": SN})
        assert not await smart_home._on_iot_device_action(
            conversation, iot_incoming(["plug:d-1"], "on"), "TextInput")
        assert not sent
    finally:
        await runtime.close()
