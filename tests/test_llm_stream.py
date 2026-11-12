"""Стриминг ответа LLM (предпрогрев озвучки первой фразы): дельты адаптера, колбэк
хода, потребление в model_reply. Провод станции не меняется — аудио реплики целиком."""
import asyncio
import json
import struct
import time

import pytest

from aiohttp import ClientSession, web

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from server_emulator.voice_stack.adapters import openai
from tests.conftest import listen, page, types

TTS_OGG = (page(2, b'OpusHead' + bytes([1, 1]) + struct.pack('<HIhB', 312, 16000, 0, 0), 0) +
           page(4, b'\xf8\xff\xfe', 1))


def sse_app(chunks, calls):
    """Фейковый OpenAI-совместимый сервер: отдаёт chunks как SSE-поток."""

    async def chat(request):
        calls.append(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for chunk in chunks:
            await response.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app


def stream_chunks(parts, usage):
    """Кадры SSE: дельты по частям + финальный usage."""
    chunks = [{"id": "c1", "object": "chat.completion.chunk", "model": "m",
               "choices": [{"index": 0, "delta": {"role": "assistant", "content": parts[0]}, "finish_reason": None}]}]
    for part in parts[1:]:
        chunks.append({"id": "c1", "object": "chat.completion.chunk", "model": "m",
                       "choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}]})
    chunks.append({"id": "c1", "object": "chat.completion.chunk", "model": "m",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    chunks.append({"id": "c1", "object": "chat.completion.chunk", "model": "m", "choices": [], "usage": usage})
    return chunks


async def test_complete_stream_deltas_and_assembled():
    """complete_stream: дельты текста, собранный ответ с usage; stream на проводе."""
    calls = []
    runner = web.AppRunner(sse_app(stream_chunks(["Секундочку", ", думаю.", " Готово"],
                                                 {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}),
                                   calls))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"

    class L:
        def client_session(self):
            return None  # стриминг litellm общий клиент не использует

    deltas, result = [], None
    try:
        async for item in openai.complete_stream(type("R", (), {"lifecycle": L()})(),
                                                 {"url": base + "/v1/chat/completions", "api_key": "k", "model": "m"},
                                                 {"model": "m", "messages": [{"role": "user", "content": "привет"}]}):
            if "delta" in item:
                deltas.append(item["delta"])
            else:
                result = item["result"]
    finally:
        await runner.cleanup()
    assert deltas == ["Секундочку", ", думаю.", " Готово"]
    assert result["choices"][0]["message"]["content"] == "Секундочку, думаю. Готово"
    assert result["usage"]["total_tokens"] == 13
    assert calls[0]["stream"] is True and calls[0]["stream_options"] == {"include_usage": True}


async def test_stream_provider_error_is_immediate():
    """Ошибка провайдера в стриме → ValueError сразу (num_retries=0 и в стриме).
    Импорт litellm — разовый прогрев вне пути ошибки; сам путь ограничен по времени."""
    from server_emulator.voice_stack.adapters import _litellm
    _litellm.prepare()
    hits = []

    async def fail(request):
        hits.append(1)
        return web.json_response({"error": {"message": "gone"}}, status=404)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", fail)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"

    class L:
        def client_session(self):
            return None

    try:
        started = time.monotonic()
        with pytest.raises(ValueError, match="HTTP 404"):
            async for _item in openai.complete_stream(type("R", (), {"lifecycle": L()})(),
                                                      {"url": base + "/v1/chat/completions", "api_key": "k", "model": "m"},
                                                      {"model": "m", "messages": [{"role": "user", "content": "x"}]}):
                pass
        assert time.monotonic() - started < 5  # ответ провайдера мгновенный — и ошибка тоже
        assert len(hits) == 1  # ретраев нет — провайдер опрошен ровно один раз
    finally:
        await runner.cleanup()


async def test_legacy_first_sentence_callback(tmp_path):
    """Ход вызывает колбэк первой фразы (spoken-форма), ответ собирается целиком;
    tool-запросы и usage не теряются."""
    runtime = Runtime(tmp_path)
    calls = []
    runner = web.AppRunner(sse_app(stream_chunks(
        ["Сейчас подумаю. ", "Включаю", " свет."],
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}), calls))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    runtime.settings["model"].update(url=base + "/v1/chat/completions", model="m")
    runtime.settings["agent"]["engine"] = "legacy"  # стриминговый колбэк — legacy-цикл
    fired = []
    try:
        result = await runtime.chat("включи свет", first_sentence=fired.append)
    finally:
        await runner.cleanup()
        await runtime.close()
    assert fired and fired[0].startswith("Сейчас подумаю.")
    assert result["text"] == "Сейчас подумаю. Включаю свет."
    assert result["usage"]["total_tokens"] == 15


async def test_no_stream_without_url(tmp_path):
    """Без настроенного URL ход идёт через runtime.provider (заглушки тестов);
    колбэк не вызывается (условие 1: заглушки без complete_stream работают как раньше)."""
    runtime = Runtime(tmp_path)
    calls = []

    async def provider(kind, payload=None, audio=None, filename=None):
        calls.append(kind)
        return b'{"choices":[{"message":{"content":"stub"}}],"usage":{"total_tokens": 1}}', "application/json"

    runtime.provider = provider
    fired = []
    try:
        result = await runtime.chat("привет", first_sentence=fired.append)
    finally:
        await runtime.close()
    assert result["text"] == "stub" and calls == ["model"]
    assert fired == []


async def test_prefetch_starts_during_model_stream(tmp_path):
    """Предпрогрев: синтез первой фразы начинается, пока модель ещё стримит
    (латентность «модель+фраза» вместо «модель+весь TTS»); модель тоже с задержкой."""
    runtime = Runtime(tmp_path)
    tts_calls = []
    model_done = {"ts": None}

    async def chat(request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)

        async def emit(chunk, delay):
            await asyncio.sleep(delay)
            await response.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        # три фразы по 0,3 с — модель стримит ~0,9 с
        await emit({"id": "c", "object": "chat.completion.chunk", "model": "m",
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Первая фраза готова."}, "finish_reason": None}]}, 0.3)
        await emit({"id": "c", "object": "chat.completion.chunk", "model": "m",
                    "choices": [{"index": 0, "delta": {"content": " Вторая тоже."}, "finish_reason": None}]}, 0.3)
        await emit({"id": "c", "object": "chat.completion.chunk", "model": "m",
                    "choices": [{"index": 0, "delta": {"content": " Третья завершает."}, "finish_reason": None}]}, 0.3)
        await emit({"id": "c", "object": "chat.completion.chunk", "model": "m",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}, 0.0)
        model_done["ts"] = time.monotonic()
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    async def tts(request):
        body = await request.json()
        tts_calls.append({"input": body["input"], "ts": time.monotonic()})
        await asyncio.sleep(0.2)
        return web.Response(body=TTS_OGG, content_type="audio/ogg")

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_post("/tts", tts)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    runtime.settings["model"].update(url=base + "/v1/chat/completions", model="m")
    runtime.settings["tts"].update(url=base + "/tts", model="t", voice="v", response_format="wav")
    runtime.settings["aapi_tts"] = True

    # лёгкая заглушка Conversation: _prefetch_speech использует только runtime
    from types import SimpleNamespace
    from server_emulator.voice import Conversation
    conv = SimpleNamespace(runtime=runtime, pending_speech=None)
    conv._prefetch_speech = lambda spoken: Conversation._prefetch_speech(conv, spoken)
    fired = []
    first_audio_ready = {"ts": None}

    def first_sentence(spoken):
        if conv.pending_speech is None:
            fired.append(spoken)
            started = time.monotonic()

            async def synth():
                conv.pending_speech["audio"] = await conv._prefetch_speech(spoken)
                first_audio_ready["ts"] = time.monotonic()
            conv.pending_speech = {"prefix": spoken, "task": asyncio.create_task(synth()), "started": started}

    payload = {"model": "m", "temperature": 0.3,
               "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "включи"}]}
    from server_emulator.provider_manager import ProviderManager
    try:
        result = await ProviderManager(runtime)._chat_legacy(runtime, "включи", [], mcp=False,
                                                             first_sentence=first_sentence)
        # подождать предпрогрев как model_reply
        pending = conv.pending_speech
        first_pages = await pending["task"]
        rest_started = time.monotonic()
        assert result["text"].startswith(fired[0]) and fired
        # первая фраза ушла в TTS до конца стрима модели
        assert tts_calls and tts_calls[0]["ts"] < model_done["ts"]
        assert tts_calls[0]["input"] == fired[0]
        # предпрогрев готов раньше конца модели (первый звук — раньше прежнего пути)
        assert first_audio_ready["ts"] < model_done["ts"]
    finally:
        await runner.cleanup()
        await runtime.close()
