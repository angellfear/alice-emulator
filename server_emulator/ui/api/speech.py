"""Домен speech: структурная трасса речевого тракта, только чтение
(?limit=, не больше 300 последних событий) (RF-03). Ветка перенесена
дословно из legacy-замыкания legacy_api (server_emulator/runtime.py);
явной проверки метода в legacy не было — объявлены ("GET", "POST").
"""
from aiohttp import web

from . import route


@route("speech", methods=("GET", "POST"))
async def speech(request, runtime, app):
    # v9 §2: структурная трасса речевого тракта (только чтение)
    limit = min(int(request.query.get('limit', 100)), 300)
    return web.json_response({'events': runtime.speech_log[-limit:]})
