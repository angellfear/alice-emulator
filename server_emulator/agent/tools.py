"""Инструменты агента (LangChain StructuredTool) из источников эмулятора.

Источники: MCP-хаб (встроенный alice + внешние серверы), alice_action (при
settings.model_actions), сценарии (agent/scenarios.py). Исполнение — прежние
исполнители; ошибка инструмента возвращается модели текстом, а не исключением.
Каждый вызов пишется в runtime.tool_log и в context["runs"] текущего прогона.
"""
import json
import time

from langchain_core.tools import StructuredTool

from .. import mcp_hub, model_actions
from . import scenarios

EMPTY = {"type": "object", "properties": {}}


def specs(runtime):
    """[(name, description, JSON schema)] — что видит модель."""
    result = [(t["function"]["name"], t["function"].get("description", ""), t["function"].get("parameters") or EMPTY)
              for t in mcp_hub.model_tools(runtime)]
    if runtime.settings.get("model_actions"):
        function = model_actions.TOOL_SCHEMA["function"]
        result.append((function["name"], function["description"], function["parameters"]))
    if getattr(runtime, "dialog", None) is not None:
        result += [(s["name"], s["description"], s["inputSchema"]) for s in scenarios.SPECS]
    return result


async def execute(runtime, name, arguments, context):
    """Вызов → текст результата для модели."""
    if name == model_actions.TOOL_NAME:
        notes, _executed, _deferred = model_actions.execute(
            runtime, [{"id": "", **arguments}], context.get("sn") or "", None, context["directives"])
        return " ".join(notes) or "Выполнено."
    if name in scenarios.NAMES:
        return json.dumps(await scenarios.call(runtime, name, arguments, context), ensure_ascii=False)
    return mcp_hub.result_text(await mcp_hub.call(runtime, name, arguments, context))


def build(runtime, context, extra=()):
    """extra — [(name, description, schema, executor)]: инструменты прогона (память)."""
    executors = {name: executor for name, _d, _s, executor in extra}

    def runner(name):
        async def run(**arguments):
            runtime.note_tool_call(name)  # TASK-088: «вызывает инструмент» в строке шапки (api/live)
            try:
                if name in executors:
                    text = str(await executors[name](**arguments))
                else:
                    text = await execute(runtime, name, arguments, context)
            except Exception as exc:  # инструмент не должен ронять ответ модели
                text = "Ошибка инструмента: " + (str(exc) or type(exc).__name__)
            runtime.note_tool_done()  # агент думает дальше: следующий вызов или ответ
            record = {"name": name, "arguments": arguments, "result": text, "ts": time.time()}
            context["runs"].append(record)
            runtime.tool_log.append(record)  # на месте: сессионная копия runtime делит журнал с панелью
            del runtime.tool_log[:-100]
            return text
        return run

    return [StructuredTool.from_function(coroutine=runner(name), name=name, description=description or name,
                                         args_schema=schema, infer_schema=False)
            for name, description, schema in specs(runtime) + [(n, d, s) for n, d, s, _e in extra]]
