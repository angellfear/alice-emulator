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
