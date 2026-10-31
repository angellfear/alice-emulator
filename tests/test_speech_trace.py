"""v9 §2: структурированная трасса речевого тракта — run ID, направления, терминалы."""
import asyncio
import io
import struct
import wave
from pathlib import Path

import pytest
from aiohttp import ClientSession

from server_emulator.runtime import Runtime
from server_emulator.schema import load_type
from server_emulator.server import create_app
from server_emulator.speech_trace import SpeechTrace, reason_name
from server_emulator.voice import Conversation
from tests.conftest import listen
from tests.conftest import page, types

ROOT = Path(__file__).resolve().parents[1] / 'server_emulator' / 'schemas'


def names(runtime):
    return [(e['name'], e['direction']) for e in runtime.speech_log]


def by_run(runtime):
    runs = {}
    for event in runtime.speech_log:
        runs.setdefault(event['run'], []).append(event)
    return runs


def test_trace_unit_directions_time_and_context():
    seen = []
    trace = SpeechTrace(seen.append, context={'session': 's1', 'sn': None})
    first = trace.event('voice_input_start', 'c2s', channel=3)
    with pytest.raises(ValueError):
        trace.event('bad', 'sideways')
    trace.context.update(sn='SN-1')  # контекст живёт с соединением
    second = trace.event('client_close', 'c2s', reason=reason_name(0))
    assert first['run'] == second['run']  # единый run ID
    assert first['t_ms'] <= second['t_ms']
    assert 'sn' not in first and second['sn'] == 'SN-1'  # None не пишется, актуальное значение пишется
    assert 'session' in first and first['session'] == 's1'
    trace.bump('audio_bytes', 100)
    assert trace.snapshot() == {'audio_bytes': 100}
    assert reason_name(5) == 'NO_SPEECH' and reason_name(99) == '99'


async def test_voice_pipeline_full_trace_one_run(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_voice=True, aapi_tts=True)
    ogg = page(2, b'OpusHead'+bytes([1, 1])+struct.pack('<HIhB', 312, 16000, 0, 0), 0)+page(4, b'\xf8\xff\xfe', 1)

    async def provider(kind, payload=None, audio=None, filename=None):
        if kind == 'stt':
            return b'{"text":"hello"}', 'application/json'
        if kind == 'model':
            return b'{"choices":[{"message":{"content":"answer"}}]}', 'application/json'
        return ogg, 'audio/ogg'
    runtime.provider = provider
    client_type, server_type = types()
    incoming = client_type()
    incoming.Event.Header.MessageId = 'voice-message'
    incoming.Event.Header.StreamId = 3
    incoming.Event.VoiceInput.Header.RequestId = 'request'
    incoming.Event.VoiceInput.Format = 'audio/x-pcm;bit=16;rate=16000;channels=1'
    close = client_type()
    close.StreamControl.StreamId = 3
    close.StreamControl.Close.Reason = 0
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url + '/uni.ws') as ws:
                runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
                runtime.identities.mutate({'operation': 'device', 'sn': 'SN-T', 'user_id': 'owner', 'device_ids': ['dev-t']})
                sync = client_type()
                sync.Event.Header.MessageId = 'sync'
                sync.Event.SynchronizeStateRequest.Application.DeviceId = 'dev-t'
                await ws.send_bytes(b'AAPI' + sync.SerializeToString())
                await asyncio.wait_for(ws.receive(), 3)
                await ws.send_bytes(b'AAPI' + incoming.SerializeToString())
                await ws.send_bytes(struct.pack('>I', 3) + b'\0\0' * 160)
                await ws.send_bytes(b'AAPI' + close.SerializeToString())
                for _ in range(10):
                    packet = await asyncio.wait_for(ws.receive(), 3)
                    if packet.data.startswith(b'AAPI'):
                        message = server_type.FromString(packet.data[4:])
                        if message.WhichOneof('Message') == 'StreamControl' and message.StreamControl.StreamId == 2:
                            break
        events = names(runtime)
        assert ('voice_input_start', 'c2s') in events
        assert ('client_close', 'c2s') in events
        # Один run ID прошёл весь тракт; идентичность соединения в каждом событии
        runs = by_run(runtime)
        assert len(runs) == 1
        run_events = next(iter(runs.values()))
        # внутренние решения сервера отличаются по направлению от отправленных клиенту
        for required in ('model_done', 'tts_done'):
            assert (required, 'server') in events, events
        for required in ('asr_final', 'alice_response', 'tts_audio', 'stream_close'):
            assert (required, 's2c') in events, events
        assert all(e.get('sn') == 'SN-T' for e in run_events if e['name'] != 'voice_input_start' or True)
        assert all(e.get('session') for e in run_events)
        close_events = [e for e in run_events if e['name'] == 'client_close']
        assert close_events[0]['audio_bytes'] == 320  # счётчик байтов входного аудио
        assert close_events[0]['reason'] == 'OK'
    finally:
        await runner.cleanup()


