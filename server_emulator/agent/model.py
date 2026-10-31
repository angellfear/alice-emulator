"""Чат-модель LangChain поверх конвейера эмулятора.

Запрос идёт через Runtime.provider("model") — те же адаптеры voice_stack (OpenAI-совместимый,
Anthropic, свои из state/voice_stack/adapters), пресеты и профили устройств, что и раньше.
Формат на проводе — OpenAI Chat Completions: адаптеры уже переводят его в свой API.
"""
import json
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, convert_to_openai_messages
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict


def parse_message(message):
    """OpenAI message → AIMessage: аргументы-строки разбираются, битые — в invalid_tool_calls."""
    calls, invalid = [], []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        call_id = call.get("id") or "call_" + uuid.uuid4().hex[:12]  # без id нельзя сопоставить результат
        arguments = function.get("arguments") or "{}"
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
            if not isinstance(args, dict):
                raise ValueError("arguments: нужен объект")
            calls.append({"name": function.get("name", ""), "args": args, "id": call_id, "type": "tool_call"})
        except ValueError as exc:
            invalid.append({"name": function.get("name", ""), "args": str(arguments), "id": call_id,
                            "error": str(exc), "type": "invalid_tool_call"})
    return AIMessage(content=message.get("content") or "", tool_calls=calls, invalid_tool_calls=invalid)


class RuntimeChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    runtime: Any
    counter: Any = None  # {"calls", "tokens"} прогона — журнал и дневной бюджет (agent/ops.py)

    @property
    def _llm_type(self):
        return "alice-emulator-provider"

    def bind_tools(self, tools, tool_choice=None, **kwargs):
        """tool_choice в форме OpenAI: "any" (LangChain/trustcall) → "required", имя → function."""
        if tool_choice in ("any", True):
            tool_choice = "required"
        elif isinstance(tool_choice, str) and tool_choice not in ("auto", "none", "required"):
            tool_choice = {"type": "function", "function": {"name": tool_choice}}
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], tool_choice=tool_choice, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("RuntimeChatModel работает только асинхронно")

    def _payload(self, messages, tools, tool_choice):
        cfg = self.runtime.settings["model"]
        payload = {"model": cfg["model"], "temperature": cfg.get("temperature", 0.3),
                   "messages": convert_to_openai_messages(messages)}
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        return payload

    def _finish(self, result):
        """Ответ провайдера → AIMessage с usage; счётчик прогона (журнал, бюджет)."""
        message = parse_message(result["choices"][0]["message"])
        usage = message.response_metadata["usage"] = result.get("usage") or {}
        if self.counter is not None:
            self.counter["calls"] += 1
            self.counter["tokens"] += int(usage.get("total_tokens") or sum(
                int(usage.get(k) or 0) for k in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens")))
        return message

    async def _agenerate(self, messages, stop=None, run_manager=None, tools=None, tool_choice=None, **kwargs):
        raw, _ = await self.runtime.provider("model", self._payload(messages, tools, tool_choice))
        return ChatResult(generations=[ChatGeneration(message=self._finish(json.loads(raw)))])
