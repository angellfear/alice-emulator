"""Адаптер ElevenLabs на заглушке API: синтез потоком, голоса, запекание голоса (Instant Voice Clone), Scribe."""
import io
import logging
import struct
import wave

import pytest
from aiohttp import ClientSession, web

from server_emulator.audio_buffer import ogg_pages
from server_emulator.voice_stack import adapters, validate_fields, voice_modes
from server_emulator.voice_stack.adapters import elevenlabs

TOKEN = "test-token"
PRESET = "21m00Tcm4TlvDq8ikWAM"
BAKED = "BakedVoiceId0001"


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
    seen = {"tts": [], "voices": [], "added": [], "deleted": [], "stt": []}

    def guard(request):
        key = request.headers.get("xi-api-key", "")
        if key == "bad":
            return web.json_response({"detail": {"status": "invalid_api_key", "message": "Invalid API key"}}, status=401)
        if key == "limit":
            return web.json_response({"detail": {"status": "quota_exceeded", "message": "quota exceeded"}}, status=429)
        return None

    async def tts(request):
        failure = guard(request)
        if failure:
            return failure
        seen["tts"].append({"voice": request.match_info["voice"], "query": dict(request.query),
                            "key": request.headers.get("xi-api-key"), "body": await request.json()})
        response = web.StreamResponse()
        await response.prepare(request)
        pcm = tone(int(request.query["output_format"].split("_")[1]))
        for pos in range(0, len(pcm), 2501):
            await response.write(pcm[pos:pos + 2501])
        await response.write_eof()
        return response

    async def voices(request):
        failure = guard(request)
        if failure:
            return failure
        seen["voices"].append(dict(request.query))
        if request.query.get("next_page_token") == "page2":
            return web.json_response({"voices": [
                {"voice_id": BAKED, "name": "Мой голос", "category": "cloned", "labels": {"language": "ru"}}], "has_more": False})
        return web.json_response({"voices": [
            {"voice_id": PRESET, "name": "Rachel", "category": "premade", "labels": {"gender": "Female", "language": "en"}}],
            "has_more": True, "next_page_token": "page2"})

    async def add(request):
        failure = guard(request)
        if failure:
            return failure
        form = await request.post()
        upload = form["files"]
        seen["added"].append({"name": form["name"], "filename": upload.filename, "data": upload.file.read()})
        return web.json_response({"voice_id": BAKED, "requires_verification": False})

    async def delete(request):
        failure = guard(request)
        if failure:
            return failure
        seen["deleted"].append(request.match_info["voice"])
        return web.json_response({"status": "ok"})

    async def stt(request):
        failure = guard(request)
        if failure:
            return failure
        form = await request.post()
        seen["stt"].append({"model_id": form["model_id"], "language_code": form.get("language_code"),
                            "tag_audio_events": form.get("tag_audio_events"), "data": form["file"].file.read()})
        return web.json_response({"text": " Включи свет ", "language_code": "ru", "words": []})

    app = web.Application()
    app.router.add_post("/v1/text-to-speech/{voice}/stream", tts)
    app.router.add_get("/v2/voices", voices)
    app.router.add_post("/v1/voices/add", add)
    app.router.add_delete("/v1/voices/{voice}", delete)
    app.router.add_post("/v1/speech-to-text", stt)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    async with ClientSession() as session:
        yield Runtime(session), base, seen
    await runner.cleanup()


def test_adapter_registered_and_marked():
    assert adapters.get("elevenlabs") is elevenlabs
    assert elevenlabs.TITLE.endswith("(Online)")
    tts = elevenlabs.SERVICES["tts"]
    validate_fields(tts["fields"])
    validate_fields(elevenlabs.SERVICES["asr"]["fields"])
    assert voice_modes(tts) == ["preset", "baked"] and tts["bake"] == {"text": False}
    fields = {f["key"]: f for f in tts["fields"]}
    assert fields["api_key"]["type"] == "password" and fields["api_key"]["link"] and fields["api_key"]["required"]
    assert fields["voice"]["options_from"] == "voices"
    assert [o["value"] for o in fields["model"]["options"]] == [
        "eleven_multilingual_v2", "eleven_flash_v2_5", "eleven_turbo_v2_5"]
    for key in ("stability", "similarity_boost", "style", "speed", "use_speaker_boost", "sample_rate", "url", "language"):
        assert fields[key]["group"] == "advanced"


