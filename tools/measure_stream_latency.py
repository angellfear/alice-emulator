"""Замер латентности «первое аудио готово»: стрим против прежнего пути.

Модель 1 с до полного ответа (стрим по третям), TTS 0,5 с на вызов.
Заглушки на локальных серверах, без внешней сети. Числа для «Итог» задания 123.
"""
import asyncio
import json
import struct
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web

from server_emulator.provider_manager import ProviderManager
from server_emulator.runtime import Runtime
from tests.conftest import page

MODEL_TEXT = "Первая фраза готова. Вторая тоже готова. Третья завершает."


def tts_ogg():
    return (page(2, b'OpusHead' + bytes([1, 1]) + struct.pack('<HIhB', 312, 16000, 0, 0), 0) +
            page(4, b'\xf8\xff\xfe', 1))


async def chat_stream(request):
    """Стриминговый сервер: 3 фразы по 1/3 секунды."""
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    for part in ("Первая фраза готова.", " Вторая тоже готова.", " Третья завершает."):
        await asyncio.sleep(1 / 3)
        chunk = {"id": "c", "object": "chat.completion.chunk", "model": "m",
                 "choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}]}
        await response.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
    chunk = {"id": "c", "object": "chat.completion.chunk", "model": "m",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    await response.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
    await response.write(b"data: [DONE]\n\n")
    await response.write_eof()
    return response


async def chat_plain(request):
    """Прежний путь: полный ответ через 1 секунду."""
    await asyncio.sleep(1.0)
    return web.json_response({"choices": [{"message": {"content": MODEL_TEXT}}]})


async def tts(request):
    await request.json()
    await asyncio.sleep(0.5)
    return web.Response(body=tts_ogg(), content_type="audio/ogg")


async def scenario(base, tts_url, streaming):
    runtime = Runtime(Path(tempfile.mkdtemp()))
    runtime.settings["model"].update(url=base + "/v1/chat/completions", model="m")
    runtime.settings["tts"].update(url=tts_url, model="t", voice="v", response_format="opus")
    pm = ProviderManager(runtime)
    pending = {}

    def first_sentence(spoken):
        if "task" not in pending:
            pending["ts"] = time.monotonic()

            async def synth():
                from server_emulator.audio_buffer import synthesize
                pages, speech = await synthesize(runtime, spoken)
                if speech:  # потоковый движок TTS: сливаем фразу в страницы
                    async for p in speech:
                        pages.append(p)
                return pages
            pending["task"] = asyncio.create_task(synth())

    started = time.monotonic()
    await pm._chat_legacy(runtime, "включи свет", [], mcp=False,
                          first_sentence=first_sentence if streaming else None)
    if streaming:
        first_pages = await pending["task"]
        first_ready = time.monotonic()
        first_len = len(first_pages or [])
    else:
        await asyncio.sleep(0.5)  # прежний путь: весь текст одним вызовом TTS
        first_ready = time.monotonic()
        first_len = None
    await runtime.close()
    return started, pending.get("ts"), first_ready, first_len


async def main():
    app = web.Application()
    app.router.add_post("/stream/v1/chat/completions", chat_stream)
    app.router.add_post("/plain/v1/chat/completions", chat_plain)
    app.router.add_post("/tts", tts)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"

    # прогрев: импорт litellm до замера (оба пути)
    warm = Runtime(Path(tempfile.mkdtemp()))
    warm.settings["model"].update(url=base + "/plain/v1/chat/completions", model="m")
    warm.settings["tts"].update(url=base + "/tts", model="t", voice="v", response_format="opus")
    await ProviderManager(warm)._chat_legacy(warm, "грей", [], mcp=False)
    await warm.close()

    s0, ts, first, _ = await scenario(base + "/plain", base + "/tts", streaming=False)
    print(f"БЕЗ стрима: аудио реплики готово через {first - s0:.2f} с (модель 1,0 + TTS 0,5)")
    s0, ts, first, plen = await scenario(base + "/stream", base + "/tts", streaming=True)
    print(f"СО стримом: фраза распознана на {ts - s0:.2f} с, её аудио готово на {first - s0:.2f} с ({plen} стр.)")
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
