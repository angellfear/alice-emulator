"""Домен routines: вкладка «Задания» — задания агента по расписанию (agent/routines.py).

GET → {available, routines, runs, inbox, users, devices, scenarios}.
POST {op}: save {routine} (без id — создать) / delete {id} / run {id} — запустить сейчас /
read {ids} — отметить входящие прочитанными. Контракт — docs/contracts/routines.md.
"""
from aiohttp import web

from . import route


@route("routines", methods=("GET", "POST"))
async def routines_api(request, runtime, app):
    routines = getattr(runtime, "routines", None)
    if routines is None:
        return web.json_response({"available": False})
    if request.method == "GET":
        users = [{"id": u["id"], "name": u.get("name") or u["id"]} for u in runtime.identities.data["users"]]
        devices = [{"sn": d["sn"], "name": d.get("name") or d["sn"], "user_id": d.get("user_id")}
                   for d in runtime.identities.data["devices"] if d.get("user_id")]
        catalog = [{"id": s["id"], "title": (s.get("title") or {}).get("ru") if isinstance(s.get("title"), dict)
                    else s.get("title") or s["id"]} for s in runtime.dialog.scenarios()
                   if any(t.get("kind") == "phrase" for t in s.get("triggers", []))]
        return web.json_response({"available": True, "routines": routines.list(), "runs": routines.runs(limit=100),
                                  "inbox": routines.inbox(), "users": users, "devices": devices, "scenarios": catalog})
    value = await request.json()
    op = value.get("op")
    if op == "save":
        return web.json_response({"routine": routines.save(value["routine"])})
    if op == "delete":
        routines.delete(value["id"])
        return web.json_response({"ok": True})
    if op == "run":
        return web.json_response({"result": await routines.fire(value["id"], manual=True)})
    if op == "read":
        routines.mark_read([int(i) for i in value.get("ids") or []])
        return web.json_response({"ok": True})
    raise ValueError("Неизвестная операция: " + str(op))
