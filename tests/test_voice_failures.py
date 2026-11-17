"""Сбои конвейера видны и слышны (voice.failure_text/failure_speech), текст для синтеза без
разметки (spoken.speakable), раздел «AI агент» звукового кеша. Сбой потокового распознавания —
реплика сбоя этапа «asr», а не закрытие соединения колонки; сбой озвучки — строка в журнале."""
import asyncio
import json
import struct

import numpy as np

from server_emulator.native_audio import encode_opus
from server_emulator.runtime import Runtime
from server_emulator.spoken import speakable
from server_emulator.voice import Conversation
from tests.conftest import types


async def make_ogg(seconds=0.3):
    async def pcm():
        yield (0.2 * np.sin(np.arange(int(48000 * seconds)) / 48000 * 2 * np.pi * 440)).astype(np.float32)
    return b"".join([page async for page in encode_opus(pcm())])


async def failing_run(tmp_path, model_error=None, tts_error=None, tts_delay=0):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, aapi_tts=True, tts_wait_s=0.2)
    runtime.settings["model"]["stub_text"] = ""
    runtime.settings["agent"]["async_reply_s"] = 0  # отложенный ответ — tests/test_async_reply.py
    ogg, tts = await make_ogg(), []

    async def provider(kind, payload=None, **kwargs):
        if kind == "tts":
            if tts_error:
                raise tts_error
            tts.append(payload["input"])
            await asyncio.sleep(tts_delay)
            return ogg, "audio/ogg"
        if model_error:
            raise model_error
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
    await runtime.server_log.flush()
    journal = [item["message"] for item in (await runtime.server_log.query(source="ws"))["items"]]
    await runtime.close()
    responses = [server.FromString(raw[4:]).Event.AliceResponse for raw in sent
                 if raw.startswith(b"AAPI") and server.FromString(raw[4:]).Event.WhichOneof("Event") == "AliceResponse"]
    return responses[-1], tts, journal


async def test_model_failure_is_shown_and_spoken(tmp_path):
    response, tts, _ = await failing_run(tmp_path, model_error=ValueError("model: HTTP 502 от провайдера"))
    text = response.Response.Cards[0].Text
    assert text.startswith("Не получилось получить ответ модели") and "502" in text
    # голос — та же фраза без кода (конечный набор, озвучка из звукового кеша)
    assert response.VoiceResponse.HasVoiceResponse and tts == [text.replace(" 502", "")]


async def test_tts_failure_explained_on_card(tmp_path):
    response, tts, _ = await failing_run(tmp_path, tts_error=ValueError("TTS HTTP 500"))
    assert [c.Text for c in response.Response.Cards][0] == "Готово."
    assert response.Response.Cards[1].Text.startswith("Озвучка недоступна") and not response.VoiceResponse.HasVoiceResponse


async def test_slow_tts_is_card_without_voice(tmp_path):
    response, tts, _ = await failing_run(tmp_path, tts_delay=5)
    assert response.Response.Cards[0].Text == "Готово." and not response.VoiceResponse.HasVoiceResponse
    assert response.Response.Cards[1].Text.startswith("Озвучка недоступна: сервис не ответил вовремя")


async def test_tts_failure_is_in_the_server_journal(tmp_path):
    """Молчание колонки объяснимо в журнале сервера: сбой синтеза — строка source `ws` с причиной,
    а не только `runtime.recent` и трасса хода."""
    response, tts, journal = await failing_run(tmp_path, tts_error=ValueError("TTS HTTP 500"))
    assert not response.VoiceResponse.HasVoiceResponse
    assert any("Озвучка не удалась" in message and "TTS HTTP 500" in message for message in journal)


async def test_streaming_asr_break_replies_instead_of_closing(tmp_path):
    """Потоковый ASR умер посреди фразы: кадр аудио не поднимает исключение из обработчика
    соединения (оно закрыло бы WS колонки кодом 1011), канал снимается, а ответ уходит обычной
    репликой сбоя этапа «asr» с закрытием канала."""
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_voice=True, aapi_tts=False)
    runtime.asr_streaming = lambda: True

    async def recognize_stream(chunks, fmt, on_partial=None, trace=None):
        raise OSError("connection closed")  # сервер распознавания оборвал поток
    runtime.recognize_stream = recognize_stream
    client, server = types()
    sent = []

    async def send(data):
        sent.append(data)
    conv = Conversation(runtime, server, send, trace_context={"sn": "SN-T"})
    incoming = client()
    incoming.Event.Header.MessageId = "voice"
    incoming.Event.Header.StreamId = 1
    incoming.Event.VoiceInput.Header.RequestId = "req"
    incoming.Event.VoiceInput.Format = "audio/x-pcm;bit=16;rate=16000;channels=1"
    try:
        await conv.receive(incoming, "VoiceInput")
        for _ in range(5):
            await asyncio.sleep(0)  # задача распознавания успевает упасть
        assert conv.channels[1]["asr_task"].done()
        await conv.receive_audio(struct.pack(">I", 1) + b"\0\0" * 16000)  # без исключения
        assert 1 not in conv.channels  # канал отдан run(), а не остался висеть
        await asyncio.wait_for(conv.task, 5)
        await conv.shutdown()
        await runtime.server_log.flush()
        journal = [item["message"] for item in (await runtime.server_log.query(source="ws"))["items"]]
    finally:
        await conv.shutdown()
        await runtime.close()
    responses = [server.FromString(raw[4:]).Event.AliceResponse for raw in sent
                 if raw.startswith(b"AAPI") and server.FromString(raw[4:]).Event.WhichOneof("Event") == "AliceResponse"]
    assert responses, "нет ответа колонке"
    assert responses[-1].Response.Cards[0].Text.startswith("Не получилось распознать речь")
    assert any("Потоковое распознавание прервалось" in message and "connection closed" in message
               for message in journal)


def test_speakable_drops_markdown_and_emoji():
    text = "\n\nПривет! 👋\n\nМогу:\n- Включить **свет**\n- Узнать погоду"
    assert speakable(text) == "Привет! Могу: Включить свет. Узнать погоду"


def test_agent_phrases_in_sound_cache(tmp_path):
    """Раздел «AI агент» звукового кеша: короткий ответ отложенного ответа и фразы сбоев (без кодов)."""
    from server_emulator.async_reply import WAIT_TEXT
    from server_emulator.tts_cache import static_phrases
    from server_emulator.voice import failure_phrases
    runtime = Runtime(tmp_path)
    agent = {t for t, e in static_phrases(runtime).items() if e["source"] == "agent"}
    assert WAIT_TEXT in agent and set(failure_phrases()) <= agent
    assert "Не получилось получить ответ модели: сервис ответил ошибкой. Подробности — в журнале эмулятора." in agent
    assert not any("Озвучка недоступна" in t for t in agent)


async def test_agent_phrases_warmed_into_cache(tmp_path):
    """Фразы раздела «AI агент» синтезируются заранее; уже готовые повторно не синтезируются."""
    from server_emulator.tts_cache import agent_phrases, engine_identity
    runtime = Runtime(tmp_path)
    calls = []

    class Engine:
        async def synth_pcm(self, text):
            calls.append(text)
            return np.full(2400, 0.1, dtype=np.float32), 24000
    engine = Engine()
    try:
        phrases = agent_phrases(runtime)
        assert await runtime.tts_cache.warm(engine, phrases) == len(phrases)
        assert all(runtime.tts_cache.cached(engine_identity(engine), t) for t in phrases)
        assert await runtime.tts_cache.warm(engine, phrases) == 0 and len(calls) == len(phrases)
    finally:
        await runtime.close()
