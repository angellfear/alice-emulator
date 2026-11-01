"""OpenAI-совместимый адаптер и Anthropic: быстрое заполнение (examples) и разметка полей для UI."""
import pytest
from aiohttp import ClientSession, web

from server_emulator.voice_stack import coerce, validate_fields
from server_emulator.voice_stack.adapters import anthropic, openai

LABELS = ("(Online Free)", "(Online)")


class Lifecycle:
    def __init__(self, session):
        self.session = session

    def client_session(self):
        return self.session


class Runtime:
    def __init__(self, session):
        self.lifecycle = Lifecycle(session)


def examples(service):
    return openai.SERVICES[service]["examples"]


@pytest.mark.parametrize("service", ["llm", "asr", "tts"])
def test_examples_use_declared_fields_and_valid_urls(service):
    spec = openai.SERVICES[service]
    validate_fields(spec["fields"])
    keys = {f["key"] for f in spec["fields"]}
    for name, values in spec["examples"].items():
        assert set(values) <= keys, name
        assert str(values["url"]).startswith(("https://", "http://127.0.0.1")), name
        assert coerce(spec["fields"], values) == values  # приводится формой без потерь
        cloud = not values["url"].startswith("http://127.0.0.1")
        assert cloud == name.endswith(LABELS), name  # облачные — с меткой, локальные — без


def test_vendor_presets_required_by_the_task_are_present():
    llm, asr, tts = examples("llm"), examples("asr"), examples("tts")
    assert llm["OpenAI (Online)"]["url"].startswith("https://api.openai.com/")
    assert llm["Groq Llama (Online Free)"]["url"].startswith("https://api.groq.com/openai/")
    assert llm["DeepSeek (Online)"]["url"].startswith("https://api.deepseek.com/")
    assert llm["OpenRouter бесплатные модели (Online Free)"]["model"].endswith(":free")
    assert "OpenRouter (Online)" in llm
    gemini = llm["Gemini, OpenAI-совместимый (Online Free)"]
    assert gemini["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert {"OpenAI gpt-4o-transcribe (Online)", "OpenAI gpt-4o-mini-transcribe (Online)"} <= set(asr)
    assert asr["Groq Whisper large-v3 (Online Free)"]["model"] == "whisper-large-v3"
    assert asr["Groq Whisper large-v3-turbo (Online Free)"]["model"] == "whisper-large-v3-turbo"
    assert tts["OpenAI gpt-4o-mini-tts (Online)"]["model"] == "gpt-4o-mini-tts"
    assert "Ollama" in llm and "vLLM" in llm and "faster-whisper (speaches)" in asr


def test_markup_conventions():
    for service in ("llm", "asr", "tts"):
        fields = {f["key"]: f for f in openai.SERVICES[service]["fields"]}
        assert fields["api_key"]["type"] == "password" and fields["api_key"]["link"].startswith("https://")
        assert fields["url"]["required"] and fields["model"]["required"]
    assert next(f for f in openai.SERVICES["llm"]["fields"] if f["key"] == "temperature")["group"] == "advanced"
    assert next(f for f in openai.SERVICES["tts"]["fields"] if f["key"] == "response_format")["group"] == "advanced"
    voice = next(f for f in openai.SERVICES["tts"]["fields"] if f["key"] == "voice")
    assert "coral" in voice["help"] and openai.SERVICES["tts"]["voice_modes"] == ["preset"]


def test_anthropic_markup_and_label():
    assert anthropic.TITLE.endswith("(Online)")
    spec = anthropic.SERVICES["llm"]
    validate_fields(spec["fields"])
    fields = {f["key"]: f for f in spec["fields"]}
    assert fields["api_key"]["type"] == "password" and fields["api_key"]["required"] and fields["api_key"]["link"]
    for key in ("temperature", "max_tokens", "url"):
        assert fields[key]["group"] == "advanced"
    assert set(spec["examples"]["Claude Haiku (Online)"]) <= set(fields)


async def test_reasoning_effort_is_sent_only_when_set():
    seen = []

    async def chat(request):
        seen.append(await request.json())
        return web.json_response({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}/v1/chat/completions"
    try:
        async with ClientSession() as session:
            runtime = Runtime(session)
            payload = {"model": "m", "messages": []}
            await openai.complete(runtime, {"url": url, "api_key": "test-token", "reasoning_effort": "none"}, payload)
            await openai.complete(runtime, {"url": url, "api_key": "test-token", "reasoning_effort": ""}, payload)
            await openai.complete(runtime, {"url": url, "api_key": "test-token"}, {**payload, "reasoning_effort": "high"})
    finally:
        await runner.cleanup()
    assert seen[0]["reasoning_effort"] == "none" and "reasoning_effort" not in seen[1] and seen[2]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in payload  # исходный запрос не меняется
