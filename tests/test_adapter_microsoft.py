"""Адаптер Microsoft (Edge TTS, Azure Speech) на заглушках: REST Azure и WebSocket Edge без интернета."""
import io
import logging
import re
import struct
import wave

import pytest
from aiohttp import ClientSession, WSMsgType, web

from server_emulator.audio_buffer import ogg_pages
from server_emulator.voice_stack import adapters, validate_fields
from server_emulator.voice_stack.adapters import microsoft

TOKEN = "test-token"


def tone(rate=24000, seconds=0.3):
    return b"".join(struct.pack("<h", int(8000 * (1 if (i // 40) % 2 else -1))) for i in range(int(rate * seconds)))


def wav_of(pcm, rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


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
    seen = {"tts": [], "stt": [], "edge": [], "list_calls": 0}

    def guard(request):
        key = request.headers.get("Ocp-Apim-Subscription-Key", "")
        if key == "bad":
            return web.json_response({"error": {"message": "Access denied due to invalid subscription key"}}, status=401)
        if key == "limit":
            return web.Response(status=429, text="too many")
        return None

    async def tts(request):
        failure = guard(request)
        if failure:
            return failure
        seen["tts"].append({"headers": dict(request.headers), "body": (await request.read()).decode()})
        response = web.StreamResponse()
        await response.prepare(request)
        pcm = tone()
        for pos in range(0, len(pcm), 3001):  # нечётные границы — конвейер обязан их собрать
            await response.write(pcm[pos:pos + 3001])
        await response.write_eof()
        return response

    async def listing(request):
        failure = guard(request)
        if failure:
            return failure
        seen["list_calls"] += 1
        return web.json_response([
            {"ShortName": "en-US-JennyNeural", "Locale": "en-US", "Gender": "Female", "LocalName": "Jenny"},
            {"ShortName": "ru-RU-DmitryNeural", "Locale": "ru-RU", "Gender": "Male", "LocalName": "Дмитрий"},
            {"ShortName": "en-US-AvaMultilingualNeural", "Locale": "en-US", "Gender": "Female", "LocalName": "Ava",
             "SecondaryLocaleList": ["ru-RU", "de-DE"]},
            {"ShortName": "ru-RU-SvetlanaNeural", "Locale": "ru-RU", "Gender": "Female", "LocalName": "Светлана"}])

    async def stt(request):
        failure = guard(request)
        if failure:
            return failure
        body = await request.read()
        seen["stt"].append({"query": dict(request.query), "headers": dict(request.headers), "body": body})
        status = {"empty": "NoMatch"}.get(request.query.get("language"), "Success")
        return web.json_response({"RecognitionStatus": status, "DisplayText": "Включи свет."})

    async def edge(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        seen["edge"].append({"query": dict(request.query), "headers": dict(request.headers)})
        texts = []
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                texts.append(message.data)
                if "Path:ssml" in message.data:
                    seen["edge"][-1]["messages"] = texts
                    await ws.send_str("X-RequestId:1\r\nContent-Type:text/plain\r\nPath:turn.start\r\n\r\n{}")
                    pcm = tone()
                    head = b"X-RequestId:1\r\nContent-Type:audio/pcm\r\nPath:audio\r\n"
                    for pos in range(0, len(pcm), 4000):
                        await ws.send_bytes(struct.pack(">H", len(head)) + head + pcm[pos:pos + 4000])
                    other = b"X-RequestId:1\r\nPath:audio.metadata\r\n"
                    await ws.send_bytes(struct.pack(">H", len(other)) + other + b"zzz")
                    await ws.send_str("X-RequestId:1\r\nContent-Type:text/plain\r\nPath:turn.end\r\n\r\n{}")
                    await ws.close()
        return ws

    async def edge_denied(request):
        return web.Response(status=403)

    async def edge_silent(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.type == WSMsgType.TEXT and "Path:ssml" in message.data:
                await ws.send_str("Path:turn.end\r\n\r\n{}")
        return ws

    app = web.Application()
    app.router.add_post("/cognitiveservices/v1", tts)
    app.router.add_get("/cognitiveservices/voices/list", listing)
    app.router.add_post("/stt", stt)
    app.router.add_get("/edge", edge)
    app.router.add_get("/edge403", edge_denied)
    app.router.add_get("/edge-silent", edge_silent)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"127.0.0.1:{runner.addresses[0][1]}"
    async with ClientSession() as session:
        yield Runtime(session), base, seen
    await runner.cleanup()


def test_adapter_registered_with_service_labels():
    assert adapters.get("microsoft") is microsoft
    for service in ("tts", "asr"):
        spec = microsoft.SERVICES[service]
        validate_fields(spec["fields"])
        assert spec["title"].endswith("(Online Free)")
    assert set(microsoft.SERVICES["tts"]["examples"]) == {
        "Edge TTS (Online Free)", "Azure Speech F0 (Online Free)", "Azure Speech S0 (Online)"}
    assert "неофициальный" in microsoft.SERVICES["tts"]["description"]
    fields = {f["key"]: f for f in microsoft.SERVICES["tts"]["fields"]}
    assert fields["api_key"]["type"] == "password" and fields["api_key"]["link"] and fields["region"]["help"]
    assert fields["voice"]["options_from"] == "voices" and fields["style"]["group"] == "advanced"
    assert [o["value"] for o in fields["voice"]["options"]] == [
        "ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural", "ru-RU-DariyaNeural"]


async def test_azure_tts_request_and_ogg(api):
    runtime, base, seen = api
    config = {"engine": "azure", "api_key": TOKEN, "url": f"http://{base}/cognitiveservices/v1", "region": "westeurope",
              "voice": "ru-RU-DmitryNeural", "style": "cheerful", "rate": 10, "pitch": -2, "sample_rate": "24000"}
    data, content_type = await microsoft.speech(runtime, config, {"input": "Привет <мир> & я"})
    assert content_type == "audio/ogg" and ogg_pages(data)
    request = seen["tts"][0]
    assert request["headers"]["Ocp-Apim-Subscription-Key"] == TOKEN
    assert request["headers"]["Content-Type"] == "application/ssml+xml"
    assert request["headers"]["X-Microsoft-OutputFormat"] == "raw-24khz-16bit-mono-pcm"
    assert request["headers"]["User-Agent"]
    body = request["body"]
    assert "name='ru-RU-DmitryNeural'" in body and "xml:lang='ru-RU'" in body
    assert "Привет &lt;мир&gt; &amp; я" in body
    assert "<prosody rate='+10%' pitch='-2Hz'>" in body and "<mstts:express-as style='cheerful'>" in body


async def test_azure_stream_cache_pcm_and_default_region_address(api):
    runtime, base, seen = api
    config = {"engine": "azure", "api_key": TOKEN, "url": f"http://{base}/cognitiveservices/v1", "voice": "ru-RU-SvetlanaNeural",
              "sample_rate": "16000"}
    assert microsoft.streams(config)
    pages = [page async for page in microsoft.speech_stream(runtime, config, "Привет")]
    assert pages[0][:4] == b"OggS"
    assert seen["tts"][0]["headers"]["X-Microsoft-OutputFormat"] == "raw-16khz-16bit-mono-pcm"
    pcm, rate = await microsoft.synth_pcm(runtime, config, "Привет")
    assert rate == 16000 and pcm.dtype.name == "float32" and pcm.size == len(tone()) // 2
    assert microsoft._tts_url({"region": "westeurope"}) == "https://westeurope.tts.speech.microsoft.com/cognitiveservices/v1"
    with pytest.raises(ValueError, match="регион"):
        microsoft._tts_url({"region": "evil/host"})


async def test_azure_errors_do_not_leak_key(api, caplog):
    runtime, base, seen = api
    caplog.set_level(logging.DEBUG)
    config = {"engine": "azure", "url": f"http://{base}/cognitiveservices/v1", "voice": "ru-RU-SvetlanaNeural"}
    with pytest.raises(ValueError, match="Неверный ключ"):
        await microsoft.speech(runtime, {**config, "api_key": "bad"}, {"input": "x"})
    with pytest.raises(ValueError, match="лимит"):
        await microsoft.speech(runtime, {**config, "api_key": "limit"}, {"input": "x"})
    with pytest.raises(ValueError, match="введите ключ"):
        await microsoft.speech(runtime, {**config, "api_key": ""}, {"input": "x"})
    with pytest.raises(ValueError, match="имя голоса"):
        await microsoft.speech(runtime, {**config, "api_key": TOKEN, "voice": "x'><evil"}, {"input": "x"})
    assert TOKEN not in caplog.text


async def test_azure_voices_ru_only_cached(api):
    runtime, base, seen = api
    config = {"engine": "azure", "api_key": "list-token", "url": f"http://{base}/cognitiveservices/v1"}
    items = await microsoft.voices(runtime, config)
    assert [i["value"] for i in items] == ["ru-RU-DmitryNeural", "ru-RU-SvetlanaNeural", "en-US-AvaMultilingualNeural"]
    assert items[0]["gender"] == "male" and "многоязычный" in items[2]["label"]
    await microsoft.voices(runtime, config)
    assert seen["list_calls"] == 1
    await microsoft.voices(runtime, {**config, "refresh": True})
    assert seen["list_calls"] == 2
    assert await microsoft.voices(runtime, {"engine": "azure", "api_key": ""}) == microsoft.OPTIONS
    assert await microsoft.voices(runtime, {"engine": "edge", "api_key": "x"}) == microsoft.OPTIONS


async def test_edge_tts_without_key_over_websocket(api):
    runtime, base, seen = api
    config = {"engine": "edge", "url": f"ws://{base}/edge", "voice": "ru-RU-SvetlanaNeural", "rate": -5, "style": "cheerful"}
    data, _ = await microsoft.speech(runtime, config, {"input": "Привет"})
    assert ogg_pages(data)
    call = seen["edge"][0]
    assert "Origin" in call["headers"] and "Ocp-Apim-Subscription-Key" not in call["headers"]
    config_message, ssml_message = call["messages"]
    assert "Path:speech.config" in config_message and "raw-24khz-16bit-mono-pcm" in config_message
    assert "Path:ssml" in ssml_message and "Content-Type:application/ssml+xml" in ssml_message
    assert "name='ru-RU-SvetlanaNeural'" in ssml_message and "rate='-5%'" in ssml_message
    assert "express-as" not in ssml_message  # стили — только у Azure
    pages = [page async for page in microsoft.speech_stream(runtime, config, "Привет")]
    assert pages and pages[0][:4] == b"OggS"
    pcm, rate = await microsoft.synth_pcm(runtime, config, "Привет")
    assert rate == 24000 and pcm.size == len(tone()) // 2  # только кадры Path:audio


async def test_edge_default_address_and_failures(api):
    runtime, base, seen = api
    address = microsoft._edge_address({})
    assert address.startswith(microsoft.EDGE_URL) and "TrustedClientToken=" in address
    assert re.search(r"Sec-MS-GEC=[0-9A-F]{64}", address)
    with pytest.raises(ValueError, match="HTTP 403"):
        await microsoft.speech(runtime, {"engine": "edge", "url": f"ws://{base}/edge403"}, {"input": "x"})
    with pytest.raises(ValueError, match="не вернула звук"):
        await microsoft.speech(runtime, {"engine": "edge", "url": f"ws://{base}/edge-silent"}, {"input": "x"})


async def test_azure_asr_short_audio(api):
    runtime, base, seen = api
    config = {"api_key": TOKEN, "url": f"http://{base}/stt", "language": "ru-RU", "profanity": "masked"}
    text = await microsoft.transcribe(runtime, config, wav_of(tone(16000)), "input.wav")
    assert text == "Включи свет."
    call = seen["stt"][0]
    assert call["query"] == {"language": "ru-RU", "format": "simple", "profanity": "masked"}
    assert call["headers"]["Ocp-Apim-Subscription-Key"] == TOKEN
    assert call["headers"]["Content-Type"] == "audio/wav; codecs=audio/pcm; samplerate=16000"
    assert call["body"][:4] == b"RIFF"
    assert await microsoft.transcribe(runtime, {**config, "language": "empty"}, wav_of(tone(16000)), "i.wav") == ""
    with pytest.raises(ValueError, match="Неверный ключ"):
        await microsoft.transcribe(runtime, {**config, "api_key": "bad"}, wav_of(tone(16000)), "i.wav")
    with pytest.raises(ValueError, match="WAV и Ogg"):
        await microsoft.transcribe(runtime, config, b"junk", "i.bin")
