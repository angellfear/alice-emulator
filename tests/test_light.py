"""Раздел «Умный дом» (scenarios/smart_home/light.json): комната из словаря сценария,
по умолчанию — комната колонки или единственная лампа; честные отказы."""
import datetime

import pytest
from aiohttp import ClientSession  # noqa: F401  (WS-хелперы conftest)

from server_emulator.runtime import Runtime
from server_emulator.scenario_engine.light import apply_light, entities_of
from server_emulator.server import create_app
from tests.conftest import _open_session, listen


def device_with_light(identities, rooms, room=None):
    identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    identities.mutate({'operation': 'device', 'sn': 'SN-T', 'user_id': 'owner',
                       'device_ids': ['dev-t'], 'profile': {'aapi_model': True}})
    device = next(d for d in identities.data['devices'] if d['sn'] == 'SN-T')
    identities.mutate({'operation': 'state', 'sn': 'SN-T', 'state': dict(device['state'], light=rooms)})
    if room:
        device['room'] = room


def _lights(runtime):
    return next(d for d in runtime.identities.data['devices'] if d['sn'] == 'SN-T')['state']['light']


async def _say(runtime, text):
    replies = []

    async def send(data):
        from server_emulator.schema import load_type
        cls = load_type("server_emulator/schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
        message = cls.FromString(data[4:])
        if message.Event.WhichOneof("Event") == "AliceResponse":
            replies.append(message.Event.AliceResponse.Response.Cards[0].Text)

    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg", "sn": "SN-T", "text": text},
           "clock": {"now": datetime.datetime.now()}}
    ctx.update(runtime._scenario_context(sn="SN-T"))
    report = await runtime.dialog.handle_text(text, ctx, "dg", send)
    return report, replies[0] if replies else None


@pytest.mark.parametrize("phrase,room,on,brightness,reply", [
    ("включи свет на кухне", "кухня", True, 100, "Свет (кухня) включён на 100%."),
    ("выключи свет в спальне", "спальня", False, 100, "Свет (спальня) выключен."),
    ("включи свет на кухне на 50 процентов", "кухня", True, 50, "Свет (кухня) включён на 50%."),
])
async def test_light_rooms(tmp_path, phrase, room, on, brightness, reply):
    runtime = Runtime(tmp_path)
    device_with_light(runtime.identities, {'кухня': {'on': False, 'brightness': 100},
                                           'спальня': {'on': True, 'brightness': 100}})
    try:
        report, text = await _say(runtime, phrase)
        assert report["scenario"] == "smart_home.light" and text == reply
        assert _lights(runtime)[room] == {'on': on, 'brightness': brightness}
    finally:
        await runtime.close()


async def test_light_default_room(tmp_path):
    runtime = Runtime(tmp_path)
    device_with_light(runtime.identities, {'кухня': {'on': False, 'brightness': 100}})
    try:
        _, text = await _say(runtime, "включи свет")  # единственная лампа
        assert text == "Свет (кухня) включён на 100%."
    finally:
        await runtime.close()


async def test_light_ambiguous_and_unknown(tmp_path):
    runtime = Runtime(tmp_path)
    device_with_light(runtime.identities, {'кухня': {'on': False}, 'спальня': {'on': False}})
    try:
        _, text = await _say(runtime, "включи свет")
        assert text.startswith("Не понял, какую лампу")
        _, text = await _say(runtime, "включи свет в ванной")
        assert text.startswith("Лампы «ванная» нет")
    finally:
        await runtime.close()


def test_apply_light_mutates_device_state(tmp_path):
    from server_emulator.identity import IdentityRegistry
    from server_emulator.state_store import StateStore
    identities = IdentityRegistry(tmp_path, StateStore(tmp_path / "e.sqlite3"))
    identities.mutate({'operation': 'user', 'id': 'u', 'name': 'U'})
    identities.mutate({'operation': 'device', 'sn': 'SN-1', 'user_id': 'u', 'device_ids': ['d1'], 'profile': {}})
    device = next(d for d in identities.data['devices'] if d['sn'] == 'SN-1')
    identities.mutate({'operation': 'state', 'sn': 'SN-1',
                       'state': dict(device['state'], light={'кухня': {'on': False, 'brightness': 100}})})
    assert apply_light(identities, 'SN-1', 'кухня', 'on', 40) == {'on': True, 'brightness': 40}
    stored = entities_of(next(d for d in identities.data['devices'] if d['sn'] == 'SN-1'))
    assert stored['кухня'] == {'on': True, 'brightness': 40}
    with pytest.raises(ValueError):
        apply_light(identities, 'SN-1', 'баня', 'on')


async def test_light_scenario_on_off_over_ws(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)
    device_with_light(runtime.identities, {'кухня': {'on': False, 'brightness': 100}})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        first, _, ws = await _open_session(runtime, url, "включи свет на кухне")
        assert first.Event.AliceResponse.Response.Cards[0].Text == "Свет (кухня) включён на 100%."
        assert _lights(runtime)['кухня']['on'] is True
        second, _, _ = await _open_session(runtime, url, "выключи свет на кухне")
        assert second.Event.AliceResponse.Response.Cards[0].Text == "Свет (кухня) выключен."
        await ws.close()
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_light_without_entities_honest_refusal(tmp_path):
    runtime = Runtime(tmp_path)
    device_with_light(runtime.identities, {})
    try:
        _, text = await _say(runtime, "включи свет")
        assert text == "Лампы не настроены: добавьте state.light устройству."
    finally:
        await runtime.close()
