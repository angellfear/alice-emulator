import asyncio
import io
import json
from pathlib import Path
import struct
import wave

import pytest
from aiohttp import ClientSession

from server_emulator.runtime import Runtime
from server_emulator.schema import load_type
from server_emulator.server import create_app
from server_emulator.voice import Conversation, input_audio, ogg_pages
from tests.conftest import listen, page, types


def test_audio_requires_matching_format_and_complete_tts():
    raw, filename = input_audio(b'\0\0'*320, 'audio/x-pcm;bit=16;rate=16000;channels=1')
    with wave.open(io.BytesIO(raw)) as audio:
        assert audio.getnframes() == 320 and audio.getframerate() == 16000
    assert filename == 'input.wav'
    with pytest.raises(ValueError):
        input_audio(b'\0', 'audio/x-pcm;bit=16;rate=16000;channels=1')
    with pytest.raises(ValueError):
        ogg_pages(b'RIFFnot-ogg')
    with pytest.raises(ValueError):
        ogg_pages(page(2, b'OpusHead'+b'\0'*11, 0))


async def test_websocket_voice_stt_model_tts_and_channel_correlation(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_voice=True, aapi_tts=True)
    calls = []
    ogg = page(2, b'OpusHead'+bytes([1, 1])+struct.pack('<HIhB', 312, 16000, 0, 0), 0)+page(4, b'\xf8\xff\xfe', 1)
    async def provider(kind, payload=None, audio=None, filename=None):
        calls.append(kind)
        if kind == 'stt':
            with wave.open(io.BytesIO(audio)) as source:
                assert source.getnframes() == 160
            assert filename == 'input.wav'
            return b'{"text":"hello"}', 'application/json'
        if kind == 'model':
            assert payload['messages'][-1]['content'] == 'hello'
            return b'{"choices":[{"message":{"content":"answer"}}]}', 'application/json'
        assert payload['input'] == 'answer'
        return ogg, 'audio/ogg'
    runtime.provider = provider
    client_type, server_type = types()
    incoming = client_type()
    incoming.Event.Header.MessageId = 'voice-message'
    incoming.Event.Header.StreamId = 3
    incoming.Event.VoiceInput.Header.RequestId = 'request'
    incoming.Event.VoiceInput.Header.DialogId = 'dialog'
    incoming.Event.VoiceInput.Format = 'audio/x-pcm;bit=16;rate=16000;channels=1'
    close = client_type()
    close.StreamControl.StreamId = 3
    close.StreamControl.Close.Reason = 0
    runner, url = await listen(create_app(runtime=runtime))
    seen, audio_frames = [], []
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url+'/uni.ws') as ws:
                runtime.identities.mutate({'operation':'user','id':'owner','name':'Test owner'})
                runtime.identities.mutate({'operation':'device','sn':'TEST-SN','user_id':'owner','device_ids':['test-device']})
                sync = client_type()
                sync.Event.Header.MessageId = 'sync'
                sync.Event.SynchronizeStateRequest.Application.DeviceId = 'test-device'
                await ws.send_bytes(b'AAPI'+sync.SerializeToString())
                synchronized = server_type.FromString((await ws.receive()).data[4:])
                assert synchronized.Event.WhichOneof('Event') == 'SynchronizeStateResponse'
                await ws.send_bytes(b'AAPI'+incoming.SerializeToString())
                await ws.send_bytes(struct.pack('>I', 3)+b'\0\0'*160)
                await ws.send_bytes(b'AAPI'+close.SerializeToString())
                for _ in range(10):
                    packet = await asyncio.wait_for(ws.receive(), 3)
                    if not packet.data.startswith(b'AAPI'):
                        audio_frames.append(packet.data)
                        continue
                    message = server_type.FromString(packet.data[4:])
                    if message.WhichOneof('Message') == 'StreamControl':
                        assert message.StreamControl.Close.Reason == 0
                        assert message.StreamControl.StreamId == 2
                        break
                    name = message.Event.WhichOneof('Event')
                    seen.append(name)
                    assert message.Event.Header.RefMessageId == 'voice-message'
                    if name == 'AliceResponse':
                        assert message.Event.AliceResponse.Header.RequestId == 'request'
                        assert message.Event.AliceResponse.VoiceResponse.HasVoiceResponse
                    if name == 'TtsSpeak':
                        assert message.Event.Header.StreamId == 2
                else:
                    pytest.fail('No stream close')
        assert calls == ['stt', 'model', 'tts']
        assert seen == ['InputStartAck', 'AsrResult', 'AliceResponse', 'TtsSpeak']
        assert b''.join(frame[4:] for frame in audio_frames) == ogg
    finally:
        await runner.cleanup()


async def test_cancel_interrupts_provider_and_never_sends_stale_answer(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings['aapi_model'] = True
    started = asyncio.Event()
    cancelled = asyncio.Event()
    async def chat(text, history=None, **context):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    runtime.chat = chat
    sent = []
    async def send(data):
        sent.append(data)
    client, server = types()
    session = Conversation(runtime, server, send)
    text = client()
    text.Event.Header.MessageId = 'text'
    text.Event.TextInput.Request.Event.Text = 'wait'
    await session.receive(text, 'TextInput')
    await asyncio.wait_for(started.wait(), 1)
    cancel = client()
    cancel.Event.CancelRequest.SetInParent()
    await session.receive(cancel, 'CancelRequest')
    assert cancelled.is_set()
    assert len(sent) == 1
    assert server.FromString(sent[0][4:]).Event.WhichOneof('Event') == 'InputStartAck'
    await session.shutdown()


async def test_tts_failure_keeps_text_response(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, aapi_tts=True)
    async def chat(text, history=None, **context):
        return {'text': 'text survives', 'tool_calls': []}
    async def provider(*args, **kwargs):
        raise ValueError('TTS unavailable')
    runtime.chat, runtime.provider = chat, provider
    sent = []
    async def send(data):
        sent.append(data)
    client, server = types()
    session = Conversation(runtime, server, send)
    incoming = client()
    incoming.Event.Header.MessageId = 'text'
    incoming.Event.TextInput.Request.Event.Text = 'hello'
    await session.receive(incoming, 'TextInput')
    await asyncio.wait_for(session.task, 1)
    response = server.FromString(sent[-1][4:]).Event.AliceResponse
    assert response.Response.Cards[0].Text == 'text survives'
    assert not response.VoiceResponse.HasVoiceResponse
    assert runtime.recent[-1]['kind'] == 'tts'
    await session.shutdown()
