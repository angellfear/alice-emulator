"""Блок умного дома в шторке: карточка set_upper_shutter по реестру и ответ на колбэк
collect_upper_shutter (форма настоящего ответа: InputStartAck + AliceResponse с одной
директивой, div2_card с card_id upper_shutter_key_static_key)."""
import json
import re
import urllib.parse
from types import SimpleNamespace

from google.protobuf import json_format

from server_emulator import smart_home, upper_shutter
from server_emulator.schema import load_type, schemas_dir, station_file
from server_emulator.voice import Conversation

SERVER = load_type(schemas_dir(), "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
CLIENT = load_type(schemas_dir(), "alice-api.pb", "NAlice.NAliceApi.TClientMessage")
SN = "1928374650"


def _registry(tmp_path):
    registry = smart_home.SmartHomeRegistry(SimpleNamespace(directory=tmp_path, extensions=None))
    origin = "lights"
    registry.set_rooms({"r-hall": "Коридор", "r-kitchen": "Кухня"}, origin)
    add = lambda **kw: registry.add_device(kw, origin)  # noqa: E731
    add(id="light.kitchen", name="Свет кухня", room="r-kitchen", type="LightDeviceType",
        capabilities=[{"type": "on_off", "value": True}])
    add(id="light.hall", name="Свет коридор", room="r-hall", type="LightDeviceType",
        capabilities=[{"type": "on_off", "value": False}], online=False)
    add(id="fan.bath", name="Вытяжка", type="FanDeviceTypeV2",
        capabilities=[{"type": "on_off", "value": False}])
    add(id="sensor.t", name="Температура", room="r-kitchen", type="SensorDeviceType")
    add(id="switch.x", name="Реле", room="r-kitchen", type="SwitchDeviceType",
        capabilities=[{"type": "on_off", "value": True}])
    return registry


def _texts(node):
    return re.findall(r'"text":"([^"]*)"', json.dumps(node, ensure_ascii=False, separators=(",", ":")))


def test_card_from_registry(tmp_path):
    body = upper_shutter.card(_registry(tmp_path))
    assert "@@" not in body and "$dialog" not in body
    card = json.loads(body)
    root = card["card"]["states"][0]["div"]
    filters, devices = root["items"][0]["items"][:2]
    assert [_texts(chip) for chip in filters["items"]] == [["Свет"]]  # только присутствующие
    tiles = devices["items"]
    # группами по комнатам (по имени), без комнаты — в конце, затем «Показать все»
    assert [_texts(tile)[0] for tile in tiles] == ["Свет коридор", "Свет кухня", "Вытяжка",
                                                    "Показать все"]
    assert _texts(tiles[0])[:3] == ["Свет коридор", "Коридор", "Не в сети"]
    assert _texts(tiles[2])[1] == ""  # без комнаты — подписи комнаты нет
    assert tiles[0]["visibility"] == \
        "@{iot_filter_Light == 'on' || upper_shutter_all_filters_is_off ? 'visible' : 'gone'}"
    assert tiles[2]["visibility"] == \
        "@{false || upper_shutter_all_filters_is_off ? 'visible' : 'gone'}"
    variables = {v["name"]: v["value"] for v in card["card"]["variables"]}
    assert variables["device_lights_light_kitchen_on_off_state"] is True
    assert variables["device_lights_light_hall_on_off_state"] is False
    assert variables["iot_filter_Light"] == "off" and variables["upper_shutter_all_filters_is_off"]
    # действие плитки — фрейм с id реестра, ссылка закодирована как в образце
    urls = re.findall(r'"url":"(dialog://[^"]+)"', body)
    frames = [json.loads(urllib.parse.unquote(url.split("=", 1)[1])) for url in urls]
    ids = {tuple(d["payload"]["typed_semantic_frame"]["iot_device_action_semantic_frame"]
                 ["request"]["request_value"]["device_ids"])
           for frame in frames for d in frame if d["name"] == "@@mm_semantic_frame"}
    assert ids == {("lights:light.kitchen",), ("lights:fan.bath",)}
    assert "https://yastatic.net/s3/smart-display-res/iot/devices/lamp-other-on@2x.png" in body


def test_value_tiles(tmp_path):
    registry = smart_home.SmartHomeRegistry(SimpleNamespace(directory=tmp_path, extensions=None))
    origin = "ha"
    registry.set_rooms({"r-k": "Кухня"}, origin)
    add = lambda **kw: registry.add_device(kw, origin)  # noqa: E731
    add(id="sensor.v", name="Напряжение", room="r-k", type="SensorDeviceType",
        capabilities=[{"type": "level", "value": 231.1, "instance": "voltage"}])
    add(id="sensor.h", name="Влажность", room="r-k", type="ClimateSensorDeviceType",
        capabilities=[{"type": "level", "value": 26.0, "instance": "humidity"}], online=False)
    add(id="vacuum.r", name="Пылесос", room="r-k", type="VacuumCleanerDeviceType",
        capabilities=[{"type": "battery", "value": 9}], online=False)
    add(id="climate.p", name="Плита", room="r-k", type="ThermostatDeviceType",
        capabilities=[{"type": "on_off", "value": True},
                      {"type": "level", "value": 192, "instance": "temperature"}])
    add(id="sensor.time", name="Время", room="r-k", type="SensorDeviceType", capabilities=[])
    add(id="sensor.lux", name="Освещённость", room="r-k", type="SensorDeviceType",
        capabilities=[{"type": "level", "value": 5, "instance": "illuminance"}])  # без образца
    body = upper_shutter.card(registry)
    assert "@@" not in body
    card = json.loads(body)
    filters, devices = card["card"]["states"][0]["div"]["items"][0]["items"][:2]
    assert [_texts(chip) for chip in filters["items"]] == [["Датчики"], ["Климат"], ["Пылесосы"]]
    tiles = {_texts(tile)[0]: tile for tile in devices["items"]}
    assert list(tiles) == ["Влажность", "Напряжение", "Плита", "Пылесос", "Показать все"]
    assert "Не в сети" in _texts(tiles["Влажность"]) and "Не в сети" in _texts(tiles["Пылесос"])
    assert "Не в сети" not in _texts(tiles["Напряжение"])
    text = json.dumps(tiles["Напряжение"], ensure_ascii=False)
    assert "property_ha_sensor_v_voltage_devices.properties.float_value)*10.0)/10.0} В" in text
    assert "property-icons/voltage.png" in text and "devices/sensor@2x.png" in text
    assert "property-icons/battery-level.png" in json.dumps(tiles["Пылесос"])
    assert tiles["Плита"]["visibility"] ==         "@{iot_filter_Climate == 'on' || upper_shutter_all_filters_is_off ? 'visible' : 'gone'}"
    variables = {v["name"]: (v["type"], v["value"]) for v in card["card"]["variables"]}
    assert variables["property_ha_sensor_v_voltage_devices.properties.float_value"] ==         ("string", "231.1")
    assert variables["property_ha_sensor_h_humidity_devices.properties.float_value"] ==         ("string", "26")
    assert variables["property_ha_vacuum_r_battery_level_devices.properties.float_value"] ==         ("string", "9")
    assert variables["device_ha_climate_p_on_off_state"] == ("boolean", True)
    assert variables["device_ha_vacuum_r_on_off_state"] == ("boolean", False)
    assert "device_ha_sensor_v_on_off_state" not in variables  # у датчика вкл/выкл нет


def test_station_tiles(tmp_path):
    runtime = SimpleNamespace(
        identities=SimpleNamespace(data={"devices": [
            {"sn": SN, "user_id": "u1", "name": "Эта", "profile": {"platform": "chiron"}},
            {"sn": "C00TEST0000001", "user_id": "u1", "name": "Дуо", "room": "Зал",
             "profile": {"platform": "chiron"}},
            {"sn": "X00TEST0000002", "user_id": "u1", "name": "Макс",
             "profile": {"platform": "yandexstation_2"}},
            {"sn": "C00TEST0000003", "user_id": "u2", "name": "Чужая",
             "profile": {"platform": "chiron"}}]}),
        sessions={"a": {"device_sn": "C00TEST0000001", "send": object()},
                  "b": {"device_sn": SN, "send": object()}})
    found = smart_home.stations(runtime, SN)
    assert [(s["name"], s["online"]) for s in found] == [("Дуо", True), ("Макс", False)]
    empty = smart_home.SmartHomeRegistry(SimpleNamespace(directory=tmp_path, extensions=None))
    body = upper_shutter.card(empty, found)
    assert "@@" not in body
    card = json.loads(body)
    filters, devices = card["card"]["states"][0]["div"]["items"][0]["items"][:2]
    assert [_texts(chip) for chip in filters["items"]] == [["Колонки"]]
    tiles = devices["items"]
    assert [_texts(tile)[0] for tile in tiles] == ["Дуо", "Макс", "Показать все"]
    assert "Не в сети" not in _texts(tiles[0]) and "Не в сети" in _texts(tiles[1])
    assert "devices/station-Duo-max-black@2x.png" in json.dumps(tiles[0])
    urls = re.findall(r'"url":"(dialog://[^"]+)"', json.dumps(tiles[0], ensure_ascii=False,
                                                           separators=(",", ":")))
    directives = [d for url in urls
                  for d in json.loads(urllib.parse.unquote(url.split("=", 1)[1]))]
    call = next(d for d in directives if d["name"] == "phone_calls_open_device_call_ui")
    info = call["payload"]["DeviceInfo"]
    assert (info["DeviceId"], info["DeviceName"], info["RoomName"]) ==         ("C00TEST0000001", "Дуо", "Зал")
    page = next(d for d in directives if d["name"] == "open_smart_home_page_directive")
    assert page["payload"]["move_options"]["onto_id"] == smart_home.station_device_id(
        "C00TEST0000001")


def test_variable_names_match_set_div_variables():
    raw = smart_home.div_variables_raw({"lights:light.kitchen": {"on_off": True}})
    message = load_type(schemas_dir(), station_file(),
                        "NAlice.TDivViewCapability.TSetDivVariablesDirective").FromString(raw)
    assert [v.VariableName for v in message.SetVariable] == \
        ["device_lights_light_kitchen_on_off_state"]


def _callback(purpose="collect_upper_shutter"):
    client = CLIENT()
    client.Event.Header.MessageId = "msg-1"
    client.Event.TextInput.Header.RequestId = "req-9"
    event = client.Event.TextInput.Request.Event
    event.Type = "server_action"
    event.Name = "@@mm_typed_callback"
    json_format.ParseDict({"analytics": {"purpose": purpose, "origin": "ThisClient"},
                           "request_params": {"disable_output_speech": True}}, event.Payload)
    return client


async def _answer(registry, incoming):
    sent = []

    async def send(data):
        sent.append(data)

    runtime = SimpleNamespace(smart_home=registry, settings={})
    conversation = Conversation(runtime, SERVER, send, trace_context={"session": "s-1", "sn": SN})
    handled = await upper_shutter.on_text_input(conversation, incoming, "TextInput")
    return handled, [SERVER.FromString(data[4:]) for data in sent]


async def test_callback_answers_set_upper_shutter(tmp_path):
    registry = _registry(tmp_path)
    handled, sent = await _answer(registry, _callback())
    assert handled and len(sent) == 2
    assert sent[0].Event.HasField("InputStartAck")
    response = sent[1].Event.AliceResponse
    assert response.Header.RequestId == "req-9"
    (directive,) = response.Response.Directives
    assert directive.Type == "client_action" and directive.Name == "set_upper_shutter"
    assert directive.IsLedSilent
    card = json_format.MessageToDict(directive.Payload)["div2_card"]
    assert card["id"] == {"card_id": "upper_shutter_key_static_key", "card_name": "upper_shutter"}
    assert card["card_name"] == "CentaurUpperShutterData"
    assert card["string_body"] == upper_shutter.card(registry)


async def test_callback_other_purpose_or_empty_registry_passes(tmp_path):
    handled, sent = await _answer(_registry(tmp_path), _callback("collect_main_screen"))
    assert not handled and not sent
    empty = smart_home.SmartHomeRegistry(SimpleNamespace(directory=tmp_path / "e", extensions=None))
    handled, sent = await _answer(empty, _callback())
    assert not handled and not sent
