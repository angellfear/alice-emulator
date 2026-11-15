"""ChatGPT по подписке (Plus/Pro) через LiteLLM, провайдер chatgpt: тот же бэкенд, что у
Codex CLI, вход по коду устройства, без API-ключа. Инструменты модели поддерживаются.

Вход — один раз: `python -m server_emulator.voice_stack.login chatgpt` (ссылка и код в
терминале). Файл входа — state/voice_stack/chatgpt/auth.json, токен litellm обновляет сам.
"""
from pathlib import Path

TITLE = "ChatGPT по подписке (Online)"
DESCRIPTION = ("ChatGPT Plus/Pro без API-ключа: вход аккаунтом ChatGPT, лимиты подписки. "
               "Вход: python -m server_emulator.voice_stack.login chatgpt")
MODELS = [{"value": "gpt-6-luna", "label": "GPT-6 Luna"},
          {"value": "gpt-5.6-luna", "label": "GPT-5.6 Luna"}]
SERVICES = {
    "llm": {
        "fields": [{"key": "model", "label": "Модель", "type": "select", "options": MODELS},
                   {"key": "reasoning_effort", "label": "Глубина размышления", "type": "select", "group": "advanced",
                    "options": [{"value": "", "label": "Не передавать"}, {"value": "low", "label": "Малая"},
                                {"value": "medium", "label": "Средняя"}, {"value": "high", "label": "Большая"}]}],
        "defaults": {"model": "gpt-6-luna", "reasoning_effort": "low"},
        "examples": {"ChatGPT по подписке (Online)": {"model": "gpt-6-luna", "reasoning_effort": "low"}},
    },
}
LOGIN = "python -m server_emulator.voice_stack.login chatgpt"


def auth_file(state):
    """Файл входа в папке состояния эмулятора."""
    return Path(state) / "voice_stack" / "chatgpt" / "auth.json"


async def complete(runtime, config, payload):
    path = auth_file(runtime.directory)
    if not path.is_file():
        raise ValueError(f"ChatGPT: нужен вход — {LOGIN}")
    from . import _litellm
    _litellm.prepare()
    import litellm
    params = {"model": "chatgpt/" + (config.get("model") or SERVICES["llm"]["defaults"]["model"]),
              "messages": payload.get("messages") or [],
              "chatgpt_auth_file": str(path), "timeout": 90, "num_retries": 0}
    for key in ("tools", "tool_choice"):
        if payload.get(key):
            params[key] = payload[key]
    if config.get("reasoning_effort"):
        params["reasoning_effort"] = config["reasoning_effort"]
    try:
        response = await litellm.acompletion(**params)
    except Exception as exc:  # noqa: BLE001 — любая ошибка litellm → ValueError конвейера
        _litellm.provider_error("model", exc)
    return _litellm.openai_response(response)
