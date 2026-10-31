"""Anthropic Messages API (Claude): OpenAI-запрос конвейера ↔ /v1/messages, инструменты в обе стороны."""
import json

from aiohttp import ClientTimeout

from ...auth import secret

TITLE = "Anthropic (Claude)"
DESCRIPTION = "Claude через Messages API; ключ — console.anthropic.com. Инструменты модели поддерживаются."
SERVICES = {
    "llm": {
        "fields": [{"key": "api_key", "label": "API-ключ", "type": "password"},
                   {"key": "model", "label": "Модель", "type": "select", "options": [
                       {"value": "claude-haiku-5-5", "label": "Claude Haiku 5.5 (быстрый)"},
                       {"value": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5"},
                       {"value": "claude-opus-5-5", "label": "Claude Opus 5.5"}]},
                   {"key": "temperature", "label": "Температура", "type": "number", "step": 0.1},
                   {"key": "max_tokens", "label": "Максимум токенов ответа", "type": "number", "step": 1},
                   {"key": "url", "label": "URL", "type": "text"}],
        "defaults": {"url": "https://api.anthropic.com/v1/messages", "model": "claude-haiku-5-5",
                     "temperature": 0.3, "max_tokens": 512},
    },
}
MATCH = {"llm": r"api\.anthropic\.com"}
VERSION = "2023-06-01"


def to_anthropic(payload, config):
    """OpenAI chat payload → Messages API: system отдельно, tool_calls/tool → tool_use/tool_result."""
    system, messages = [], []
    for message in payload.get("messages", []):
        role, content = message.get("role"), message.get("content") or ""
        if role == "system":
            system.append(content)
        elif role == "tool":
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": message.get("tool_call_id", ""), "content": str(content)}]})
        elif role == "assistant" and message.get("tool_calls"):
            blocks = [{"type": "text", "text": content}] if content else []
            for call in message["tool_calls"]:
                function = call.get("function", {})
                arguments = function.get("arguments") or "{}"
                blocks.append({"type": "tool_use", "id": call.get("id", ""), "name": function.get("name", ""),
                               "input": json.loads(arguments) if isinstance(arguments, str) else arguments})
            messages.append({"role": "assistant", "content": blocks})
        elif role in ("user", "assistant") and content:
            if messages and messages[-1]["role"] == role and isinstance(messages[-1]["content"], str):
                messages[-1]["content"] += "\n" + content  # Messages API: роли чередуются
            else:
                messages.append({"role": role, "content": content})
    body = {"model": config.get("model") or payload.get("model"), "messages": messages,
            "max_tokens": int(config.get("max_tokens") or 512),
            "temperature": float(payload.get("temperature", config.get("temperature", 0.3)))}
    if system:
        body["system"] = "\n\n".join(system)
    if payload.get("tools"):
        body["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                          "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                         for t in payload["tools"] if t.get("type") == "function"]
        choice = payload.get("tool_choice")
        if choice == "required":
            body["tool_choice"] = {"type": "any"}
        elif isinstance(choice, dict) and (choice.get("function") or {}).get("name"):
            body["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
    return body


def from_anthropic(result):
    """Messages API ответ → OpenAI {"choices": [{"message"}], "usage"}."""
    text, calls = [], []
    for block in result.get("content", []):
        if block.get("type") == "text":
            text.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append({"id": block.get("id", ""), "type": "function",
                          "function": {"name": block.get("name", ""),
                                       "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)}})
    message = {"role": "assistant", "content": "".join(text)}
    if calls:
        message["tool_calls"] = calls
    usage = result.get("usage", {})
    return {"choices": [{"message": message}],
            "usage": {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0)}}


async def complete(runtime, config, payload):
    key = secret(config.get("api_key"))
    if not key:
        raise ValueError("Anthropic: нужен API-ключ")
    headers = {"x-api-key": key, "anthropic-version": VERSION, "content-type": "application/json"}
    async with runtime.lifecycle.client_session().post(
            config.get("url") or SERVICES["llm"]["defaults"]["url"], json=to_anthropic(payload, config),
            headers=headers, timeout=ClientTimeout(total=90)) as response:
        body = await response.json(content_type=None)
        if response.status >= 400:
            detail = (body or {}).get("error", {}).get("message", "") if isinstance(body, dict) else ""
            raise ValueError(f"model: HTTP {response.status} от Anthropic {detail}".strip())
    return from_anthropic(body)
