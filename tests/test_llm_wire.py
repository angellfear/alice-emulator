"""LLM на проводе: OpenAI-совместимый адаптер, Anthropic Messages API и чат-модель агента
против поддельного локального сервера — текст, вызовы инструментов, ошибки, usage."""
import json

import pytest
from aiohttp import ClientSession, web
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from server_emulator.agent.model import RuntimeChatModel
from server_emulator.voice_stack.adapters import anthropic, openai


class Lifecycle:
    def __init__(self, session):
        self.session = session

    def client_session(self):
        return self.session


class Runtime:
    def __init__(self, session):
        self.lifecycle = Lifecycle(session)


async def serve(routes):
    app = web.Application()
    for path, handler in routes.items():
        app.router.add_post(path, handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"


TOOLS = [{"type": "function", "function": {
    "name": "lamp", "description": "Свет", "parameters": {
        "type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]}}}]


async def test_openai_complete_text_and_tool_calls():
    seen = []

    async def chat(request):
        seen.append((request.headers.get("Authorization"), await request.json()))
        if len(seen) == 1:
            return web.json_response({"choices": [{"message": {"role": "assistant", "content": "Привет"}}],
                                      "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}})
        return web.json_response({"choices": [{"message": {
            "role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "lamp", "arguments": "{\"on\": true}"}}]}}]})

    runner, base = await serve({"/v1/chat/completions": chat})
    config = {"url": base + "/v1/chat/completions", "api_key": "test-token", "model": "m"}
    try:
        async with ClientSession() as session:
            runtime = Runtime(session)
            text = await openai.complete(runtime, config, {"model": "m", "messages": [
                {"role": "user", "content": "привет"}]})
            calls = await openai.complete(runtime, config, {"model": "m", "tools": TOOLS, "tool_choice": "required",
                                                            "messages": [{"role": "user", "content": "включи"}]})
    finally:
        await runner.cleanup()
    assert text["choices"][0]["message"]["content"] == "Привет" and text["usage"]["total_tokens"] == 7
    call = calls["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "lamp" and json.loads(call["function"]["arguments"]) == {"on": True}
    assert seen[0][0] == "Bearer test-token"
    assert seen[1][1]["tools"] == TOOLS and seen[1][1]["tool_choice"] == "required"


async def test_openai_http_error_and_missing_url():
    async def fail(request):
        return web.json_response({"error": {"message": "nope"}}, status=500)

    runner, base = await serve({"/v1/chat/completions": fail})
    try:
        async with ClientSession() as session:
            runtime = Runtime(session)
            with pytest.raises(ValueError, match="HTTP 500"):
                await openai.complete(runtime, {"url": base + "/v1/chat/completions"}, {"messages": []})
            with pytest.raises(ValueError, match="Настройте endpoint"):
                await openai.complete(runtime, {"url": ""}, {"messages": []})
    finally:
        await runner.cleanup()


async def test_openai_provider_error_is_immediate():
    """Ответ провайдера с ошибкой → ValueError сразу, без ретраев (litellm num_retries=0)."""
    hits = []

    async def fail(request):
        hits.append(1)
        return web.json_response({"error": {"message": "gone"}}, status=404)

    runner, base = await serve({"/v1/chat/completions": fail})
    try:
        async with ClientSession() as session:
            with pytest.raises(ValueError, match="HTTP 404"):
                await openai.complete(Runtime(session), {"url": base + "/v1/chat/completions", "api_key": "k"},
                                      {"messages": [{"role": "user", "content": "x"}]})
        assert len(hits) == 1  # ретраев нет — провайдер опрошен ровно один раз
    finally:
        await runner.cleanup()


async def test_anthropic_complete_over_messages_api():
    seen = []

    async def messages(request):
        seen.append((dict(request.headers), await request.json()))
        return web.json_response({
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": "Включаю"},
                        {"type": "tool_use", "id": "toolu_1", "name": "lamp", "input": {"on": True}}],
            "stop_reason": "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": 11, "output_tokens": 4}})

    runner, base = await serve({"/v1/messages": messages})
    config = {"url": base + "/v1/messages", "api_key": "test-token", "model": "claude-haiku-5-5", "max_tokens": 64}
    payload = {"model": "x", "tools": TOOLS, "tool_choice": "required", "messages": [
        {"role": "system", "content": "Ты колонка"}, {"role": "user", "content": "включи свет"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "lamp", "arguments": "{\"on\": true}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "ok"}, {"role": "user", "content": "ещё"}]}
    try:
        async with ClientSession() as session:
            result = await anthropic.complete(Runtime(session), config, payload)
    finally:
        await runner.cleanup()
    headers, body = seen[0]
    assert headers["x-api-key"] == "test-token" and headers["anthropic-version"] == anthropic.VERSION
    # litellm (транспорт) шлёт system и content блоками, историю инструментов
    # переводит в tool_use/tool_result — обе формы валидны для Messages API
    assert body["system"] == [{"type": "text", "text": "Ты колонка"}]
    assert body["messages"][0] == {"role": "user", "content": [{"type": "text", "text": "включи свет"}]}
    assert body["messages"][1]["content"][0] == {"type": "tool_use", "id": "t1", "name": "lamp", "input": {"on": True}}
    assert body["messages"][2]["content"][0]["type"] == "tool_result" and body["messages"][2]["content"][0]["tool_use_id"] == "t1"
    assert body["model"] == "claude-haiku-5-5" and body["max_tokens"] == 64
    assert body["tools"][0]["name"] == "lamp" and body["tools"][0]["input_schema"] == {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]}
    assert body["tool_choice"] == {"type": "any"}
    message = result["choices"][0]["message"]
    assert message["content"] == "Включаю"
    assert message["tool_calls"][0]["id"] == "toolu_1"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"on": True}
    assert result["usage"] == {"prompt_tokens": 11, "completion_tokens": 4}


async def test_anthropic_errors():
    async def bad(request):
        return web.json_response({"error": {"message": "invalid model"}}, status=400)

    runner, base = await serve({"/v1/messages": bad})
    try:
        async with ClientSession() as session:
            runtime = Runtime(session)
            with pytest.raises(ValueError, match="HTTP 400 от Anthropic invalid model"):
                await anthropic.complete(runtime, {"url": base + "/v1/messages", "api_key": "test-token"},
                                         {"messages": [{"role": "user", "content": "x"}]})
            with pytest.raises(ValueError, match="нужен API-ключ"):
                await anthropic.complete(runtime, {"url": base + "/v1/messages"}, {"messages": []})
            with pytest.raises(ValueError, match="Claude по подписке"):
                await anthropic.complete(runtime, {"api_key": "sk-ant-oat01-test-token"}, {"messages": []})
    finally:
        await runner.cleanup()


class ProviderRuntime:
    """Runtime.provider("model", payload) → (сырой ответ, тип) — как конвейер эмулятора."""
    def __init__(self, replies):
        self.settings = {"model": {"model": "m", "temperature": 0.2}}
        self.replies, self.payloads = list(replies), []

    async def provider(self, kind, payload):
        assert kind == "model"
        self.payloads.append(payload)
        return json.dumps(self.replies.pop(0)).encode(), "application/json"


@tool
def lamp(on: bool) -> str:
    """Свет."""
    return "ok"


async def test_runtime_chat_model_tool_calls_and_counter():
    runtime = ProviderRuntime([
        {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "lamp", "arguments": "{\"on\": false}"}},
            {"id": "c2", "type": "function", "function": {"name": "lamp", "arguments": "не json"}}]}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 3}},
        {"choices": [{"message": {"role": "assistant", "content": "Готово"}}], "usage": {"total_tokens": 9}}])
    counter = {"calls": 0, "tokens": 0}
    model = RuntimeChatModel(runtime=runtime, counter=counter)
    bound = model.bind_tools([lamp], tool_choice="any")
    first = await bound.ainvoke([SystemMessage("s"), HumanMessage("выключи")])
    assert first.tool_calls == [{"name": "lamp", "args": {"on": False}, "id": "c1", "type": "tool_call"}]
    assert first.invalid_tool_calls[0]["id"] == "c2"
    payload = runtime.payloads[0]
    assert payload["model"] == "m" and payload["temperature"] == 0.2 and payload["tool_choice"] == "required"
    assert payload["tools"][0]["function"]["name"] == "lamp"
    assert payload["messages"][0] == {"role": "system", "content": "s"}
    second = await model.ainvoke([HumanMessage("ну?")])
    assert second.content == "Готово" and "tools" not in runtime.payloads[1]
    assert counter == {"calls": 2, "tokens": 13 + 9}


def test_runtime_chat_model_named_tool_choice():
    model = RuntimeChatModel(runtime=ProviderRuntime([]))
    bound = model.bind_tools([lamp], tool_choice="lamp")
    assert bound.kwargs["tool_choice"] == {"type": "function", "function": {"name": "lamp"}}