async def test_tts_request_format_and_stream(api):
    runtime, base, seen = api
    config = {"api_key": TOKEN, "url": base, "model": "eleven_flash_v2_5", "voice": PRESET, "language": "ru",
              "stability": 0.4, "similarity_boost": 0.8, "style": 0.1, "speed": 1.1, "use_speaker_boost": "no",
              "sample_rate": "22050"}
    data, content_type = await elevenlabs.speech(runtime, config, {"input": "Привет"})
    assert content_type == "audio/ogg" and ogg_pages(data)
    call = seen["tts"][0]
    assert call["voice"] == PRESET and call["query"] == {"output_format": "pcm_22050"} and call["key"] == TOKEN
    assert call["body"] == {"text": "Привет", "model_id": "eleven_flash_v2_5", "language_code": "ru",
                            "voice_settings": {"stability": 0.4, "similarity_boost": 0.8, "style": 0.1,
                                               "use_speaker_boost": False, "speed": 1.1}}
    assert elevenlabs.streams(config)
    pages = [page async for page in elevenlabs.speech_stream(runtime, config, "Привет")]
    assert pages and pages[0][:4] == b"OggS"
    pcm, rate = await elevenlabs.synth_pcm(runtime, config, "Привет")
    assert rate == 22050 and pcm.size == len(tone(22050)) // 2
    # у Multilingual v2 language_code не поддерживается — не отправляется
    await elevenlabs.speech(runtime, {**config, "model": "eleven_multilingual_v2"}, {"input": "Привет"})
    assert "language_code" not in seen["tts"][-1]["body"]


async def test_errors_are_russian_and_keys_stay_out_of_logs(api, caplog):
    runtime, base, seen = api
    caplog.set_level(logging.DEBUG)
    config = {"url": base, "voice": PRESET}
    with pytest.raises(ValueError, match="Неверный ключ"):
        await elevenlabs.speech(runtime, {**config, "api_key": "bad"}, {"input": "x"})
    with pytest.raises(ValueError, match="лимит"):
        await elevenlabs.speech(runtime, {**config, "api_key": "limit"}, {"input": "x"})
    with pytest.raises(ValueError, match="введите ключ"):
        await elevenlabs.speech(runtime, {**config, "api_key": ""}, {"input": "x"})
    with pytest.raises(ValueError, match="выберите голос"):
        await elevenlabs.speech(runtime, {"url": base, "api_key": TOKEN, "voice": "../etc"}, {"input": "x"})
    assert TOKEN not in caplog.text


async def test_voices_paginated_cached_and_default_without_key(api):
    runtime, base, seen = api
    config = {"api_key": "voices-token", "url": base}
    items = await elevenlabs.voices(runtime, config)
    assert [i["value"] for i in items] == [PRESET, BAKED]
    assert items[0]["gender"] == "female" and items[1]["label"] == "Мой голос (свой)" and items[1]["lang"] == "ru"
    assert len(seen["voices"]) == 2 and seen["voices"][1]["next_page_token"] == "page2"
    await elevenlabs.voices(runtime, config)
    assert len(seen["voices"]) == 2  # кеш на сессию
    await elevenlabs.voices(runtime, {**config, "refresh": True})
    assert len(seen["voices"]) == 4
    assert await elevenlabs.voices(runtime, {"api_key": ""}) == elevenlabs.OPTIONS
    with pytest.raises(ValueError, match="Неверный ключ"):
        await elevenlabs.voices(runtime, {"api_key": "bad", "url": base})


async def test_bake_voice_uses_clone_and_baked_voice_is_synthesized(api):
    runtime, base, seen = api
    config = {"api_key": TOKEN, "url": base}
    audio = wav_of(tone(16000, 3))
    result = await elevenlabs.bake_voice(runtime, config, audio, "текст эталона не нужен")
    assert result["voice_id"] == BAKED and result["title"].startswith("alica-")
    added = seen["added"][0]
    assert added["name"] == result["title"] and added["filename"] == "reference.wav" and added["data"] == audio
    # режим «запечённый»: синтез идёт по baked_voice_id, а не по голосу из списка
    baked = {**config, "voice": PRESET, "voice_mode": "baked", "baked_voice_id": result["voice_id"]}
    await elevenlabs.speech(runtime, baked, {"input": "Привет"})
    assert seen["tts"][-1]["voice"] == BAKED
    with pytest.raises(ValueError, match="не запечён"):
        await elevenlabs.speech(runtime, {**baked, "baked_voice_id": ""}, {"input": "x"})
    await elevenlabs.unbake_voice(runtime, config, result["voice_id"])
    assert seen["deleted"] == [BAKED]
    with pytest.raises(ValueError, match="Неверный ключ"):
        await elevenlabs.bake_voice(runtime, {**config, "api_key": "bad"}, audio, "")


async def test_scribe_transcription(api):
    runtime, base, seen = api
    config = {"api_key": TOKEN, "url": base, "model": "scribe_v2", "language": "ru"}
    audio = wav_of(tone(16000))
    assert await elevenlabs.transcribe(runtime, config, audio, "input.wav") == "Включи свет"
    call = seen["stt"][0]
    assert call["model_id"] == "scribe_v2" and call["language_code"] == "ru" and call["tag_audio_events"] == "false"
    assert call["data"] == audio
    with pytest.raises(ValueError, match="Неверный ключ"):
        await elevenlabs.transcribe(runtime, {**config, "api_key": "bad"}, audio, "input.wav")
    with pytest.raises(ValueError, match="лимит"):
        await elevenlabs.transcribe(runtime, {**config, "api_key": "limit"}, audio, "input.wav")
