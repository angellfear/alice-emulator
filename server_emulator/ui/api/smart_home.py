"""Домен smart_home: реестр умного дома для вкладки — устройства (id, название, комната,
источник, online) и комнаты. Изменяют реестр расширения через SDK, не панель."""
from aiohttp import web

from . import route


@route("smart_home", methods=("GET",))
async def devices(request, runtime, app):
    return web.json_response({"devices": runtime.smart_home.devices(),
                              "rooms": runtime.smart_home.rooms()})
