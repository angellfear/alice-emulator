"""Адаптеры моделей по подписке: разметка полей, параметры транспорта, понятная ошибка без входа."""
import types

import litellm
import pytest

from server_emulator.voice_stack import validate_fields
from server_emulator.voice_stack.adapters import chatgpt, claude


def test_chatgpt_markup():
    spec = chatgpt.SERVICES["llm"]
    validate_fields(spec["fields"])
    fields = {f["key"]: f for f in spec["fields"]}
    assert spec["defaults"]["model"] in {o["value"] for o in fields["model"]["options"]}
    assert all(set(example) <= set(fields) for example in spec["examples"].values())


async def test_chatgpt_without_login(tmp_path):
    with pytest.raises(ValueError, match="voice_stack.login chatgpt"):
        await chatgpt.complete(types.SimpleNamespace(directory=tmp_path), {}, {"messages": []})


async def test_chatgpt_params(tmp_path, monkeypatch):
    path = chatgpt.auth_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    seen = {}

    async def acompletion(**params):
        seen.update(params)
        return litellm.ModelResponse(choices=[{"message": {"role": "assistant", "content": "Привет"}}])

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    tools = [{"type": "function", "function": {"name": "lamp", "parameters": {"type": "object", "properties": {}}}}]
    result = await chatgpt.complete(types.SimpleNamespace(directory=tmp_path),
                                    {"model": "gpt-6-luna", "reasoning_effort": "low"},
                                    {"messages": [{"role": "user", "content": "x"}], "tools": tools,
                                     "temperature": 0.3})
    assert seen["model"] == "chatgpt/gpt-6-luna" and seen["chatgpt_auth_file"] == str(path)
    assert seen["tools"] == tools and seen["reasoning_effort"] == "low"
    assert "temperature" not in seen  # модели с размышлением температуру не принимают
    assert result["choices"][0]["message"]["content"] == "Привет"


def _result(**fields):
    import claude_agent_sdk
    base = {"subtype": "success", "duration_ms": 1, "duration_api_ms": 1, "is_error": False,
            "num_turns": 1, "session_id": "s"}
    return claude_agent_sdk.ResultMessage(**{**base, **fields})


def test_claude_markup():
    spec = claude.SERVICES["llm"]
    validate_fields(spec["fields"])
    fields = {f["key"]: f for f in spec["fields"]}
    assert fields["oauth_token"]["type"] == "password"
    assert all(set(example) <= set(fields) for example in spec["examples"].values())


def test_claude_transcript():
    assert claude.transcript([{"role": "system", "content": "Ты колонка"},
                              {"role": "user", "content": "привет"}]) == ("Ты колонка", "привет")
    system, prompt = claude.transcript([
        {"role": "user", "content": "включи свет"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "lamp", "arguments": "{\"on\": true}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "ok"}])
    assert system == ""
    assert "Пользователь: включи свет" in prompt and "Ассистент вызвал инструмент lamp {\"on\": true}" in prompt
    assert "Результат инструмента lamp: ok" in prompt


async def test_claude_tool_call_is_deferred(tmp_path, monkeypatch):
    import claude_agent_sdk
    seen = {}

    async def query(prompt, options):
        seen.update(prompt=prompt, options=options)
        yield claude_agent_sdk.AssistantMessage(content=[claude_agent_sdk.TextBlock(text="Включаю")], model="m")
        yield _result(subtype="success", usage={"input_tokens": 5, "cache_read_input_tokens": 2, "output_tokens": 3},
                      deferred_tool_use=claude_agent_sdk.DeferredToolUse(
                          id="toolu_1", name="mcp__alice__lamp", input={"on": True}))

    monkeypatch.setattr(claude_agent_sdk, "query", query)
    tools = [{"type": "function", "function": {"name": "lamp", "description": "Свет",
                                               "parameters": {"type": "object", "properties": {}}}}]
    result = await claude.complete(types.SimpleNamespace(directory=tmp_path),
                                   {"model": "claude-haiku-5-5", "oauth_token": "test-token"},
                                   {"messages": [{"role": "system", "content": "Ты колонка"},
                                                 {"role": "user", "content": "включи свет"}],
                                    "tools": tools, "tool_choice": "required"})
    options = seen["options"]
    assert seen["prompt"] == "включи свет" and options.tools == [] and options.setting_sources == []
    assert options.allowed_tools == ["mcp__alice__lamp"] and "alice" in options.mcp_servers
    assert options.env == {"CLAUDE_CODE_OAUTH_TOKEN": "test-token"}
    assert options.system_prompt.startswith("Ты колонка") and "вызовом одного из инструментов" in options.system_prompt
    decision = await options.hooks["PreToolUse"][0].hooks[0]({}, "toolu_1", None)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "defer"
    message = result["choices"][0]["message"]
    assert message["content"] == "Включаю"
    assert message["tool_calls"][0]["function"] == {"name": "lamp", "arguments": "{\"on\": true}"}
    assert result["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}


async def test_claude_text_and_error(tmp_path, monkeypatch):
    import claude_agent_sdk
    replies = [_result(result="Привет"), _result(subtype="error_during_execution", is_error=True, result="limit")]

    async def query(prompt, options):
        assert options.mcp_servers == {} and options.env == {}
        yield replies.pop(0)

    monkeypatch.setattr(claude_agent_sdk, "query", query)
    runtime = types.SimpleNamespace(directory=tmp_path)
    payload = {"messages": [{"role": "user", "content": "x"}]}
    result = await claude.complete(runtime, {}, payload)
    assert result["choices"][0]["message"] == {"role": "assistant", "content": "Привет"}
    with pytest.raises(ValueError, match="Claude — limit"):
        await claude.complete(runtime, {}, payload)
