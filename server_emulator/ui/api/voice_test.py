"""Домены chat, stt и tts: проверка текстового диалога и голосовых провайдеров (RF-03).

Код перенесён из runtime.py (функция legacy_api) дословно; guard
(X-Emulator + same-origin) и маппинг ошибок (значение → 400,
недоступность провайдера → 502) обеспечивает диспетчер реестра
ui/api/__init__.py.
"""
import json

from aiohttp import web

from . import route


@route("chat", methods=("POST",))
async def chat(request, runtime, app):
    # graph: студия — свой тред агента (история между репликами, «сбросить» — thread нового имени);
    # вызовы, ждущие подтверждения, попадают в pending_calls — их решает tool-call
    value = await request.json()
    answer = await runtime.chat(value["text"], thread=value.get("thread") or "studio:panel")
    runtime.pending_calls = (runtime.pending_calls + [c for c in answer.get("tool_calls", []) if c.get("thread_id")])[-30:]
    answer.pop("scenario_messages", None)  # protobuf-сообщения сценариев — только голосовому тракту
    return web.json_response(answer)


@route("stt", methods=("POST",))
async def stt(request, runtime, app):
    audio = await request.read()
    if runtime.settings['stt'].get('url', '').endswith('/asr/stream'):
        fmt = 'audio/opus' if audio.startswith(b'OggS') else 'audio/wav'

        async def chunks():
            for offset in range(0, len(audio), 8192):
                yield audio[offset:offset + 8192]
        return web.json_response(await runtime.recognize_stream(chunks(), fmt))
    raw, _ = await runtime.provider("stt", audio=audio, filename=request.headers.get("X-Filename", "input.wav"))
    return web.json_response(json.loads(raw))


@route("tts", methods=("POST",))
async def tts(request, runtime, app):
    cfg = runtime.settings["tts"]
    if cfg.get('response_format') == 'pcm_f32le':
        text = (await request.json())['text']
        response = web.StreamResponse(headers={'Content-Type': 'audio/ogg', 'X-Content-Type-Options': 'nosniff'})
        source = runtime.speech_stream(text)
        try:
            first = await anext(source)
            await response.prepare(request)
            await response.write(first)
            async for page in source:
                await response.write(page)
            await response.write_eof()
            return response
        finally:
            await source.aclose()
    raw, content_type = await runtime.provider("tts", {"model": cfg["model"], "voice": cfg["voice"], "response_format": cfg["response_format"], "input": (await request.json())["text"]})
    return web.Response(body=raw, headers={"Content-Type": content_type, "X-Content-Type-Options": "nosniff"})
