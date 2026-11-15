"""Claude по подписке (Pro/Max) через Claude Agent SDK: модель отвечает через Claude Code,
вход аккаунтом Claude, без API-ключа. Инструменты модели поддерживаются.

SDK ведёт свой цикл агента, конвейеру же нужен один шаг: ответ или вызов инструмента.
Инструменты запроса становятся инструментами SDK-сервера MCP «alice» (свои инструменты
Claude Code выключены); хук PreToolUse отвечает «defer» — SDK останавливает прогон и
отдаёт вызов в ResultMessage.deferred_tool_use, инструмент не выполняется. История
диалога (реплики, вызовы и результаты инструментов) передаётся одной репликой-стенограммой,
системные сообщения — системным промптом.

Вход: токен `claude setup-token` в поле «Токен входа» (CLAUDE_CODE_OAUTH_TOKEN), пусто —
вход установленного Claude Code (`claude auth login`). Настройки, CLAUDE.md и сессии
пользователя не читаются и не пишутся (setting_sources=[], --no-session-persistence).
"""
import json
import uuid
from pathlib import Path

from ...auth import secret

TITLE = "Claude по подписке (Online)"
DESCRIPTION = ("Claude Pro/Max без API-ключа: через Claude Agent SDK, лимиты подписки. "
               "Токен входа — вывод команды claude setup-token.")
SERVICES = {
    "llm": {
        "fields": [{"key": "oauth_token", "label": "Токен входа", "type": "password",
                    "link": "https://code.claude.com/docs/en/authentication",
                    "help": "Вывод команды claude setup-token; пусто — вход установленного Claude Code"},
                   {"key": "model", "label": "Модель", "type": "select", "options": [
                       {"value": "claude-haiku-5-5", "label": "Claude Haiku 5.5 (быстрый)"},
                       {"value": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5"},
                       {"value": "claude-opus-5-5", "label": "Claude Opus 5.5"}]},
                   {"key": "effort", "label": "Глубина размышления", "type": "select", "group": "advanced",
                    "options": [{"value": "", "label": "Не передавать"}, {"value": "low", "label": "Малая"},
                                {"value": "medium", "label": "Средняя"}, {"value": "high", "label": "Большая"}]}],
        "defaults": {"model": "claude-haiku-5-5", "effort": "low"},
        "examples": {"Claude по подписке (Online)": {"model": "claude-haiku-5-5", "effort": "low"}},
    },
}
SERVER = "alice"
PREFIX = f"mcp__{SERVER}__"
ROLE = {"user": "Пользователь", "assistant": "Ассистент"}


def _text(content):
    """content сообщения OpenAI (строка или список частей) → текст."""
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return str(content or "")


def transcript(messages):
    """(системный промпт, реплика-стенограмма) из сообщений OpenAI."""
    system, lines, names = [], [], {}
    for message in messages:
        role = message.get("role")
        text = _text(message.get("content")).strip()
        if role in ("system", "developer"):
            system.append(text)
        elif role == "tool":
            name = names.get(message.get("tool_call_id"), "")
            lines.append(f"Результат инструмента{(' ' + name) if name else ''}: {text}")
        elif role in ROLE:
            if text:
                lines.append(f"{ROLE[role]}: {text}")
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                names[call.get("id")] = function.get("name", "")
                lines.append(f"Ассистент вызвал инструмент {function.get('name', '')} {function.get('arguments') or '{}'}")
    if len(lines) == 1 and lines[0].startswith(ROLE["user"] + ": "):
        return "\n\n".join(system), lines[0][len(ROLE["user"]) + 2:]
    return "\n\n".join(system), "Диалог до этого момента:\n\n" + "\n".join(lines) + "\n\nПродолжи диалог как Ассистент."


def _choice_rule(tool_choice):
    """tool_choice OpenAI → правило системного промпта (у SDK своего поля нет)."""
    if tool_choice == "required":
        return "Ответь вызовом одного из инструментов."
    if isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            return f"Ответь вызовом инструмента {name}."
    return ""


def _tools(tools):
    """Инструменты OpenAI → SDK-сервер MCP; обработчик не вызывается (вызов откладывается)."""
    from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server

    async def deferred(args):
        return {"content": [{"type": "text", "text": "отложено"}]}

    items = []
    for spec in tools:
        function = spec.get("function") or {}
        items.append(SdkMcpTool(name=function.get("name", ""), description=function.get("description") or "",
                                input_schema=function.get("parameters") or {"type": "object", "properties": {}},
                                handler=deferred))
    return create_sdk_mcp_server(SERVER, tools=items), [PREFIX + item.name for item in items]


async def _defer(hook_input, tool_use_id, context):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "defer"}}


