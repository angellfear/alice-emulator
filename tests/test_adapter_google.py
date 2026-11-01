"""Адаптер Google (Gemini API, Cloud TTS) на заглушке API: запросы и ответы по документации поставщика."""
import base64
import io
import logging
import struct
import wave

import pytest
from aiohttp import ClientSession, web

from server_emulator.audio_buffer import ogg_pages
from server_emulator.voice_stack import adapters, validate_fields
from server_emulator.voice_stack.adapters import google

TOKEN = "test-token"


def tone(rate=24000, seconds=0.3):
    return b"".join(struct.pack("<h", int(8000 * (1 if (i // 40) % 2 else -1))) for i in range(int(rate * seconds)))


def wav_of(pcm, rate=24000):
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
    seen = {"requests": [], "voice_calls": 0}

    def guard(request, header):
        token = request.headers.get(header, "")
        token = token.removeprefix("Bearer ")
        seen["requests"].append({"path": request.path, "query": request.query_string, "token": token})
        if token == "bad":
            return web.json_response({"error": {"message": "API key not valid"}}, status=401)
        if token == "limit":
            return web.json_response({"error": {"message": "quota exceeded"}}, status=429)
        return None

    async def chat(request):
        failure = guard(request, "Authorization")
        if failure:
            return failure
        seen["chat"] = await request.json()
        return web.json_response({"choices": [{"message": {"role": "assistant", "content": "Привет"}}], "usage": {}})

    async def interactions(request):
        failure = guard(request, "x-goog-api-key")
        if failure:
            return failure
        body = await request.json()
        seen["interactions"] = body
        if "tts" in body["model"]:
            return web.json_response({"steps": [{"type": "model_output", "content": [
                {"type": "audio", "mime_type": "audio/l16;rate=24000", "data": base64.b64encode(tone()).decode()}]}]})
        return web.json_response({"steps": [{"type": "model_output", "content": [{"type": "text", "text": " включи свет "}]}]})

    async def synthesize(request):
        failure = guard(request, "X-Goog-Api-Key")
        if failure:
            return failure
        seen["cloud"] = await request.json()
        return web.json_response({"audioContent": base64.b64encode(wav_of(tone())).decode()})

    async def voices(request):
        failure = guard(request, "X-Goog-Api-Key")
        if failure:
            return failure
        seen["voice_calls"] += 1
        seen["voices_query"] = dict(request.query)
        return web.json_response({"voices": [
            {"name": "ru-RU-Standard-A", "languageCodes": ["ru-RU"], "ssmlGender": "FEMALE"},
            {"name": "ru-RU-Chirp3-HD-Puck", "languageCodes": ["ru-RU"], "ssmlGender": "MALE"},
            {"name": "ru-RU-Wavenet-B", "languageCodes": ["ru-RU"], "ssmlGender": "MALE"},
            {"name": "en-US-Neural2-A", "languageCodes": ["en-US"], "ssmlGender": "FEMALE"}]})

    app = web.Application()
    app.router.add_post("/openai/chat/completions", chat)
    app.router.add_post("/interactions", interactions)
    app.router.add_post("/v1/text:synthesize", synthesize)
    app.router.add_get("/v1/voices", voices)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    async with ClientSession() as session:
        yield Runtime(session), base, seen
    await runner.cleanup()


def test_adapter_is_registered_and_fields_valid():
    assert adapters.get("google") is google
    for service in ("llm", "asr", "tts"):
        spec = google.SERVICES[service]
        validate_fields(spec["fields"])
        assert spec["title"].endswith(("(Online Free)", "(Online)"))
        assert next(f for f in spec["fields"] if f["key"] == "api_key")["type"] == "password"
    assert google.SERVICES["tts"]["voice_modes"] == ["preset"]
    voice = next(f for f in google.SERVICES["tts"]["fields"] if f["key"] == "voice")
    assert voice["options_from"] == "voices" and len(voice["options"]) >= 30


async def test_llm_uses_openai_compatible_endpoint(api):
    runtime, base, seen = api
    config = {"api_key": TOKEN, "url": base + "/openai/chat/completions", "model": "gemini-3.8-flash",
              "temperature": 0.4, "reasoning_effort": "low"}
    payload = {"model": "ignored", "messages": [{"role": "user", "content": "Привет"}], "tools": [{"type": "function"}]}
    result = await google.complete(runtime, config, payload)
    assert result["choices"][0]["message"]["content"] == "Привет"
    assert seen["chat"]["model"] == "gemini-3.8-flash" and seen["chat"]["temperature"] == 0.4
    assert seen["chat"]["reasoning_effort"] == "low" and seen["chat"]["tools"] == [{"type": "function"}]
    assert seen["requests"][0]["token"] == TOKEN and TOKEN not in seen["requests"][0]["query"]


async def test_errors_are_russian_and_do_not_leak_key(api, caplog):
    runtime, base, seen = api
    caplog.set_level(logging.DEBUG)
    config = {"url": base + "/openai/chat/completions", "model": "m"}
    with pytest.raises(ValueError, match="Неверный ключ") as info:
        await google.complete(runtime, {**config, "api_key": "bad"}, {"messages": []})
    assert "bad" not in str(info.value).replace("Неверный", "")
    with pytest.raises(ValueError, match="лимит"):
        await google.complete(runtime, {**config, "api_key": "limit"}, {"messages": []})
    with pytest.raises(ValueError, match="введите ключ"):
        await google.complete(runtime, {**config, "api_key": ""}, {"messages": []})
    assert "bad" not in caplog.text.replace("bad request", "") and TOKEN not in caplog.text


async def test_asr_transcribe_model_request(api):
    runtime, base, seen = api
    config = {"api_key": TOKEN, "url": base + "/interactions", "model": "gemini-3.5-transcribe", "language": "ru",
              "vocabulary": "Алиса, Яндекс"}
    text = await google.transcribe(runtime, config, wav_of(tone(16000)), "input.wav")
    assert text == "включи свет"
    body = seen["interactions"]
    assert body["store"] is False and body["model"] == "gemini-3.5-transcribe"
    audio = body["input"][0]
    assert audio["type"] == "audio" and audio["mime_type"] == "audio/wav"
    assert base64.b64decode(audio["data"])[:4] == b"RIFF"
    assert body["generation_config"]["transcription_config"] == {
        "language_codes": ["ru-RU"], "custom_vocabulary": ["Алиса", "Яндекс"]}
    assert seen["requests"][0]["token"] == TOKEN


async def test_asr_regular_model_gets_prompt_and_ogg_mime(api):
    runtime, base, seen = api
    ogg = b"OggS" + b"\0" * 40
    config = {"api_key": TOKEN, "url": base + "/interactions", "model": "gemini-3.8-flash", "language": "ru"}
    assert await google.transcribe(runtime, config, ogg, "input.ogg") == "включи свет"
    body = seen["interactions"]
    assert body["input"][0]["type"] == "text" and "Язык речи: ru" in body["input"][0]["text"]
    assert body["input"][1]["mime_type"] == "audio/ogg" and "generation_config" not in body


async def test_gemini_tts_returns_ogg_and_streams(api):
    runtime, base, seen = api
    config = {"api_key": TOKEN, "url": base + "/interactions", "engine": "gemini", "model": "gemini-3.8-flash-tts",
              "voice": "Kore", "style": "спокойно"}
    data, content_type = await google.speech(runtime, config, {"input": "Привет"})
    assert content_type == "audio/ogg" and data[:4] == b"OggS" and ogg_pages(data)
    body = seen["interactions"]
    assert body["store"] is False and body["generation_config"]["speech_config"] == [{"voice": "Kore"}]
    assert body["response_format"] == {"type": "audio", "mime_type": "audio/l16", "sample_rate": 24000}
    block = body["input"][0]["content"][0]
    assert block["text"] == "Привет" and block["annotations"] == [{"type": "speech_metadata", "style": "спокойно"}]
    assert google.streams(config)
    pages = [page async for page in google.speech_stream(runtime, config, "Привет")]
    assert pages and pages[0][:4] == b"OggS"
    pcm, rate = await google.synth_pcm(runtime, config, "Привет")
    assert rate == 24000 and pcm.dtype.name == "float32" and pcm.size > 1000
    with pytest.raises(ValueError, match="не подходит Gemini TTS"):
        await google.speech(runtime, {**config, "voice": "ru-RU-Wavenet-A"}, {"input": "x"})


async def test_cloud_tts_request(api):
    runtime, base, seen = api
    config = {"api_key": TOKEN, "url": base + "/v1", "engine": "cloud", "voice": "ru-RU-Wavenet-A",
              "speed": 1.2, "pitch": -2}
    data, _ = await google.speech(runtime, config, {"input": "Привет"})
    assert ogg_pages(data)
    body = seen["cloud"]
    assert body["input"] == {"text": "Привет"}
    assert body["voice"] == {"languageCode": "ru-RU", "name": "ru-RU-Wavenet-A"}
    assert body["audioConfig"] == {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000, "speakingRate": 1.2, "pitch": -2.0}
    # у Chirp 3 HD тон не действует — не отправляется
    await google.speech(runtime, {**config, "voice": "ru-RU-Chirp3-HD-Kore"}, {"input": "Привет"})
    assert "pitch" not in seen["cloud"]["audioConfig"]
    assert seen["requests"][0]["token"] == TOKEN and TOKEN not in seen["requests"][0]["query"]


async def test_cloud_voices_filtered_cached_and_refreshable(api):
    runtime, base, seen = api
    config = {"api_key": "voices-token", "url": base + "/v1", "engine": "cloud"}
    items = await google.voices(runtime, config)
    assert [i["value"] for i in items] == ["ru-RU-Chirp3-HD-Puck", "ru-RU-Wavenet-B", "ru-RU-Standard-A"]
    assert items[0]["gender"] == "male" and items[2]["lang"] == "ru-RU" and seen["voices_query"] == {"languageCode": "ru-RU"}
    await google.voices(runtime, config)
    assert seen["voice_calls"] == 1  # кеш на сессию
    await google.voices(runtime, {**config, "refresh": True})
    assert seen["voice_calls"] == 2


async def test_voices_without_key_and_gemini_set(api):
    runtime, base, seen = api
    assert await google.voices(runtime, {"engine": "cloud", "api_key": ""}) == google.CLOUD_OPTIONS
    gemini = await google.voices(runtime, {"engine": "gemini"})
    assert len(gemini) == 30 and gemini[0]["value"] == "Kore"
    assert seen["requests"] == []
    with pytest.raises(ValueError, match="Неверный ключ"):
        await google.voices(runtime, {"engine": "cloud", "api_key": "bad", "url": base + "/v1"})