async def test_no_speech_is_server_decision(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_voice=True)

    async def provider(kind, payload=None, audio=None, filename=None):
        return b'{"text":""}', 'application/json'  # пустая расшифровка
    runtime.provider = provider
    client_type, server_type = types()
    incoming = client_type()
    incoming.Event.Header.MessageId = 'm'
    incoming.Event.Header.StreamId = 3
    incoming.Event.VoiceInput.Header.RequestId = 'r'
    incoming.Event.VoiceInput.Format = 'audio/x-pcm;bit=16;rate=16000;channels=1'
    close = client_type()
    close.StreamControl.StreamId = 3
    close.StreamControl.Close.Reason = 0
    sent = []

    async def send(data):
        sent.append(data)
    conversation = Conversation(runtime, server_type, send)
    await conversation.receive(incoming, 'VoiceInput')
    await conversation.receive_audio(struct.pack('>I', 3) + b'\0\0' * 10)
    await conversation.receive(close, 'StreamControl')
    for _ in range(50):
        await asyncio.sleep(0)
    events = names(runtime)
    assert ('no_speech', 'server') in events  # направление server, не client
    no_speech = next(e for e in runtime.speech_log if e['name'] == 'no_speech')
    assert no_speech['decision'] == 'NO_SPEECH'
    # Реальный wire (capture): канал закрывается OK, следом реплика-повтор
    assert ('no_speech_retry', 's2c') in events
    from server_emulator.schema import load_type
    server_type = load_type(ROOT, 'alice-api.pb', 'NAlice.NAliceApi.TServerMessage')
    replies = [server_type.FromString(x[4:]).Event.AliceResponse for x in sent
               if x.startswith(b'AAPI') and server_type.FromString(x[4:]).Event.WhichOneof('Event') == 'AliceResponse']
    assert any('не расслышала' in r.Response.Cards[0].Text for r in replies)
    await conversation.shutdown()


async def test_cancel_traced_as_client_cause(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings['aapi_model'] = True
    started = asyncio.Event()

    async def chat(text, history=None, **context):
        started.set()
        await asyncio.Event().wait()
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
    events = names(runtime)
    assert ('cancel', 'c2s') in events
    cancel_event = next(e for e in runtime.speech_log if e['name'] == 'cancel')
    assert cancel_event['cause'] == 'CancelRequest'
    await session.shutdown()


async def test_speech_trace_api_endpoint(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings['aapi_model'] = True

    async def chat(text, history=None, **context):
        return {'text': 'ok', 'tool_calls': []}
    runtime.chat = chat
    sent = []

    async def send(data):
        sent.append(data)
    client, server = types()
    session = Conversation(runtime, server, send)
    text = client()
    text.Event.Header.MessageId = 'text'
    text.Event.TextInput.Request.Event.Text = 'hi'
    await session.receive(text, 'TextInput')
    await asyncio.wait_for(session.task, 1)
    await session.shutdown()
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client_http:
            async with client_http.get(url + '/emulator/api/speech?limit=10') as response:
                body = await response.json()
                assert response.status == 200
                assert ('run_start', 'server') in [(e['name'], e['direction']) for e in body['events']]
    finally:
        await runner.cleanup()


async def test_empty_partial_recognition_not_sent(tmp_path):
    """Колонка показывает текст из AggregatedRecognition: пустые паршлы не уходят."""
    from server_emulator.runtime import Runtime
    from server_emulator.voice import Conversation
    runtime = Runtime(tmp_path)
    sent = []

    async def send(data):
        sent.append(data)
    client, server = types()
    conversation = Conversation(runtime, server, send)
    incoming = client()
    incoming.Event.Header.MessageId = 'm'
    await conversation.recognition(incoming, '', final=False)
    await conversation.recognition(incoming, '   ', final=False)
    assert sent == []  # пустые паршлы отфильтрованы
    await conversation.recognition(incoming, 'привет', final=False)
    message = server.FromString(sent[0][4:])
    asr = message.Event.AsrResult
    assert asr.AggregatedRecognition.RecognizedText == 'привет'  # поле экрана колонки
    assert asr.Recognition[0].Normalized == 'привет'
    await conversation.shutdown()
    await runtime.close()


async def test_client_no_speech_close_gets_retry_prompt(tmp_path):
    """Колонка закрыла микрофон NO_SPEECH → сервер досылает финал, закрывает OK, отвечает репликой."""
    import struct as _struct
    runtime = Runtime(tmp_path)
    runtime.settings['aapi_voice'] = True
    sent = []

    async def send(data):
        sent.append(data)
    client, server = types()
    conversation = Conversation(runtime, server, send)
    incoming = client()
    incoming.Event.Header.MessageId = 'm'
    incoming.Event.Header.StreamId = 3
    incoming.Event.VoiceInput.Header.RequestId = 'r'
    incoming.Event.VoiceInput.Format = 'audio/x-pcm;bit=16;rate=16000;channels=1'
    close = client()
    close.StreamControl.StreamId = 3
    close.StreamControl.Close.Reason = 5
    await conversation.receive(incoming, 'VoiceInput')
    await conversation.receive_audio(_struct.pack('>I', 3) + b'\0\0' * 10)
    await conversation.receive(close, 'StreamControl')
    for _ in range(60):
        await asyncio.sleep(0)
    from server_emulator.schema import load_type
    server_type = load_type(ROOT, 'alice-api.pb', 'NAlice.NAliceApi.TServerMessage')
    asr = [server_type.FromString(x[4:]) for x in sent
           if x.startswith(b'AAPI') and server_type.FromString(x[4:]).Event.WhichOneof('Event') == 'AsrResult']
    assert asr and asr[-1].Event.AsrResult.EndOfUtt  # финальный (пустой) AsrResult
    closes = [server_type.FromString(x[4:]) for x in sent
              if x.startswith(b'AAPI') and server_type.FromString(x[4:]).WhichOneof('Message') == 'StreamControl']
    assert closes and closes[0].StreamControl.Close.Reason == 0  # OK, как реальный сервер
    replies = [server_type.FromString(x[4:]).Event.AliceResponse for x in sent
               if x.startswith(b'AAPI') and server_type.FromString(x[4:]).Event.WhichOneof('Event') == 'AliceResponse']
    assert any('не расслышала' in r.Response.Cards[0].Text for r in replies)
    await conversation.shutdown()