def options(runtime, config, payload):
    """ClaudeAgentOptions запроса: один шаг модели, без инструментов и настроек Claude Code."""
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
    system, _ = transcript(payload.get("messages") or [])
    rule = _choice_rule(payload.get("tool_choice")) if payload.get("tools") else ""
    workdir = Path(runtime.directory) / "voice_stack" / "claude"
    workdir.mkdir(parents=True, exist_ok=True)
    kwargs = {"model": config.get("model") or SERVICES["llm"]["defaults"]["model"],
              "system_prompt": "\n\n".join(part for part in (system, rule) if part),
              "tools": [], "setting_sources": [], "strict_mcp_config": True, "cwd": str(workdir),
              "max_turns": 2, "extra_args": {"no-session-persistence": None}}
    if config.get("effort"):
        kwargs["effort"] = config["effort"]
    token = secret(config.get("oauth_token"))
    if token:
        kwargs["env"] = {"CLAUDE_CODE_OAUTH_TOKEN": token}
    if payload.get("tools"):
        server, names = _tools(payload["tools"])
        kwargs.update(mcp_servers={SERVER: server}, allowed_tools=names,
                      hooks={"PreToolUse": [HookMatcher(matcher=PREFIX + ".*", hooks=[_defer])]})
    return ClaudeAgentOptions(**kwargs)


def result_message(texts, result):
    """ResultMessage SDK → словарь OpenAI (choices/usage) — формат конвейера."""
    message = {"role": "assistant", "content": result.result or ""}
    deferred = getattr(result, "deferred_tool_use", None)
    if deferred is not None:
        name = deferred.name[len(PREFIX):] if deferred.name.startswith(PREFIX) else deferred.name
        message = {"role": "assistant", "content": "\n".join(texts),
                   "tool_calls": [{"id": deferred.id or "call_" + uuid.uuid4().hex[:12], "type": "function",
                                   "function": {"name": name,
                                                "arguments": json.dumps(deferred.input or {}, ensure_ascii=False)}}]}
    elif result.is_error:
        raise ValueError(f"model: Claude — {(result.result or result.subtype or 'ошибка')[:160]}")
    usage = result.usage or {}
    prompt = sum(int(usage.get(key) or 0) for key in
                 ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return {"choices": [{"message": message}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": int(usage.get("output_tokens") or 0)}}


async def complete(runtime, config, payload):
    from claude_agent_sdk import AssistantMessage, ClaudeSDKError, ResultMessage, TextBlock, query
    _, prompt = transcript(payload.get("messages") or [])
    texts, result = [], None
    try:
        async for message in query(prompt=prompt, options=options(runtime, config, payload)):
            if isinstance(message, AssistantMessage):
                texts.extend(block.text for block in message.content if isinstance(block, TextBlock) and block.text)
            elif isinstance(message, ResultMessage):
                result = message
    except ClaudeSDKError as exc:
        raise ValueError(f"model: Claude — {str(exc)[:200]}") from exc
    if result is None:
        raise ValueError("model: Claude не прислал ответ")
    return result_message(texts, result)
