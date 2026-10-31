"""Домен ws_handlers: реестр обработчиков WS и колбэки с поставщиками (TASK-058).

GET → {handlers, callbacks}: handlers — записи реестра aapi_handlers (событие, источник,
имя, условие включения, счётчики); callbacks — маршруты колбэков: внутреннее событие,
поставщик данных (сценарий с provide-триггером, пользовательский перекрывает встроенный),
счётчики колбэка.
"""
from aiohttp import web

from ... import aapi_handlers
from . import route


@route("ws_handlers", methods=("GET",))
async def ws_handlers(request, runtime, app):
    callbacks = []
    for item in aapi_handlers.routes():
        provider = runtime.dialog.provider(item["provide"])
        callbacks.append({**item,
                          "provider": None if provider is None else {"id": provider.get("id"),
                                                                     "origin": provider.get("_origin") or "user"},
                          "overridden": bool(provider) and provider.get("_origin") != "builtin",
                          "counters": aapi_handlers.counters(item["event"])})
    return web.json_response({"handlers": aapi_handlers.registry(runtime.settings), "callbacks": callbacks})
