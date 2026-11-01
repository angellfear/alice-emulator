"""Домен server_log: журнал событий сервера «Дополнительно → Лог» (TASK-068).

GET server_log?level=&source=&q=&before=<ts>&limit=200 → {items, sources, more}
(записи — новые сверху, более старые — по метке before); level — имя или имена
через запятую («warning,error» для фильтра «Предупреждения и ошибки»).
DELETE server_log — очистить журнал. Только админ (остальной admin-API).
"""
from aiohttp import web

from . import route


@route("server_log", methods=("GET", "DELETE"))
async def server_log(request, runtime, app):
    store = runtime.server_log
    if request.method == "DELETE":
        await store.flush()
        store.clear()
        return web.json_response({"cleared": True})
    before = request.query.get("before", "")
    return web.json_response(store.query(
        level=request.query.get("level", ""),
        source=request.query.get("source", ""),
        q=request.query.get("q", ""),
        before=float(before) if before else None,
        limit=int(request.query.get("limit", "200"))))
