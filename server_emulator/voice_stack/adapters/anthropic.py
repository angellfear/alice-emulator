"""Anthropic Messages API (Claude) через LiteLLM: OpenAI-запрос конвейера → ответ модели,
инструменты в обе стороны (конвертацию форматов делает litellm)."""
from ...auth import secret

TITLE = "Anthropic (Claude) (Online)"
DESCRIPTION = "Claude через Messages API: платно, оплата по токенам. Инструменты модели поддерживаются."
SERVICES = {
    "llm": {
        "fields": [{"key": "api_key", "label": "API-ключ", "type": "password", "required": True,
                    "link": "https://console.anthropic.com/settings/keys",
                    "help": "console.anthropic.com → Settings → API keys"},
                   {"key": "model", "label": "Модель", "type": "select", "options": [
                       {"value": "claude-haiku-5-5", "label": "Claude Haiku 5.5 (быстрый)"},
                       {"value": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5"},
                       {"value": "claude-opus-5-5", "label": "Claude Opus 5.5"}]},
                   {"key": "temperature", "label": "Температура", "type": "number", "step": 0.1, "group": "advanced"},
                   {"key": "max_tokens", "label": "Максимум токенов ответа", "type": "number", "step": 1, "group": "advanced"},
                   {"key": "url", "label": "Адрес сервиса", "type": "text", "group": "advanced",
                    "help": "Пусто — https://api.anthropic.com/v1/messages; нужен для прокси"}],
        "defaults": {"url": "https://api.anthropic.com/v1/messages", "model": "claude-haiku-5-5",
                     "temperature": 0.3, "max_tokens": 512},
        "examples": {
            "Claude Haiku (Online)": {"model": "claude-haiku-5-5", "temperature": 0.3, "max_tokens": 512},
            "Claude Sonnet (Online)": {"model": "claude-sonnet-5-5", "temperature": 0.3, "max_tokens": 512},
        },
    },
}
MATCH = {"llm": r"api\.anthropic\.com"}
VERSION = "2023-06-01"

# Каталог litellm считает эти модели не принимающими temperature — поле адаптера
# должно доходить до провайдера, как раньше (см. _litellm.sampling_models)
SAMPLING_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")


async def complete_stream(runtime, config, payload):
    """Поток ответа Claude (необязательно): те же дельты и собранный ответ, что у openai.py."""
    from . import _litellm
    key = secret(config.get("api_key"))
    if not key:
        raise ValueError("Anthropic: нужен API-ключ")
    _litellm.prepare()
    _litellm.sampling_models(SAMPLING_MODELS)
    import litellm
    params = {"model": "anthropic/" + (config.get("model") or payload.get("model") or ""),
              "messages": payload.get("messages") or [],
              "api_base": _litellm.api_base(config.get("url") or SERVICES["llm"]["defaults"]["url"], "/v1/messages"),
              "api_key": key,
              "max_tokens": int(config.get("max_tokens") or 512),
              "temperature": float(payload.get("temperature", config.get("temperature", 0.3))),
              "timeout": 90, "num_retries": 0, "stream": True,
              "stream_options": {"include_usage": True}}
    if payload.get("tools"):
        params["tools"] = payload["tools"]
        if payload.get("tool_choice"):
            params["tool_choice"] = payload["tool_choice"]
    try:
        chunks = []
        async for chunk in await litellm.acompletion(**params):
            chunks.append(chunk)
            delta = (chunk.choices[0].delta.content or "") if chunk.choices else ""
            if delta:
                yield {"delta": delta}
        data = _litellm.openai_response(litellm.stream_chunk_builder(chunks, messages=params["messages"]))
        usage = data.get("usage") or {}
        data["usage"] = {"prompt_tokens": int(usage.get("prompt_tokens") or 0),
                         "completion_tokens": int(usage.get("completion_tokens") or 0)}
        yield {"result": data}
    except Exception as exc:  # noqa: BLE001 — любая ошибка litellm → ValueError конвейера
        import re
        status = getattr(exc, "status_code", None)
        found = re.search(r"'message':\s*'([^']+)'", str(exc)) or re.search(r'"message":\s*"([^"]+)"', str(exc))
        detail = found.group(1) if found else ""
        if status:
            raise ValueError(f"model: HTTP {status} от Anthropic {detail}".strip()) from exc
        raise ValueError(f"model: {str(exc)[:160]}") from exc


async def complete(runtime, config, payload):
    from . import _litellm
    key = secret(config.get("api_key"))
    if not key:
        raise ValueError("Anthropic: нужен API-ключ")
    _litellm.prepare()
    _litellm.sampling_models(SAMPLING_MODELS)
    import litellm
    params = {"model": "anthropic/" + (config.get("model") or payload.get("model") or ""),
              "messages": payload.get("messages") or [],
              "api_base": _litellm.api_base(config.get("url") or SERVICES["llm"]["defaults"]["url"], "/v1/messages"),
              "api_key": key,
              "max_tokens": int(config.get("max_tokens") or 512),
              "temperature": float(payload.get("temperature", config.get("temperature", 0.3))),
              "timeout": 90, "num_retries": 0}  # прежний адаптер не ретраил — ошибка провайдера сразу
    if payload.get("tools"):
        params["tools"] = payload["tools"]
        if payload.get("tool_choice"):
            params["tool_choice"] = payload["tool_choice"]
    try:
        response = await litellm.acompletion(**params)
    except Exception as exc:  # noqa: BLE001 — любая ошибка litellm → ValueError конвейера
        import re
        status = getattr(exc, "status_code", None)
        found = re.search(r"'message':\s*'([^']+)'", str(exc)) or re.search(r'"message":\s*"([^"]+)"', str(exc))
        detail = found.group(1) if found else ""
        if status:
            raise ValueError(f"model: HTTP {status} от Anthropic {detail}".strip()) from exc
        raise ValueError(f"model: {str(exc)[:160]}") from exc
    data = _litellm.openai_response(response)
    usage = data.get("usage") or {}
    # счётчики в форме конвейера: вход/выход (у litellm словарь с деталями)
    data["usage"] = {"prompt_tokens": int(usage.get("prompt_tokens") or 0),
                     "completion_tokens": int(usage.get("completion_tokens") or 0)}
    return data
