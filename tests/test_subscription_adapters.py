"""Адаптеры моделей по подписке: разметка полей, параметры транспорта, понятная ошибка без входа."""
import types

import litellm
import pytest

from server_emulator.voice_stack import validate_fields
from server_emulator.voice_stack.adapters import chatgpt


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
