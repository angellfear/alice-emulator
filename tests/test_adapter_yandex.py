"""Адаптер Яндекса (SpeechKit REST v1 + YandexGPT) на заглушках API: формат запросов, 401/429, ключ не в логах."""
import json
import logging
from urllib.parse import parse_qs

import numpy as np
import pytest

from server_emulator.voice_stack.adapters import yandex
from tests.ru_stubs import Stub, StubRuntime, json_response, ogg_opus, pcm, wav
from aiohttp import web

KEY = "test-token"


@pytest.fixture
async def runtime():
    runtime = StubRuntime()
    yield runtime
    await runtime.close()


def config(stub, service, **extra):
    base = {"api_key": KEY, "folder_id": "b1gtest", "url": f"{stub.url}/{service}"}
    return {**yandex.SERVICES[service]["defaults"], **base, **extra}


async def audio(request, call, number):
    return web.Response(body=pcm(0.5, 48000), content_type="application/octet-stream")


async def test_tts_request_format_and_pcm(runtime):
    async with Stub({("POST", "/tts"): audio}) as stub:
        cfg = config(stub, "tts", voice="jane", emotion="evil", speed=1.5)
        samples, rate = await yandex.synth_pcm(runtime, cfg, "Привет")
        call = stub.calls[0]
        form = {k: v[0] for k, v in parse_qs(call["body"].decode()).items()}
        assert call["headers"]["Authorization"] == "Api-Key " + KEY
        assert form == {"text": "Привет", "lang": "ru-RU", "voice": "jane", "format": "lpcm", "sampleRateHertz": "48000",
                        "emotion": "evil", "speed": "1.5", "folderId": "b1gtest"}
        assert rate == 48000 and samples.dtype == np.float32 and len(samples) == 24000
        # эмоция, которой нет у голоса, не отправляется (иначе 400)
        await yandex.synth_pcm(runtime, config(stub, "tts", voice="filipp", emotion="good"), "Привет")
        assert "emotion" not in parse_qs(stub.calls[1]["body"].decode())


async def test_tts_stream_and_speech_give_ogg(runtime):
    async with Stub({("POST", "/tts"): audio}) as stub:
        cfg = config(stub, "tts")
        pages = [page async for page in yandex.speech_stream(runtime, cfg, "Привет")]
        assert pages and pages[0][:4] == b"OggS"
        body, content_type = await yandex.speech(runtime, cfg, {"input": "Привет"})
        assert body[:4] == b"OggS" and content_type == "audio/ogg" and yandex.streams(cfg)


@pytest.mark.parametrize("status,text", [(401, "Неверный ключ"), (429, "Превышен лимит")])
async def test_errors_are_readable(runtime, status, text):
    async def fail(request, call, number):
        return json_response({"error_code": "x", "error_message": "denied"}, status)
    async with Stub({("POST", "/tts"): fail, ("POST", "/asr"): fail, ("POST", "/llm"): fail}) as stub:
        for run in (yandex.synth_pcm(runtime, config(stub, "tts"), "а"),
                    yandex.transcribe(runtime, config(stub, "asr"), wav(1), "input.wav"),
                    yandex.complete(runtime, config(stub, "llm"), {"messages": []})):
            with pytest.raises(ValueError, match=text) as error:
                await run
            assert KEY not in str(error.value) and "Traceback" not in str(error.value)


async def test_asr_wav_goes_as_lpcm_and_ogg_as_is(runtime):
    async def recognize(request, call, number):
        return json_response({"result": "включи свет"})
    async with Stub({("POST", "/asr"): recognize}) as stub:
        cfg = config(stub, "asr")
        assert await yandex.transcribe(runtime, cfg, wav(1.0, 8000), "input.wav") == "включи свет"
        call = stub.calls[0]
        assert call["headers"]["Authorization"] == "Api-Key " + KEY
        assert call["query"] == {"format": "lpcm", "sampleRateHertz": "16000", "lang": "ru-RU", "topic": "general",
                                 "folderId": "b1gtest"}
        assert len(call["body"]) == 32000  # 1 с, 16 кГц, s16le: звук пересэмплирован
        opus = await ogg_opus(1.0)
        await yandex.transcribe(runtime, cfg, opus, "input.ogg")
        assert stub.calls[1]["query"]["format"] == "oggopus" and stub.calls[1]["body"] == opus


async def test_asr_limit_and_key_required(runtime):
    async with Stub({}) as stub:
        with pytest.raises(ValueError, match="длиннее лимита"):
            await yandex.transcribe(runtime, config(stub, "asr"), wav(40.0, 16000), "input.wav")
        with pytest.raises(ValueError, match="API-ключ"):
            await yandex.transcribe(runtime, config(stub, "asr", api_key=""), wav(1), "input.wav")


async def test_llm_openai_compatible_with_model_uri(runtime):
    async def chat(request, call, number):
        return json_response({"choices": [{"message": {"role": "assistant", "content": "Готово"}}], "usage": {}})
    async with Stub({("POST", "/llm"): chat}) as stub:
        cfg = config(stub, "llm")
        result = await yandex.complete(runtime, cfg, {"model": "ignored", "messages": [{"role": "user", "content": "привет"}],
                                                      "temperature": 0.2})
        call = stub.calls[0]
        body = json.loads(call["body"])
        assert result["choices"][0]["message"]["content"] == "Готово"
        assert body["model"] == "gpt://b1gtest/yandexgpt-lite/latest" and body["max_tokens"] == 512 and body["temperature"] == 0.2
        assert call["headers"]["Authorization"] == "Bearer " + KEY and call["headers"]["OpenAI-Project"] == "b1gtest"
        with pytest.raises(ValueError, match="каталога"):
            await yandex.complete(runtime, config(stub, "llm", folder_id=""), {"messages": []})


async def test_env_key_and_no_key_in_logs(runtime, caplog, monkeypatch):
    monkeypatch.setenv("YC_KEY_FOR_TEST", KEY)
    caplog.set_level(logging.DEBUG)

    async def bad(request, call, number):
        return json_response({"error_message": "no"}, 401)
    async with Stub({("POST", "/tts"): bad}) as stub:
        with pytest.raises(ValueError):
            await yandex.synth_pcm(runtime, config(stub, "tts", api_key="env:YC_KEY_FOR_TEST"), "а")
        assert stub.calls[0]["headers"]["Authorization"] == "Api-Key " + KEY
    assert KEY not in caplog.text


def test_fields_describe_secret_and_advanced():
    for service, spec in yandex.SERVICES.items():
        key = next(f for f in spec["fields"] if f["key"] == "api_key")
        assert key["type"] == "password" and key["required"] and key["link"].startswith("https://")
        assert any(f.get("group") == "advanced" for f in spec["fields"])
    assert yandex.SERVICES["tts"]["voice_modes"] == ["preset"]
    assert "(Online)" in yandex.TITLE
