"""Домен updates: инвентарь обновлений устройства и сохранение OTA-политики.

GET ?sn= — журнал одного устройства, ?platform= — всех устройств платформы;
device_platforms — платформа каждого устройства реестра (серверная истина
для карточек и уровня политики «платформа»); device_versions — последняя
прошивка устройства по журналу трафика (версия из get_sync_info);
catalog — все найденные реальные URL прошивок с платформами (каталог не
зависит от области).
"""
import re
import sqlite3

import aiosqlite
from aiohttp import web

from ...updates import platform_of, url_platforms
from . import route


async def _device_versions(runtime, sns):
    """Последняя версия прошивки устройства: get_sync_info в журнале трафика
    несёт её в query (?version=0.368…). Читается read-only отдельным соединением aiosqlite
    (журнал трафика — другая база, чем у воркера записи; WAL читателям не мешает)."""
    out = {}
    path = runtime.directory / 'traffic.sqlite3'
    if not sns or not path.exists():
        return out
    try:
        async with aiosqlite.connect(f'file:{path}?mode=ro', uri=True) as db:
            marks = ','.join('?' * len(sns))
            rows = await (await db.execute(
                f'SELECT device_sn, path FROM exchanges WHERE device_sn IN ({marks}) '
                f"AND path LIKE '%get_sync_info%' ORDER BY ts DESC LIMIT 400", list(sns))).fetchall()
            for sn, p in rows:
                if sn in out:
                    continue
                m = re.search(r'version=([0-9.]+)', p or '')
                if m:
                    out[sn] = m.group(1)
    except sqlite3.Error:
        pass
    return out


@route("updates", methods=("GET", "POST"))
async def updates(request, runtime, app):
    if request.method == "GET":
        devices = runtime.identities.data["devices"]
        platforms = {d["sn"]: platform_of(d) for d in devices}
        platform = request.query.get('platform')
        sns = [sn for sn, p in platforms.items() if p == platform] if platform else None
        result = await runtime.updates.inventory(request.query.get('sn'), sns)
        for item in result['catalog']:
            item['platforms'] = url_platforms(item['url'], item['sns'], devices)
        versions = await _device_versions(runtime, list(platforms))
        return web.json_response({**result, 'device_platforms': platforms, 'device_versions': versions})
    value = await request.json()
    async with runtime.lock:
        return web.json_response(await runtime.updates.save(value))
