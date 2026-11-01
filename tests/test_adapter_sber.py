"""Адаптер Сбера (SaluteSpeech REST + GigaChat) на заглушках API: обмен токена с кешем, 401/429, ключ не в логах."""
import base64
import json
import logging
import time
import uuid
from urllib.parse import parse_qs

import pytest
from aiohttp import web

from server_emulator.voice_stack.adapters import sber
from tests.ru_stubs import Stub, StubRuntime, json_response, ogg_opus, pcm, wav

AUTH_KEY = base64.b64encode(b"client:test-token").decode()


@pytest.fixture(autouse=True)
def clean_tokens():
    sber._tokens.clear()
    yield
    sber._tokens.clear()


@pytest.fixture
async def runtime():
    runtime = StubRuntime()
    yield runtime
    await runtime.close()


def oauth(expires_in=1800):
    async def handler(request, call, number):
        return json_response({"access_token": f"token-{number}", "expires_at": int((time.time() + expires_in) * 1000)})
    return handler


def config(stub, service, **extra):
    return {**sber.SERVICES[service]["defaults"], "api_key": AUTH_KEY, "auth_url": stub.url + "/oauth",
            "url": stub.url + ("/rest" if service != "llm" else "/chat"), **extra}


async def synth(request, call, number):
    return web.Response(body=pcm(0.5, 24000), content_type="application/octet-stream")


async def test_token_exchange_format_and_cache(runtime):
    async with Stub({("POST", "/oauth"): oauth(), ("POST", "/rest/text:synthesize"): synth}) as stub:
        cfg = config(stub, "tts", scope="B2B")
        await sber.synth_pcm(runtime, cfg, "один")
        await sber.synth_pcm(runtime, cfg, "два")
        oauth_calls = [c for c in stub.calls if c["path"] == "/oauth"]
        assert len(oauth_calls) == 1  # токен кешируется до истечения
        call = oauth_calls[0]
        assert call["headers"]["Authorization"] == "Basic " + AUTH_KEY
        assert str(uuid.UUID(call["headers"]["RqUID"], version=4)) == call["headers"]["RqUID"]
        assert parse_qs(call["body"].decode()) == {"scope": ["SALUTE_SPEECH_B2B"]}
        assert [c["headers"]["Authorization"] for c in stub.calls if c["path"] != "/oauth"] == ["Bearer token-1"] * 2


async def test_expired_token_is_refreshed(runtime):
    async with Stub({("POST", "/oauth"): oauth(expires_in=30), ("POST", "/rest/text:synthesize"): synth}) as stub:
        cfg = config(stub, "tts")
        await sber.synth_pcm(runtime, cfg, "один")  # срок < минуты — запас не позволяет переиспользовать
        await sber.synth_pcm(runtime, cfg, "два")
        assert len([c for c in stub.calls if c["path"] == "/oauth"]) == 2


async def test_revoked_token_gets_one_retry(runtime):
    async def flaky(request, call, number):
        if request.headers["Authorization"] == "Bearer token-1":
            return json_response({"message": "expired"}, 401)
        return web.Response(body=pcm(0.1), content_type="application/octet-stream")
    async with Stub({("POST", "/oauth"): oauth(), ("POST", "/rest/text:synthesize"): flaky}) as stub:
        samples, rate = await sber.synth_pcm(runtime, config(stub, "tts"), "а")
        assert rate == 24000 and len(samples) == 2400
        assert len([c for c in stub.calls if c["path"] == "/oauth"]) == 2


async def test_tts_query_ssml_and_ogg(runtime):
    async with Stub({("POST", "/oauth"): oauth(), ("POST", "/rest/text:synthesize"): synth}) as stub:
        cfg = config(stub, "tts", voice="Bys_24000")
        await sber.synth_pcm(runtime, cfg, "Привет")
        call = stub.calls[1]
        assert call["query"] == {"format": "pcm16", "voice": "Bys_24000"} and call["body"] == "Привет".encode()
        assert call["headers"]["Content-Type"].startswith("application/text")
        await sber.synth_pcm(runtime, {**cfg, "speed": "fast"}, "a < b")
        ssml = stub.calls[2]
        assert ssml["headers"]["Content-Type"].startswith("application/ssml")
        assert ssml["body"].decode() == '<speak><prosody rate="fast">a &lt; b</prosody></speak>'
        pages = [page async for page in sber.speech_stream(runtime, cfg, "Привет")]
        assert pages[0][:4] == b"OggS" and sber.streams(cfg)
        assert sber._rate({"voice": "Nec_8000"}) == 8000


