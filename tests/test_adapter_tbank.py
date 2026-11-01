"""Адаптер T-Bank VoiceKit (REST) на заглушках API: JWT, форматы запросов, 401/429, ключи не в логах."""
import base64
import hashlib
import hmac
import json
import logging
import time

import pytest

from server_emulator.voice_stack.adapters import tbank
from tests.ru_stubs import Stub, StubRuntime, json_response, pcm, wav

API_KEY = "test-api-key"
SECRET = base64.urlsafe_b64encode(b"test-secret-bytes").decode().rstrip("=")


@pytest.fixture(autouse=True)
def clean_tokens():
    tbank._tokens.clear()
    yield
    tbank._tokens.clear()


@pytest.fixture
async def runtime():
    runtime = StubRuntime()
    yield runtime
    await runtime.close()


def config(stub, service, **extra):
    return {**tbank.SERVICES[service]["defaults"], "api_key": API_KEY, "secret_key": SECRET, "endpoint": stub.url, **extra}


def decode(token):
    head, body, signature = token.split(".")
    pad = lambda text: text + "=" * (-len(text) % 4)
    return (json.loads(base64.urlsafe_b64decode(pad(head))), json.loads(base64.urlsafe_b64decode(pad(body))),
            base64.urlsafe_b64decode(pad(signature)), f"{head}.{body}")


def test_jwt_structure_and_signature():
    token, exp = tbank.jwt(API_KEY, SECRET, "tinkoff.cloud.stt", now=1000)
    header, payload, signature, signed = decode(token)
    assert header == {"alg": "HS256", "typ": "JWT", "kid": API_KEY}
    assert payload == {"iss": "test_issuer", "sub": "test_user", "aud": "tinkoff.cloud.stt", "exp": 1600} and exp == 1600
    expected = hmac.new(b"test-secret-bytes", signed.encode(), hashlib.sha256).digest()
    assert signature == expected
    with pytest.raises(ValueError, match="Base64"):
        tbank.jwt(API_KEY, "!!!", "x")


async def test_stt_request_response_and_token_cache(runtime):
    async def recognize(request, call, number):
        return json_response({"results": [{"alternatives": [{"transcript": "включи", "confidence": 0.9}]},
                                          {"alternatives": [{"transcript": "свет"}]}]})
    async with Stub({("POST", "/v1/stt:recognize"): recognize}) as stub:
        cfg = config(stub, "asr")
        assert await tbank.transcribe(runtime, cfg, wav(1.0, 8000), "input.wav") == "включи свет"
        await tbank.transcribe(runtime, cfg, wav(1.0, 16000), "input.wav")
        call = stub.calls[0]
        body = json.loads(call["body"])
        assert body["config"] == {"encoding": "LINEAR16", "sample_rate_hertz": 16000, "num_channels": 1}
        assert len(base64.b64decode(body["audio"]["content"])) == 32000
        token = call["headers"]["Authorization"].removeprefix("Bearer ")
        assert decode(token)[1]["aud"] == "tinkoff.cloud.stt"
        assert stub.calls[1]["headers"]["Authorization"] == call["headers"]["Authorization"]  # токен из кеша


async def test_tts_request_pcm_and_ogg(runtime):
    async def synth(request, call, number):
        return json_response({"audio_content": base64.b64encode(pcm(0.5, 24000)).decode()})
    async with Stub({("POST", "/v1/tts:synthesize"): synth}) as stub:
        cfg = config(stub, "tts", voice="maxim", sample_rate="24000")
        samples, rate = await tbank.synth_pcm(runtime, cfg, "Привет")
        body = json.loads(stub.calls[0]["body"])
        assert body == {"input": {"text": "Привет"}, "voice": {"name": "maxim"},
                        "audio_config": {"audio_encoding": "LINEAR16", "sample_rate_hertz": 24000}}
        assert decode(stub.calls[0]["headers"]["Authorization"].removeprefix("Bearer "))[1]["aud"] == "tinkoff.cloud.tts"
        assert rate == 24000 and len(samples) == 12000
        pages = [page async for page in tbank.speech_stream(runtime, cfg, "Привет")]
        assert pages[0][:4] == b"OggS" and tbank.streams(cfg)
        assert (await tbank.speech(runtime, cfg, {"input": "Привет"}))[0][:4] == b"OggS"


@pytest.mark.parametrize("status,text", [(401, "Неверный ключ"), (429, "Превышен лимит")])
async def test_errors_are_readable(runtime, status, text):
    async def fail(request, call, number):
        return json_response({"message": "denied"}, status)
    async with Stub({("POST", "/v1/stt:recognize"): fail, ("POST", "/v1/tts:synthesize"): fail}) as stub:
        for run in (tbank.transcribe(runtime, config(stub, "asr"), wav(1), "input.wav"),
                    tbank.synth_pcm(runtime, config(stub, "tts"), "а")):
            with pytest.raises(ValueError, match=text) as error:
                await run
            assert API_KEY not in str(error.value) and SECRET not in str(error.value)


async def test_keys_required_and_not_logged(runtime, caplog):
    caplog.set_level(logging.DEBUG)

    async def fail(request, call, number):
        return json_response({}, 401)
    async with Stub({("POST", "/v1/tts:synthesize"): fail}) as stub:
        with pytest.raises(ValueError, match="укажите API-ключ и секретный ключ"):
            await tbank.synth_pcm(runtime, config(stub, "tts", secret_key=""), "а")
        with pytest.raises(ValueError):
            await tbank.synth_pcm(runtime, config(stub, "tts"), "а")
    assert SECRET not in caplog.text and API_KEY not in caplog.text


def test_expiring_token_is_renewed(monkeypatch):
    cfg = {"api_key": API_KEY, "secret_key": SECRET}
    first = tbank.token(cfg, "tinkoff.cloud.stt")
    assert tbank.token(cfg, "tinkoff.cloud.stt") == first
    monkeypatch.setattr(time, "time", lambda: time.monotonic() * 0 + 4_000_000_000)  # токен истёк
    assert tbank.token(cfg, "tinkoff.cloud.stt") != first


def test_fields():
    for service, spec in tbank.SERVICES.items():
        for key in ("api_key", "secret_key"):
            field = next(f for f in spec["fields"] if f["key"] == key)
            assert field["type"] == "password" and field["required"] and field["link"].startswith("https://")
        assert any(f.get("group") == "advanced" for f in spec["fields"])
    assert tbank.TITLE.endswith("(Online)")
