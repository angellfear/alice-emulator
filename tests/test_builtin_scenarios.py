"""R07+M08: встроенный сценарий времени через реальный WS-путь TextInput.

Флаг builtin_scenarios выключен по умолчанию — голосовая заглушка не меняется;
при включении «сколько времени» отвечает сценарий (executor+composer+dialog)."""
import asyncio
import datetime

import pytest
from aiohttp import ClientSession

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen
from tests.conftest import types


async def _text_roundtrip(runtime, tmp_path, phrase):
    client_type, server_type = types()
    text = client_type()
    text.Event.Header.MessageId = 'm1'
    text.Event.TextInput.Header.RequestId = 'rq1'
    text.Event.TextInput.Header.DialogId = 'dg1'
    text.Event.TextInput.Request.Event.Text = phrase
    sync = client_type()
    sync.Event.Header.MessageId = 'sync'
    sync.Event.SynchronizeStateRequest.Application.DeviceId = 'test-device'
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-T', 'user_id': 'owner',
                               'device_ids': ['test-device'], 'profile': {'aapi_model': True}})
    runner, url = await listen(create_app(runtime=runtime))
    received = []
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url + '/uni.ws') as ws:
                await ws.send_bytes(b'AAPI' + sync.SerializeToString())
                await asyncio.wait_for(ws.receive(), 3)  # SynchronizeStateResponse
                await ws.send_bytes(b'AAPI' + text.SerializeToString())
                for _ in range(3):
                    packet = await asyncio.wait_for(ws.receive(), 3)
                    if packet.data.startswith(b'AAPI'):
                        message = server_type.FromString(packet.data[4:])
                        received.append(message)
                        if message.Event.WhichOneof('Event') == 'AliceResponse':
                            break
    finally:
        await runner.cleanup()
    return received


async def test_builtin_clock_scenario_answers_through_dialog(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings['aapi_model'] = True
    runtime.settings['builtin_scenarios'] = True  # явное включение семейства
    before = datetime.datetime.now()
    received = await _text_roundtrip(runtime, tmp_path, 'сколько времени')
    responses = [m for m in received if m.Event.WhichOneof('Event') == 'AliceResponse']
    assert responses, [m.Event.WhichOneof('Event') for m in received]
    response = responses[0].Event.AliceResponse
    card = response.Response.Cards[0].Text
    assert card.startswith('Сейчас') and card.endswith('.')  # local_time_phrase
    hour = before.hour
    assert str(hour % 24) in card or str(hour % 12 or 12) in card
    assert response.Header.RequestId == 'rq1'  # correlation ID из запроса


async def test_builtin_disabled_keeps_stub_path(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings['aapi_model'] = True
    runtime.settings['builtin_scenarios'] = False  # умолчание теперь True
    runtime.settings['model']['stub_text'] = 'заглушка'
    received = await _text_roundtrip(runtime, tmp_path, 'сколько времени')
    responses = [m for m in received if m.Event.WhichOneof('Event') == 'AliceResponse']
    assert responses and responses[0].Event.AliceResponse.Response.Cards[0].Text == 'заглушка'


async def test_builtin_flag_migrated_for_old_settings(tmp_path):
    """Настройки засеяны ДО появления ключа builtin_scenarios — дефолт True доносится."""
    import asyncio
    runtime = Runtime(tmp_path)
    await runtime.close()
    # стираем ключ, как в старой production-БД
    runtime2 = Runtime(tmp_path)
    value = runtime2.store.load_settings()
    del value["builtin_scenarios"]
    runtime2.store.save_settings(value)
    await runtime2.close()
    runtime3 = Runtime(tmp_path)
    assert runtime3.settings.get("builtin_scenarios") is True  # миграция донесла дефолт
    catalog = runtime3.dialog.scenarios()
    assert any(s["id"] == "informers.clock" for s in catalog)  # каталог встроенных снова полный
    await runtime3.close()
