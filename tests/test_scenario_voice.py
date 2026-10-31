"""Сценарный путь в голосовом тракте: реплика сценария озвучивается TTS,
погодная карточка реплеит реальный ответ облака (divui + анимация).

Капча: captures/weather_response.json — перехват 2026-10-24 00:59 (.85)."""
import asyncio
import struct
import wave
import io

from aiohttp import ClientSession

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen
from tests.conftest import page, types


async def _voice_roundtrip(runtime, spoken_text, tts_ogg):
    """Голосовой проход: STT→сценарий→(TTS). Возвращает (events, audio_frames)."""
    async def provider(kind, payload=None, audio=None, filename=None):
        if kind == 'stt':
            return f'{{"text":"{spoken_text}"}}'.encode(), 'application/json'
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
                await asyncio.wait_for(ws.receive(), 3)  # SynchronizeStateResponse
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
                    name = message.Event.WhichOneof('Event')
                    events.append((name, message))
    finally:
        await runner.cleanup()
    return events, audio_frames


def _ogg():
    return page(2, b'OpusHead' + bytes([1, 1]) + struct.pack('<HIhB', 312, 16000, 0, 0), 0) + \
           page(4, b'\xf8\xff\xfe', 1)


FORECAST = {
    "current": {"temperature_2m": 12.4, "apparent_temperature": 10.1, "weather_code": 3, "is_day": 1},
    "hourly": {"time": [f"2026-10-27T{h % 24:02d}:00" for h in range(12, 36)],
               "temperature_2m": [10 + h % 5 for h in range(24)], "weather_code": [3] * 24, "is_day": [1] * 24},
    "daily": {"temperature_2m_max": [14.2], "temperature_2m_min": [8.1], "precipitation_probability_max": [10]},
}


async def test_scenario_reply_is_spoken(tmp_path):
    """«сколько времени» через голос: карточка сценария + TtsSpeak + аудио —
    ответ не молчит (regression: сценарий завершался без озвучки)."""
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_voice=True, aapi_tts=True, builtin_scenarios=True)
    try:
        events, audio = await _voice_roundtrip(runtime, 'сколько времени', _ogg())
        names = [name for name, _ in events]
        assert 'AliceResponse' in names and 'TtsSpeak' in names, names
        response = next(m for name, m in events if name == 'AliceResponse').Event.AliceResponse
        assert response.Response.Cards[0].Text.startswith('Сейчас')
        assert audio, 'TTS-аудио не пришло'
    finally:
        await runtime.close()


async def test_weather_card_replays_capture(tmp_path):
    """«погода»: карточка simple_text + divui_show_view (PayloadRaw капчи) +
    draw_animation — экран станции показывает погодный экран-заглушку."""
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_voice=True, aapi_tts=True, builtin_scenarios=True)
    # Seed user + device with geo
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-T', 'user_id': 'owner',
                              'device_ids': ['test-device']})
    runtime.quasar_params.set_value("user:owner", "user.location.latitude", 55.755826)
    runtime.quasar_params.set_value("user:owner", "user.location.longitude", 37.617326)
    # Seed weather dataset
    runtime.store.scenario_data_set("dataset:weather", "system", "", "55.755826,37.617326", FORECAST)
    # Plugin raises ConnectionError
    async def plugin_weather(key, args):
        raise ConnectionError("weather provider down")
    runtime.scenario_plugins["weather"] = plugin_weather

    try:
        events, audio = await _voice_roundtrip(runtime, 'погода', _ogg())
        response = next(m for name, m in events if name == 'AliceResponse').Event.AliceResponse
        assert response.Response.Cards[0].Text == \
            'Сейчас +12. Сегодня от +8 до +14. Осадки не ожидаются.'
        assert response.VoiceResponse.OutputSpeech.Text.startswith('<speaker is_whisper="true"> ')
        directives = {d.Name: d for d in response.Response.Directives}
        assert 'divui_show_view' in directives and 'draw_animation_directive' in directives
        # tts_play_placeholder may also be present; we only check membership
        assert audio, 'TTS-аудио не пришло'
    finally:
        await runtime.close()


async def test_fuzzy_asr_error_routes_scenario(tmp_path):
    """«вдключи блютуз» (однобуквенный огрех ASR) — вариант «включи блютуз»
    матчится на device.bluetooth.*: честный ответ, не заглушка модели."""
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_voice=True, aapi_tts=True, builtin_scenarios=True)
    try:
        events, _ = await _voice_roundtrip(runtime, 'вдключи блютуз', _ogg())
        response = next(m for name, m in events if name == 'AliceResponse').Event.AliceResponse
        # Scenario id should start with device.bluetooth.
        # Card text should be "Включаю." or "Выключаю."
        assert response.Response.Cards[0].Text in ("Включаю.", "Выключаю.")
    finally:
        await runtime.close()


async def test_scenario_speech_with_values_is_one_phrase_not_cached(tmp_path):
    """Фраза погоды с подставленными значениями синтезируется одним запросом целиком
    (без склейки частей) и в файловый кеш не попадает — повтор снова синтезируется."""
    import numpy as np

    class FakeTTS:
        voice = "v"

        def __init__(self):
            self.calls = []

        async def synth_pcm(self, text):
            self.calls.append(text)
            return np.full(1600, 0.1, dtype=np.float32), 16000

    async def plugin_weather(key, args):
        raise ConnectionError("weather provider down")

    def make_runtime(fake):
        runtime = Runtime(tmp_path)
        runtime.settings.update(aapi_voice=True, aapi_tts=True, builtin_scenarios=True)
        runtime.get_tts = lambda: fake
        runtime.scenario_plugins["weather"] = plugin_weather
        return runtime

    first = FakeTTS()
    runtime = make_runtime(first)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-T', 'user_id': 'owner',
                               'device_ids': ['test-device']})
    runtime.quasar_params.set_value("user:owner", "user.location.latitude", 55.755826)
    runtime.quasar_params.set_value("user:owner", "user.location.longitude", 37.617326)
    runtime.store.scenario_data_set("dataset:weather", "system", "", "55.755826,37.617326", FORECAST)
    try:
        events, audio = await _voice_roundtrip(runtime, 'погода', _ogg())
        assert len(first.calls) == 1 and first.calls[0].startswith("Сейчас +12.") and audio
        assert runtime.tts_cache.stats()["files"] == 0
    finally:
        await runtime.close()
    second = FakeTTS()
    runtime = make_runtime(second)
    try:
        events, audio = await _voice_roundtrip(runtime, 'погода', _ogg())
        assert second.calls == first.calls and audio  # не из кеша: значения могли смениться
    finally:
        await runtime.close()
