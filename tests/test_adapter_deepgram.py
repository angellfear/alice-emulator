"""Адаптер Deepgram на заглушке API: потоковый WebSocket (частичные результаты, пауза, CloseStream) и REST."""
import asyncio
import io
import json
import logging
import struct
import wave

import pytest
from aiohttp import ClientSession, WSMsgType, web

from server_emulator.voice_stack import adapters, validate_fields
from server_emulator.voice_stack.adapters import deepgram

TOKEN = "test-token"
FORMAT = "audio/x-pcm;bit=16;rate=16000;channels=1"


def tone(rate=16000, seconds=0.3):
    return b"".join(struct.pack("<h", int(8000 * (1 if (i // 40) % 2 else -1))) for i in range(int(rate * seconds)))


def wav_of(pcm, rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def result(text, final=False, speech_final=False):
    return {"type": "Results", "is_final": final, "speech_final": speech_final,
            "channel": {"alternatives": [{"transcript": text}]}}


class Lifecycle:
    def __init__(self, session):
        self.session = session

    def client_session(self):
        return self.session


class Runtime:
    def __init__(self, session):
        self.lifecycle = Lifecycle(session)


@pytest.fixture
async def api():
    seen = {"ws": [], "rest": [], "closed": 0}

    def guard(request):
        token = request.headers.get("Authorization", "")
        if token == "Token bad":
            return web.json_response({"err_code": "INVALID_AUTH", "err_msg": "Invalid credentials"}, status=401)
        if token == "Token limit":
            return web.json_response({"err_msg": "rate"}, status=429)
        if token == "Token nocredit":
            return web.json_response({"err_msg": "Insufficient credits"}, status=402)
        return None

    async def listen_ws(request):
        failure = guard(request)
        if failure:
            return failure
        scenario = request.query.get("scenario", "pause")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        call = {"query": dict(request.query), "auth": request.headers["Authorization"], "bytes": 0, "texts": []}
        seen["ws"].append(call)
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                call["bytes"] += len(message.data)
                if call["bytes"] == len(message.data):
                    await ws.send_json(result("включи"))
                if scenario == "pause" and call["bytes"] >= 6400:
                    await ws.send_json(result("включи", final=True))
                    await ws.send_json(result("свет", final=False))
                    await ws.send_json(result("свет в кухне", final=True, speech_final=True))
            elif message.type == WSMsgType.TEXT:
                call["texts"].append(json.loads(message.data))
                if scenario == "close":
                    await ws.send_json(result("включи", final=True))
                    await ws.send_json(result("свет", final=True))
                    await ws.send_json({"type": "Metadata", "request_id": "1"})
                    await ws.close()
        seen["closed"] += 1
        return ws

    async def listen_rest(request):
        failure = guard(request)
        if failure:
            return failure
        seen["rest"].append({"query": dict(request.query), "content_type": request.content_type,
                             "auth": request.headers["Authorization"], "body": await request.read()})
        return web.json_response({"results": {"channels": [{"alternatives": [{"transcript": "Включи свет."}]}]}})

    app = web.Application()
    app.router.add_get("/v1/listen", listen_ws)
    app.router.add_post("/v1/listen", listen_rest)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}/v1/listen"
    async with ClientSession() as session:
        yield Runtime(session), base, seen
    await runner.cleanup()


def test_adapter_registered_and_fields():
    assert adapters.get("deepgram") is deepgram
    spec = deepgram.SERVICES["asr"]
    validate_fields(spec["fields"])
    assert deepgram.TITLE.endswith("(Online Free)") and spec["title"].endswith("(Online Free)")
    fields = {f["key"]: f for f in spec["fields"]}
    assert fields["api_key"]["type"] == "password" and fields["api_key"]["link"] and fields["api_key"]["required"]
    assert [o["value"] for o in fields["model"]["options"]] == ["nova-3", "nova-2"]
    for key in ("smart_format", "punctuate", "endpointing", "url"):
        assert fields[key]["group"] == "advanced"
    assert spec["defaults"]["language"] == "ru" and not hasattr(deepgram, "speech")


async def test_stream_returns_on_speech_pause_without_closing_microphone(api):
    runtime, base, seen = api
    microphone = asyncio.Event()

    async def chunks():
        for _ in range(4):
            yield tone()  # 9600 байт
        await microphone.wait()  # микрофон устройства ещё открыт

    partials = []

    async def partial(text):
        partials.append(text)

    config = {"api_key": TOKEN, "url": base, "model": "nova-3", "language": "ru", "smart_format": "yes",
              "punctuate": "no", "endpointing": 400}
    answer = await asyncio.wait_for(deepgram.recognize_stream(runtime, config, chunks(), FORMAT, partial), 5)
    assert answer == {"text": "включи свет в кухне"}
    assert partials[0] == "включи" and partials[-1] == "включи свет в кухне"
    call = seen["ws"][0]
    assert call["auth"] == "Token " + TOKEN
    assert call["query"] == {"model": "nova-3", "language": "ru", "smart_format": "true", "punctuate": "false",
                             "encoding": "linear16", "sample_rate": "16000", "channels": "1", "interim_results": "true",
                             "endpointing": "400"}
    assert call["bytes"] >= 6400 and TOKEN not in json.dumps(call["query"])
    await asyncio.sleep(0.05)
    assert seen["closed"] == 1  # соединение закрыто, отправка звука остановлена


async def test_stream_collects_finals_after_close_stream(api):
    runtime, base, seen = api

    async def chunks():
        yield tone()

    config = {"api_key": TOKEN, "url": base + "?scenario=close"}
    answer = await asyncio.wait_for(deepgram.recognize_stream(runtime, config, chunks(), FORMAT, None), 5)
    assert answer == {"text": "включи свет"}
    assert seen["ws"][0]["texts"] == [{"type": "CloseStream"}]
    assert seen["ws"][0]["query"]["model"] == "nova-3" and seen["ws"][0]["query"]["language"] == "ru"


async def test_stream_errors_are_russian_and_keep_key_out_of_logs(api, caplog):
    runtime, base, seen = api
    caplog.set_level(logging.DEBUG)

    async def chunks():
        yield tone()

    for key, text in (("bad", "Неверный ключ"), ("limit", "лимит"), ("nocredit", "Недостаточно")):
        with pytest.raises(ValueError, match=text):
            await deepgram.recognize_stream(runtime, {"api_key": key, "url": base}, chunks(), FORMAT, None)
    with pytest.raises(ValueError, match="введите ключ"):
        await deepgram.recognize_stream(runtime, {"api_key": "", "url": base}, chunks(), FORMAT, None)
    assert TOKEN not in caplog.text


async def test_rest_transcription(api):
    runtime, base, seen = api
    config = {"api_key": TOKEN, "url": base, "model": "nova-3", "language": "ru", "smart_format": "yes", "punctuate": "yes"}
    audio = wav_of(tone())
    assert await deepgram.transcribe(runtime, config, audio, "input.wav") == "Включи свет."
    call = seen["rest"][0]
    assert call["auth"] == "Token " + TOKEN and call["content_type"] == "audio/wav" and call["body"] == audio
    assert call["query"] == {"model": "nova-3", "language": "ru", "smart_format": "true", "punctuate": "true"}
    await deepgram.transcribe(runtime, config, b"OggS" + b"\0" * 30, "input.ogg")
    assert seen["rest"][1]["content_type"] == "audio/ogg"
    with pytest.raises(ValueError, match="Неверный ключ"):
        await deepgram.transcribe(runtime, {**config, "api_key": "bad"}, audio, "input.wav")
    with pytest.raises(ValueError, match="лимит"):
        await deepgram.transcribe(runtime, {**config, "api_key": "limit"}, audio, "input.wav")