async def test_asr_pcm_and_opus(runtime):
    async def recognize(request, call, number):
        return json_response({"result": ["включи", "свет"], "status": 200})
    async with Stub({("POST", "/oauth"): oauth(), ("POST", "/rest/speech:recognize"): recognize}) as stub:
        cfg = config(stub, "asr")
        assert await sber.transcribe(runtime, cfg, wav(1.0, 8000), "input.wav") == "включи свет"
        call = stub.calls[1]
        assert call["headers"]["Content-Type"] == "audio/x-pcm;bit=16;rate=16000" and len(call["body"]) == 32000
        assert call["query"] == {"language": "ru-RU", "model": "general"} and call["headers"]["Authorization"] == "Bearer token-1"
        opus = await ogg_opus(0.5)
        await sber.transcribe(runtime, cfg, opus, "input.ogg")
        assert stub.calls[2]["headers"]["Content-Type"] == "audio/ogg;codecs=opus" and stub.calls[2]["body"] == opus


@pytest.mark.parametrize("status,text", [(401, "Неверный ключ"), (429, "Превышен лимит")])
async def test_api_errors_are_readable(runtime, status, text):
    async def fail(request, call, number):
        return json_response({"message": "denied"}, status)
    async with Stub({("POST", "/oauth"): oauth(), ("POST", "/rest/text:synthesize"): fail,
                     ("POST", "/rest/speech:recognize"): fail, ("POST", "/chat"): fail}) as stub:
        for run in (sber.synth_pcm(runtime, config(stub, "tts"), "а"),
                    sber.transcribe(runtime, config(stub, "asr"), wav(1), "input.wav"),
                    sber.complete(runtime, config(stub, "llm"), {"messages": []})):
            with pytest.raises(ValueError, match=text) as error:
                await run
            assert "test-token" not in str(error.value) and AUTH_KEY not in str(error.value)


async def test_oauth_rejection_is_readable(runtime):
    async def deny(request, call, number):
        return json_response({"message": "bad key"}, 401)
    async with Stub({("POST", "/oauth"): deny}) as stub:
        with pytest.raises(ValueError, match="получение токена.*Неверный ключ"):
            await sber.synth_pcm(runtime, config(stub, "tts"), "а")
        with pytest.raises(ValueError, match="ключ авторизации"):
            await sber.synth_pcm(runtime, config(stub, "tts", api_key=""), "а")
        with pytest.raises(ValueError, match="тип доступа"):
            await sber.synth_pcm(runtime, config(stub, "tts", scope="X"), "а")


async def test_gigachat_functions_roundtrip(runtime):
    async def chat(request, call, number):
        return json_response({"choices": [{"message": {"role": "assistant", "content": "",
                                                       "function_call": {"name": "light", "arguments": {"on": True}}},
                                           "finish_reason": "function_call"}],
                              "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
    async with Stub({("POST", "/oauth"): oauth(), ("POST", "/chat"): chat}) as stub:
        payload = {"temperature": 0.2, "messages": [
            {"role": "system", "content": "sys"}, {"role": "user", "content": "включи"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "type": "function",
                                                                 "function": {"name": "light", "arguments": '{"on": false}'}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "ok"}],
            "tools": [{"type": "function", "function": {"name": "light", "description": "d", "parameters": {"type": "object"}}}]}
        result = await sber.complete(runtime, config(stub, "llm"), payload)
        call = stub.calls[1]
        body = json.loads(call["body"])
        assert call["headers"]["Authorization"] == "Bearer token-1"
        assert parse_qs(stub.calls[0]["body"].decode()) == {"scope": ["GIGACHAT_API_PERS"]}
        assert body["model"] == "GigaChat-2" and body["function_call"] == "auto" and body["functions"][0]["name"] == "light"
        assert body["messages"][2]["function_call"] == {"name": "light", "arguments": {"on": False}}
        assert body["messages"][3]["role"] == "function" and body["messages"][3]["name"] == "light"
        message = result["choices"][0]["message"]
        assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"on": True}
        assert message["tool_calls"][0]["function"]["name"] == "light"
        assert result["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}


async def test_certificate_settings(runtime, tmp_path):
    assert sber._ssl({"verify": "off"}) is False and sber._ssl({}) is None
    with pytest.raises(ValueError, match="сертификатов"):
        sber._ssl({"ca_bundle": str(tmp_path / "missing.pem")})
    broken = tmp_path / "broken.pem"
    broken.write_text("not a certificate", encoding="utf-8")
    with pytest.raises(ValueError, match="сертификатов"):
        sber._ssl({"ca_bundle": str(broken)})


async def test_secrets_not_in_logs(runtime, caplog):
    caplog.set_level(logging.DEBUG)
    async with Stub({("POST", "/oauth"): oauth(), ("POST", "/rest/text:synthesize"): synth}) as stub:
        await sber.synth_pcm(runtime, config(stub, "tts"), "а")
    assert AUTH_KEY not in caplog.text and "token-1" not in caplog.text


def test_fields_and_titles():
    for service, spec in sber.SERVICES.items():
        key = next(f for f in spec["fields"] if f["key"] == "api_key")
        assert key["type"] == "password" and key["required"] and key["link"].startswith("https://")
        assert {"ca_bundle", "verify"} <= {f["key"] for f in spec["fields"] if f.get("group") == "advanced"}
    assert sber.SERVICES["llm"]["title"].endswith("(Online Free)") and sber.SERVICES["tts"]["title"].endswith("(Online)")
