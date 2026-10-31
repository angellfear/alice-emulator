"""End-to-end голосового потока (DEBT-08): AAPI WS → ASR → Router → сценарий/агент → TTS → AAPI.

Тесты не требуют внешних сервисов: ASR/LLM/TTS — заглушки в runtime.provider.
Конвейер проверяется насквозь через настоящее WS-соединение /uni.ws, а не вызовами
функций voice.py по отдельности — это страховка при разбиении voice.py (TASK-005/008).
"""
import asyncio
import struct

from aiohttp import ClientSession

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen, page, types


def _tts_ogg():
    """Минимальный корректный Ogg Opus (BoS + EOS) для заглушки TTS."""
    return (page(2, b'OpusHead' + bytes([1, 1]) + struct.pack('<HIhB', 312, 16000, 0, 0), 0) +
            page(4, b'\xf8\xff\xfe', 1))


async def _voice_roundtrip(runtime, spoken_text, tts_ogg):
    """Полный голосовой проход по WS: синхронизация устройства, VoiceInput (PCM),
    закрытие потока. Возвращает (events, audio_frames, provider_calls)."""
    calls = []

    async def provider(kind, payload=None, audio=None, filename=None):
        calls.append(kind)
        if kind == 'stt':
            return f'{{"text":"{spoken_text}"}}'.encode(), 'application/json'
        if kind == 'model':
            return b'{"choices":[{"message":{"content":"stub answer"}}]}', 'application/json'
        assert kind == 'tts' and payload['input'], payload
        return tts_ogg, 'audio/ogg'
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
    events, audio_frames = [], []
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url + '/uni.ws') as ws:
                runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
                runtime.identities.mutate({'operation': 'device', 'sn': 'SN-T', 'user_id': 'owner',
                                           'device_ids': ['test-device']})
                sync = client_type()
                sync.Event.Header.MessageId = 'sync'
                sync.Event.SynchronizeStateRequest.Application.DeviceId = 'test-device'
                await ws.send_bytes(b'AAPI' + sync.SerializeToString())
                synchronized = server_type.FromString((await asyncio.wait_for(ws.receive(), 3)).data[4:])
                assert synchronized.Event.WhichOneof('Event') == 'SynchronizeStateResponse'

                await ws.send_bytes(b'AAPI' + incoming.SerializeToString())
                await ws.send_bytes(struct.pack('>I', 3) + b'\0' * 320)
                await ws.send_bytes(b'AAPI' + close.SerializeToString())

                for _ in range(24):
                    packet = await asyncio.wait_for(ws.receive(), 5)
                    if not packet.data.startswith(b'AAPI'):
                        audio_frames.append(packet.data)
                        continue
                    message = server_type.FromString(packet.data[4:])
                    if message.WhichOneof('Message') == 'StreamControl':
                        break
                    events.append((message.Event.WhichOneof('Event'), message))
    finally:
        await runner.cleanup()
    return events, audio_frames, calls


async def test_full_pipeline_stubs_answer_with_voice(tmp_path):
    """Базовый ответ: аудио → ASR stub → агент (model stub) → TTS stub → AliceResponse + TtsSpeak."""
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_voice=True, aapi_tts=True)
    ogg = _tts_ogg()
    try:
        events, audio_frames, calls = await _voice_roundtrip(runtime, 'hello', ogg)
        names = [name for name, _ in events]
        assert names == ['InputStartAck', 'AsrResult', 'AliceResponse', 'TtsSpeak'], names
        assert calls == ['stt', 'model', 'tts'], calls

        by_name = dict(events)
        asrs = [m.Event.AsrResult for name, m in events if name == 'AsrResult']
        assert any(r.EndOfUtt for r in asrs), 'нет финального AsrResult'
        assert any(rec.Normalized == 'hello' for r in asrs for rec in r.Recognition), asrs
        response = by_name['AliceResponse'].Event.AliceResponse
        assert response.Header.RequestId == 'request'
        assert response.VoiceResponse.HasVoiceResponse
        for name in ('InputStartAck', 'AsrResult', 'AliceResponse'):
            assert by_name[name].Event.Header.RefMessageId == 'voice-message', name
        assert response.Response.Cards[0].Text == 'stub answer'
        assert b''.join(frame[4:] for frame in audio_frames) == ogg
    finally:
        await runtime.close()


async def test_scenario_phrase_handled_without_agent(tmp_path):
    """Фраза встроенного сценария: срабатывает сценарий, агент (model) не вызывается,
    реплика сценария озвучивается TTS."""
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_voice=True, aapi_tts=True, builtin_scenarios=True)
    ogg = _tts_ogg()
    try:
        events, audio_frames, calls = await _voice_roundtrip(runtime, 'сколько времени', ogg)
        assert 'model' not in calls, f'агент вызван вместо сценария: {calls}'
        assert calls == ['stt', 'tts'], calls

        names = [name for name, _ in events]
        assert 'AliceResponse' in names and 'TtsSpeak' in names, names
        response = next(m for name, m in events if name == 'AliceResponse').Event.AliceResponse
        assert response.Response.Cards[0].Text.startswith('Сейчас'), response.Response.Cards[0].Text
        assert b''.join(frame[4:] for frame in audio_frames) == ogg
    finally:
        await runtime.close()


async def test_scenarios_mode_no_model_and_phrase_queued(tmp_path):
    """Режим «Интеллект: сценарии» (TASK-011/013): фраза без сценария — стандартный ответ
    not_understood с голосом, модель не вызывается, фраза — в очереди «Без ответа»."""
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_voice=True, aapi_tts=True, intelligence='scenarios')
    try:
        events, _, calls = await _voice_roundtrip(runtime, 'hello', _tts_ogg())
        assert calls == ['stt', 'tts'], calls
        response = next(m for name, m in events if name == 'AliceResponse').Event.AliceResponse
        assert response.Response.Cards[0].Text == 'Я пока этого не умею.'
        assert response.VoiceResponse.HasVoiceResponse
        assert [m['key'] for m in runtime.phrase_misses.public()] == ['hello']
    finally:
        await runtime.close()
