"""Режимы вывода ответа (карточка «Синтез (TTS)»): settings["voice_stack"].speak/card —
озвучивать ли ответ и показывать ли карточку simple_text. Озвучка выключена — синтез не
вызывается ни для модели, ни для сценария, VoiceResponse пустой и с tts_play_placeholder
(как беззвучные ответы настоящего сервера при disable_output_speech), сервер пакета TTS
и прогрев звукового кеша не запускаются; карточка выключена — simple_text не добавляется;
«не озвучивать + без карточки» сохраняется как «не озвучивать + карточка».

Только локальные серверы: провайдеры подменены.
"""
import asyncio
import copy
import json

from server_emulator.runtime import Runtime
from server_emulator.scenario_engine.reply_compose import compose_reply
from server_emulator.settings_service import DEFAULT, SettingsService
from server_emulator.voice import Conversation
from tests.conftest import types
from tests.test_voice_failures import make_ogg

SCHEMAS = "server_emulator/schemas"


# --- валидация настроек -------------------------------------------------------

def test_validate_forces_card_without_speech(tmp_path):
    service = SettingsService(None, tmp_path)  # validate хранилище не трогает
    value = copy.deepcopy(DEFAULT)
    value["voice_stack"] = {"speak": False, "card": False}
    out = service.validate(value, copy.deepcopy(DEFAULT))
    assert out["voice_stack"]["speak"] is False and out["voice_stack"]["card"] is True


def test_validate_defaults_are_speak_and_card(tmp_path):
    service = SettingsService(None, tmp_path)
    out = service.validate(copy.deepcopy(DEFAULT), copy.deepcopy(DEFAULT))
    assert out["voice_stack"]["speak"] is True and out["voice_stack"]["card"] is True


# --- сценарный ответ (compose_reply — общий составитель JSON-сценариев) -------

def _ctx(speak=True, card=True):
    return {"input": {"request_id": "r"}, "settings": {"tts_speak": speak, "tts_card": card}}


def test_scenario_speaks_and_shows_card_by_default():
    response = compose_reply({"text": "Ответ", "speech": "Реплика"}, _ctx(), SCHEMAS).Event.AliceResponse
    assert [c.Text for c in response.Response.Cards] == ["Ответ"]
    assert response.VoiceResponse.OutputSpeech.Text == "Реплика"


def test_scenario_without_card():
    response = compose_reply({"text": "Ответ", "speech": "Реплика"}, _ctx(card=False),
                             SCHEMAS).Event.AliceResponse
    assert not response.Response.Cards and response.VoiceResponse.OutputSpeech.Text == "Реплика"


def test_scenario_silent_keeps_card_and_empty_voice():
    # озвучка выключена: карточка настроена включённой (валидатор не даёт её снять),
    # VoiceResponse пустой — как настоящий сервер при disable_output_speech
    response = compose_reply({"text": "Ответ", "speech": "Реплика"},
                             _ctx(speak=False, card=True), SCHEMAS).Event.AliceResponse
    assert [c.Text for c in response.Response.Cards] == ["Ответ"]
    assert response.VoiceResponse.SerializeToString() == b""


# --- ответ модели через Conversation ------------------------------------------

async def _model_roundtrip(tmp_path, speak, card):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, aapi_tts=True)
    runtime.settings["voice_stack"] = {"speak": speak, "card": card}
    ogg, tts = await make_ogg(), []

    async def provider(kind, payload=None, **kwargs):
        if kind == "tts":
            tts.append(payload["input"])
            return ogg, "audio/ogg"
        return json.dumps({"choices": [{"message": {"content": "Готово."}}], "usage": {}}).encode(), "application/json"
    runtime.provider = provider
    client, server = types()
    sent = []

    async def send(data):
        sent.append(data)
    conv = Conversation(runtime, server, send, trace_context={"sn": "SN-T"})
    incoming = client()
    incoming.Event.Header.MessageId = "m1"
    incoming.Event.TextInput.Request.Event.Text = "расскажи что-нибудь"
    await conv.receive(incoming, "TextInput")
    await asyncio.wait_for(conv.task, 10)
    await conv.shutdown()
    await runtime.close()
    response = [server.FromString(raw[4:]).Event.AliceResponse for raw in sent
                if raw.startswith(b"AAPI")
                and server.FromString(raw[4:]).Event.WhichOneof("Event") == "AliceResponse"][-1]
    return response, tts


