import asyncio
import copy
import struct
from pathlib import Path

import pytest
from aiohttp import ClientSession

from server_emulator.behavior import standard_for, validate_standard
from server_emulator.runtime import Runtime
from server_emulator.schema import load_type
from server_emulator.server import create_app
from server_emulator.voice import Conversation
from tests.conftest import listen

ROOT = Path(__file__).resolve().parents[1] / 'server_emulator' / 'schemas'


def types():
    return (load_type(ROOT, 'alice-api.pb', 'NAlice.NAliceApi.TClientMessage'),
            load_type(ROOT, 'alice-api.pb', 'NAlice.NAliceApi.TServerMessage'))


def page(flags, packet, seq):
    return b'OggS\0' + bytes([flags]) + struct.pack('<QII', 960 if seq else 0, 123, seq) + b'\0'*4 + bytes([1, len(packet)]) + packet


def ogg():
    return page(2, b'OpusHead' + b'\0'*11, 0) + page(4, b'\xf8\xff\xfe', 1)


def test_validate_standard_rejects_bad_entries():
    validate_standard([{'id': 'a', 'name': 'A', 'case': 'unclaimed', 'enabled': True, 'text': 'Реплика'}])
    with pytest.raises(ValueError):
        validate_standard([{'id': 'a', 'name': 'A', 'case': 'unknown-case', 'enabled': True, 'text': 'x'}])
    with pytest.raises(ValueError):
        validate_standard([{'id': 'a', 'name': 'A', 'case': 'unclaimed', 'enabled': True, 'text': '  '}])
    with pytest.raises(ValueError):
        validate_standard([{'id': 'a', 'name': 'A', 'case': 'unclaimed', 'enabled': True, 'text': 'x'},
                           {'id': 'a', 'name': 'B', 'case': 'unclaimed', 'enabled': True, 'text': 'y'}])


def test_standard_for_picks_first_enabled():
    rules = [{'id': 'off', 'case': 'unclaimed', 'enabled': False, 'text': 'a'},
             {'id': 'on', 'case': 'unclaimed', 'enabled': True, 'text': 'b'}]
    assert standard_for(rules, 'unclaimed')['id'] == 'on'
    assert standard_for(rules, 'other') is None
    assert standard_for(None, 'unclaimed') is None


def test_defaults_carry_unclaimed_reply(tmp_path):
    runtime = Runtime(tmp_path)
    reply = standard_for(runtime.settings['standard_replies'], 'unclaimed')
    assert reply and reply['text'].strip()


async def test_unclaimed_text_input_gets_standard_reply_not_model(tmp_path):
    runtime = Runtime(tmp_path)
    scoped = copy.copy(runtime)
    scoped.settings = runtime.identities.settings_for(runtime.settings, None)
    assert scoped.settings['unclaimed'] is True and scoped.settings['aapi_model'] is False
    calls = []

    async def provider(kind, payload=None, **_):
        calls.append(kind)
        assert kind == 'tts'
        assert payload['input'] == standard_for(runtime.settings['standard_replies'], 'unclaimed')['text']
        return ogg(), 'audio/ogg'
    scoped.provider = provider
    scoped.pending_calls, scoped.recent = [], []

    sent = []

    async def send(data):
        sent.append(data)
    _, server = types()
    conversation = Conversation(scoped, server, send)
    client, _ = types()
    text = client()
    text.Event.Header.MessageId = 'text-1'
    text.Event.TextInput.Header.RequestId = 'req-1'
    text.Event.TextInput.Request.Event.Text = 'включи свет'
    assert await conversation.receive(text, 'TextInput') is True
    await asyncio.wait_for(conversation.task, 3)

    names = []
    for frame in sent:
        if not frame.startswith(b'AAPI'):
            continue
        message = server.FromString(frame[4:])
        if message.WhichOneof('Message') == 'Event':
            names.append(message.Event.WhichOneof('Event'))
            if names[-1] == 'AliceResponse':
                response = message.Event.AliceResponse
                assert response.Header.RequestId == 'req-1'
                assert response.VoiceResponse.HasVoiceResponse
                assert response.VoiceResponse.OutputSpeech.Text == standard_for(
                    runtime.settings['standard_replies'], 'unclaimed')['text']
    assert names == ['InputStartAck', 'AliceResponse', 'TtsSpeak']
    assert calls == ['tts']  # модель и распознавание не вызываются


async def test_unclaimed_websocket_session_gets_spoken_reply(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings['mode'] = 'emulate'

    async def provider(kind, payload=None, **_):
        return ogg(), 'audio/ogg'
    runtime.provider = provider
    client_type, server_type = types()
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url + '/uni.ws', headers={'X-UPRX-DEVICE-ID': 'stranger-device'}) as ws:
                text = client_type()
                text.Event.Header.MessageId = 'm1'
                text.Event.TextInput.Header.RequestId = 'r1'
                text.Event.TextInput.Request.Event.Text = 'привет'
                await ws.send_bytes(b'AAPI' + text.SerializeToString())
                names = []
                for _ in range(6):
                    packet = await asyncio.wait_for(ws.receive(), 3)
                    if not packet.data.startswith(b'AAPI'):
                        continue
                    message = server_type.FromString(packet.data[4:])
                    if message.WhichOneof('Message') != 'Event':
                        continue
                    names.append(message.Event.WhichOneof('Event'))
                    if names[-1] == 'AliceResponse':
                        assert message.Event.AliceResponse.Header.RequestId == 'r1'
                        assert 'не привязана' in message.Event.AliceResponse.VoiceResponse.OutputSpeech.Text
                        break
                else:
                    pytest.fail('нет стандартного ответа')
                assert names == ['InputStartAck', 'AliceResponse']
    finally:
        await runner.cleanup()


async def test_save_settings_validates_standard_replies(tmp_path):
    runtime = Runtime(tmp_path)
    value = copy.deepcopy(runtime.settings)
    value['standard_replies'] = [{'id': 'x', 'name': 'X', 'case': 'nope', 'enabled': True, 'text': 't'}]
    with pytest.raises(ValueError):
        await runtime.save_settings(value)
