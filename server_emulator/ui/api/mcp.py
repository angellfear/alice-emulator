"""Домены MCP: раздел «MCP» панели, обнаружение и вызов инструментов (RF-03).

mcp — GET: встроенные инструменты, серверы и текст state/mcp.json, выбор
инструментов внешних серверов (settings.tools), журнал вызовов;
POST {op}: config_save (текст mcp.json → проверка, запись, tools/list всех
включённых серверов) / discover / tool (вкл/класс риска) / call (пробный вызов).
Транспорт и исполнение — server_emulator/mcp_hub.py, контракт — docs/contracts/mcp.md.
mcp-discover и tool-call — прежняя поверхность (ручная панель «Проверка»); вызов агента,
ждущий подтверждения (thread_id), tool-call продолжает в его треде: approve или reject.
"""
import json
import time

from aiohttp import web

from ... import mcp_hub
from ...agent import policy
from . import route


async def _save_tools(runtime, mutate):
    # save_settings берёт runtime.lock сам — здесь замок не нужен (он не реентерабелен)
    settings = runtime.public()
    result = mutate(settings)
    await runtime.save_settings(settings, preserve_mode=True)
    return result


async def _discover(runtime, server_id):
    server = mcp_hub.server_of(runtime, server_id)
    found = await mcp_hub.list_tools(runtime, server, server_id)
    await _save_tools(runtime, lambda s: s.update(tools=mcp_hub.merge_tools(s["tools"], server_id, found)))
    return len(found)


def _servers(runtime):
    result = []
    for key, server in mcp_hub.servers(runtime).items():
        kind = mcp_hub.transport(server)
        result.append({"id": key, "type": kind, "disabled": bool(server.get("disabled")),
                       "target": server.get("url") if kind == "http" else " ".join([server.get("command", ""), *map(str, server.get("args", []))]),
                       "error": runtime.mcp_errors.get(key)})
    return result


@route("mcp", methods=("GET", "POST"))
async def mcp(request, runtime, app):
    if request.method == "GET":
        mcp_hub.load_config(runtime)  # создать mcp.json при первом заходе
        return web.json_response({
            "builtin": {"id": mcp_hub.BUILTIN_ID, "title": mcp_hub.BUILTIN_TITLE, "tools": mcp_hub.builtin_tools()},
            "config": mcp_hub.config_path(runtime).read_text(encoding="utf-8"),
            "path": str(mcp_hub.config_path(runtime)),
            "servers": _servers(runtime), "tools": runtime.public()["tools"],
            "log": list(runtime.tool_log)[-50:]})
    value = await request.json()
    op = value.get("op")
    if op == "config_save":
        try:
            config = json.loads(value.get("text") or "")
        except ValueError as exc:
            raise ValueError("mcp.json: неверный JSON — " + str(exc))
        mcp_hub.save_config(runtime, config)
        extension = mcp_hub.extension_servers(runtime)  # серверы расширений (TASK-032) — не из mcp.json
        keys = set(config["mcpServers"]) | set(extension)
        # инструменты удалённых из mcp.json серверов больше не нужны
        await _save_tools(runtime, lambda s: s.update(tools=[t for t in s["tools"] if t.get("server_id") in keys]))
        report = {}
        for key, server in {**extension, **config["mcpServers"]}.items():
            if server.get("disabled"):
                continue
            try:
                report[key] = {"count": await _discover(runtime, key)}
                runtime.mcp_errors.pop(key, None)
            except Exception as exc:  # один недоступный сервер не мешает остальным
                runtime.mcp_errors[key] = str(exc) or type(exc).__name__
                report[key] = {"error": runtime.mcp_errors[key]}
        return web.json_response({"saved": True, "servers": report})
    if op == "discover":
        server_id = value.get("server_id")
        try:
            count = await _discover(runtime, server_id)
        except Exception as exc:
            runtime.mcp_errors[server_id] = str(exc) or type(exc).__name__
            raise ValueError(runtime.mcp_errors[server_id])
        runtime.mcp_errors.pop(server_id, None)
        return web.json_response({"count": count})
    if op == "tool":
        name = value.get("name")

        def update(settings):
            tool = next((t for t in settings["tools"] if t["name"] == name), None)
            if tool is None:
                raise ValueError("Инструмент не найден: " + str(name))
            if "enabled" in value:
                tool["enabled"] = bool(value["enabled"])
            if "mode" in value:
                if value["mode"] not in mcp_hub.MODES:
                    raise ValueError("Режим: auto или confirm")
                tool["mode"] = value["mode"]
                tool.pop("risk", None)  # класс риска снова выводится из режима
            if "risk" in value:
                if value["risk"] not in policy.RISKS:
                    raise ValueError("Класс риска: " + ", ".join(policy.RISKS))
                tool["risk"], tool["mode"] = value["risk"], policy.mode_of(value["risk"])
            return tool
        return web.json_response({"tool": await _save_tools(runtime, update)})
    if op == "call":
        name, arguments = value.get("name"), value.get("arguments") or {}
        context = {k: value[k] for k in ("sn", "user_id") if value.get(k)}
        started = time.time()
        result = await mcp_hub.call(runtime, name, arguments, context, require_enabled=False)
        run = {"name": name, "arguments": arguments, "result": mcp_hub.result_text(result),
               "ts": started, "origin": "panel", "ms": int((time.time() - started) * 1000)}
        runtime.tool_log.append(run)
        del runtime.tool_log[:-100]
        return web.json_response({"result": result, "run": run})
    raise ValueError("Неизвестная операция: " + str(op))


@route("mcp-discover", methods=("POST",))
async def mcp_discover(request, runtime, app):
    value = await request.json()
    count = await _discover(runtime, value["server_id"])
    return web.json_response({"count": count, "next_cursor": None})


def _agent_call(runtime, call_id):
    """Ожидающий подтверждения вызов агента → (runtime его сессии, вызов) или (None, None)."""
    for scoped in [runtime] + [s["runtime"] for s in runtime.sessions.values() if s.get("runtime")]:
        call = next((c for c in scoped.pending_calls if c.get("id") == call_id and c.get("thread_id")), None)
        if call:
            return scoped, call
    return None, None


async def _agent_decision(runtime, value):
    scoped, call = _agent_call(runtime, value.get("call_id"))
    if call is None:
        return None
    thread = call["thread_id"]
    approve = () if value.get("reject") else {call["id"]}
    answer = await runtime.agent.resume(scoped, thread, approve=approve)
    for owner in [runtime] + [s["runtime"] for s in runtime.sessions.values() if s.get("runtime")]:
        owner.pending_calls = [c for c in owner.pending_calls if c.get("thread_id") != thread]
    scoped.pending_calls = (scoped.pending_calls + answer["tool_calls"])[-30:]  # агент мог спросить снова
    run = next((r for r in answer["tool_runs"] if r["name"] == call["function"]["name"]), None)
    text = run["result"] if run else "Отклонено."
    return {"content": [{"type": "text", "text": text}], "isError": False,
            "answer": answer["text"], "thread": thread}


@route("tool-call", methods=("POST",))
async def tool_call(request, runtime, app):
    value = await request.json()
    if value.get("call_id"):
        decided = await _agent_decision(runtime, value)
        if decided is not None:
            return web.json_response(decided)
    result = await mcp_hub.call(runtime, value["name"], value.get("arguments", {}))
    if value.get("call_id"):
        runtime.pending_calls = [call for call in runtime.pending_calls if call.get("id") != value["call_id"]]
    return web.json_response(result)
