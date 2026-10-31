"""Домен scenario-drafts: черновики сценариев агента во вкладке «Сценарии» (agent/scenario_edit.py).

GET → {available, drafts}. POST {op}: test {draft_id, phrases?} / activate {draft_id} — администратор,
в т.ч. предложения агента / discard {draft_id} / history {id} / revert {id, version}.
Контракт — docs/contracts/scenarios.md, раздел «Правка агентом».
"""
from aiohttp import web

from . import route


@route("scenario-drafts", methods=("GET", "POST"))
async def scenario_drafts(request, runtime, app):
    try:
        from ...agent import scenario_edit
    except ImportError:
        return web.json_response({"available": False})
    if request.method == "GET":
        return web.json_response({"available": True, "drafts": scenario_edit.drafts(runtime)})
    value = await request.json()
    op = value.get("op")
    if op == "test":
        return web.json_response(scenario_edit.test(runtime, value["draft_id"], value.get("phrases") or []))
    if op == "activate":
        return web.json_response(await scenario_edit.activate(runtime, value["draft_id"], approved_by_admin=True))
    if op == "discard":
        scenario_edit.discard(runtime, value["draft_id"])
        return web.json_response({"ok": True})
    if op == "history":
        return web.json_response({"versions": scenario_edit.history(runtime, value["id"])})
    if op == "revert":
        return web.json_response(await scenario_edit.revert(runtime, value["id"], value["version"]))
    raise ValueError("Неизвестная операция: " + str(op))