async def test_silent_model_reply_has_card_and_empty_voice(tmp_path):
    response, tts = await _model_roundtrip(tmp_path, speak=False, card=True)
    assert tts == []  # синтез не вызывался
    assert [c.Text for c in response.Response.Cards] == ["Готово."]
    assert response.VoiceResponse.SerializeToString() == b""  # пустой, как у настоящего сервера
    assert "tts_play_placeholder" in [d.Name for d in response.Response.Directives]


async def test_card_off_model_reply_keeps_voice(tmp_path):
    response, tts = await _model_roundtrip(tmp_path, speak=True, card=False)
    assert not response.Response.Cards
    assert response.VoiceResponse.HasVoiceResponse and tts == ["Готово."]
    assert response.VoiceResponse.OutputSpeech.Text == "Готово."


# --- сценарий через голосовой проход ------------------------------------------

async def test_silent_scenario_keeps_card_without_synth(tmp_path):
    """Озвучка выключена: сценарий отдаёт карточку без синтеза — tts-провайдер не дёргается."""
    import struct

    from aiohttp import ClientSession

    from server_emulator.server import create_app
    from tests.conftest import listen, page

    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_voice=True, aapi_tts=True, builtin_scenarios=True)
    runtime.settings["voice_stack"] = {"speak": False, "card": True}

    async def provider(kind, payload=None, audio=None, filename=None):
        if kind == 'stt':
            return '{"text":"сколько времени"}'.encode(), 'application/json'
        raise AssertionError("синтез не должен вызываться при выключенной озвучке")
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
    responses = []
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
                        continue
                    message = server_type.FromString(packet.data[4:])
                    if message.WhichOneof('Message') != 'Event':
                        continue
                    if message.Event.WhichOneof('Event') == 'AliceResponse':
                        responses.append(message.Event.AliceResponse)
                        break  # без звука событий мало — ответ получен, цикл не ждёт дальше
    finally:
        await runner.cleanup()
        await runtime.close()
    assert responses, 'сценарий не ответил'
    response = responses[-1]
    assert [c.Text for c in response.Response.Cards][0].startswith('Сейчас')
    assert not response.VoiceResponse.HasVoiceResponse
    assert response.VoiceResponse.OutputSpeech.Text == ''  # пустой VoiceResponse — как у облака


# --- запуск TTS-сервера пакета и прогрев звукового кеша ------------------------

async def test_autostart_skips_tts_and_warm_when_silent(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings["voice_stack"] = {"speak": False, "card": True,
                                       "asr": "k:asr-pkg", "tts": "k:tts-pkg"}
    started, warmed = [], []
    async def start(pkg):
        started.append(pkg)

    async def wait_ready(pkg, timeout):
        return "already"
    runtime.voice_stack.local.start = start
    runtime.voice_stack.local.wait_ready = wait_ready
    runtime.voice_stack.package = lambda identifier: {"id": identifier}
    runtime.voice_stack.active = lambda service: {"tts": "k:tts-pkg", "asr": "k:asr-pkg"}.get(service, "")

    async def warm(tts, texts):
        warmed.append(texts)
    runtime.tts_cache.warm = warm
    runtime.get_tts = lambda: object()
    jobs = []
    runtime.lifecycle.spawn = lambda job: jobs.append(job)  # задачи прогрева — под рукой

    runtime.warm_sound_cache()
    await runtime.voice_stack.autostart()
    for job in jobs:
        await job
    assert started == [{"id": "asr-pkg"}]  # TTS-пакет не поднимается, ASR — да
    assert warmed == []  # предзапекание кеша не запускается

    runtime.settings["voice_stack"]["speak"] = True
    started.clear()
    jobs.clear()
    runtime.warm_sound_cache()
    await runtime.voice_stack.autostart()
    for job in jobs:
        await job
    assert started == [{"id": "asr-pkg"}, {"id": "tts-pkg"}]
    assert warmed  # озвучка включена — кеш прогревается
