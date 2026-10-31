"""Домен updates: инвентарь обновлений устройства и сохранение OTA-политики.

GET ?sn= — журнал одного устройства, ?platform= — всех устройств платформы;
device_platforms — платформа каждого устройства реестра (серверная истина
для карточек и уровня политики «платформа»); catalog — все найденные
реальные URL прошивок с платформами (каталог не зависит от области).
"""
import asyncio

from aiohttp import web

from ...updates import platform_of, url_platforms
from . import route


@route("updates", methods=("GET", "POST"))
async def updates(request, runtime, app):
    if request.method == "GET":
        devices = runtime.identities.data["devices"]
        platforms = {d["sn"]: platform_of(d) for d in devices}
        platform = request.query.get('platform')
        sns = [sn for sn, p in platforms.items() if p == platform] if platform else None
        result = await asyncio.to_thread(runtime.updates.inventory, request.query.get('sn'), sns)
        for item in result['catalog']:
            item['platforms'] = url_platforms(item['url'], item['sns'], devices)
        return web.json_response({**result, 'device_platforms': platforms})
    value = await request.json()
    async with runtime.lock:
        return web.json_response(runtime.updates.save(value))
