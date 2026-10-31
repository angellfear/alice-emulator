"""Домен status: сводка ошибок и ожидающих вызовов по всем сессиям."""
from aiohttp import web

from . import route


@route("status", methods=("GET",))
async def status(request, runtime, app):
    errors, pending = list(runtime.recent), list(runtime.pending_calls)
    for session in runtime.sessions.values():
        scope = {k: session.get(k) for k in ('device_sn', 'user_id')}
        errors.extend({**e, **scope} for e in session['runtime'].recent)
        pending.extend({**c, **scope} for c in session['runtime'].pending_calls)
    from ...runtime import STANDALONE_KEY
    return web.json_response({"errors": errors, "pending_calls": pending,
                              "standalone": app.get(STANDALONE_KEY, False),
                              "ui_extensions": list(runtime.ui_extensions)})
