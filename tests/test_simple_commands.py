"""Раздел «Устройство» (scenarios/device/*.json): громкость, Bluetooth, приложения —
фраза → JSON-сценарий → Push-действие, без обращения к модели."""
import datetime

import pytest
from google.protobuf.json_format import MessageToDict

from server_emulator.runtime import Runtime
from server_emulator.schema import load_type
from server_emulator.server import create_app
from tests.conftest import _open_session, listen


def _ctx(runtime, text, sn=""):
    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg", "sn": sn, "text": text},
           "clock": {"now": datetime.datetime.now()}}
    ctx.update(runtime._scenario_context(sn=sn))
    return ctx


def _card(sent):
    cls = load_type("server_emulator/schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
    for raw in sent:
        message = cls.FromString(raw[4:])
        if message.Event.WhichOneof("Event") == "AliceResponse":
            return message.Event.AliceResponse.Response.Cards[0].Text
    raise AssertionError("нет AliceResponse")


def _alice(sent):
    """Первый Event.AliceResponse из отправленного (wire TServerMessage, 4-байтный заголовок)."""
    cls = load_type("server_emulator/schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
    for raw in sent:
        message = cls.FromString(raw[4:])
        if message.Event.WhichOneof("Event") == "AliceResponse":
            return message.Event.AliceResponse
    raise AssertionError("нет AliceResponse")


async def _say(runtime, text, sn=""):
    sent, pushed = [], []

    async def send(data):
        sent.append(data)

    async def push_action(target, action, arguments):
        pushed.append((target, action, arguments))
        return {"action": action, "delivery": "no_session"}

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.push_action, runtime.chat = push_action, chat
    report = await runtime.dialog.handle_text(text, _ctx(runtime, text, sn), "dg", send)
    return report, sent, pushed


@pytest.mark.parametrize("phrase,action,arguments,reply", [
    # TASK-083: ответных образцов этих фраз в журнале нет — прежний проводной путь
    # (wire-Push sound_louder/sound_quiter/sound_set_level из приложения, TASK-025), БЛОКЕР
    ("громкость громче", "sound_louder", {}, "Громче."),
    ("тише", "sound_quiter", {}, "Тише."),
    ("максимальная громкость", "sound_set_level", {"level": 10}, "Максимальная громкость."),
    ("выключи звук", "sound_set_level", {"level": 0}, "Звук выключен."),
])
async def test_volume_step_push(tmp_path, phrase, action, arguments, reply):
    runtime = Runtime(tmp_path)
    try:
        report, sent, pushed = await _say(runtime, phrase, sn="SN-V")
        assert report["handled"] and report["scenario"] == "device.volume"
        assert [(p[0], p[1]) for p in pushed] == [("SN-V", action)]
        assert {k: str(v) for k, v in (pushed[0][2] or {}).items()} == {k: str(v) for k, v in arguments.items()}
        assert _card(sent) == reply
    finally:
        await runtime.close()


@pytest.mark.parametrize("phrase,level,percent", [
    # тихий ответ директивой настоящего сервера (журнал «громкость два»: new_level +
    # new_percent_level = level·100/MaxLevel); шкала без состояния запроса — по умолчанию 0..10
    ("громкость два", 2.0, 20.0),
    ("громкость пять", 5.0, 50.0),
    ("громкость на 5", 5.0, 50.0),
    ("громкость на семь", 7.0, 70.0),
    ("громкость на 50", 5.0, 50.0),  # проценты → шкала колонки (0..10 без состояния)
])
async def test_volume_set_is_silent_directive(tmp_path, phrase, level, percent):
    runtime = Runtime(tmp_path)
    try:
        report, sent, pushed = await _say(runtime, phrase, sn="SN-V")
        assert report["handled"] and report["scenario"] == "device.volume"
        assert pushed == []  # level-фразы — ответ директивой в том же AliceResponse, не Push
        alice = _alice(sent)
        names = [d.Name for d in alice.Response.Directives]
        assert names == ["sound_set_level", "tts_play_placeholder"]  # порядок образца
        directive = alice.Response.Directives[0]
        assert directive.Type == "client_action" and directive.IsLedSilent
        assert MessageToDict(directive.Payload) == {"new_level": level,
                                                    "new_percent_level": percent}
        assert not [c.Text for c in alice.Response.Cards]  # тихий ответ: речи и карточек нет
        speech = alice.VoiceResponse.OutputSpeech.Text if alice.HasField("VoiceResponse") else ""
        assert speech == ""
    finally:
        await runtime.close()


@pytest.mark.parametrize("phrase,scenario,action,reply", [
    ("включи блютуз", "device.bluetooth.on", "start_bluetooth", "Включаю."),
    ("выключи блютуз", "device.bluetooth.off", "stop_bluetooth", "Выключаю."),
])
async def test_bluetooth(tmp_path, phrase, scenario, action, reply):
    runtime = Runtime(tmp_path)
    try:
        report, sent, pushed = await _say(runtime, phrase, sn="SN-B")
        assert report["scenario"] == scenario and pushed[0][1] == action and _card(sent) == reply
    finally:
        await runtime.close()


async def test_app_open_found_and_not_found(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-A', 'user_id': 'owner', 'device_ids': []})
    device = next(d for d in runtime.identities.data['devices'] if d['sn'] == 'SN-A')
    runtime.identities.mutate({'operation': 'state', 'sn': 'SN-A', 'state': dict(
        device['state'], apps=[{"name": "YouTube", "package": "com.google.android.youtube"}])})
    try:
        report, sent, _ = await _say(runtime, "открой youtube", sn="SN-A")
        assert report["scenario"] == "device.app.open" and "«YouTube» найдено" in _card(sent)
        report, sent, _ = await _say(runtime, "запусти спотифай", sn="SN-A")
        assert _card(sent) == "Приложение «спотифай» не найдено."
    finally:
        await runtime.close()


async def test_enable_word_is_not_app_open(tmp_path):
    """«включи сказки» больше не перехватывается app.open (триггер «включи» убран)."""
    runtime = Runtime(tmp_path)
    try:
        report, _, _ = await _say(runtime, "открой сказки")
        assert report["scenario"] == "device.app.open"
        async def send(data):  # «включи сказки» теперь сценарий медиа (TASK-071) — доставка идёт
            pass
        report = await runtime.dialog.handle_text("включи сказки", _ctx(runtime, "включи сказки"), "dg", send)
        assert report.get("scenario") != "device.app.open"
    finally:
        await runtime.close()


async def test_volume_phrase_over_ws_without_model(tmp_path):
    """TASK-083: level-фраза по WS — тихий ответ директивой образца, без речи и карточек."""
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    runner, url = await listen(create_app(runtime=runtime))
    try:
        reply, _, ws = await _open_session(runtime, url, "громкость на 5")
        alice = reply.Event.AliceResponse
        assert [d.Name for d in alice.Response.Directives] == ["sound_set_level",
                                                               "tts_play_placeholder"]
        payload = MessageToDict(alice.Response.Directives[0].Payload)
        assert payload == {"new_level": 5.0, "new_percent_level": 50.0}
        assert not [c.Text for c in alice.Response.Cards]
        await ws.close()
    finally:
        await runner.cleanup()
        await runtime.close()
