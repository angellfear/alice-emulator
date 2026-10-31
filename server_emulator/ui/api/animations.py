"""Домены animations и animations-media: раздел «Анимации» (RF-03).

animations — каталог LED-анимаций: GET список + реестр моделей; PUT
сохранение записи (с media_base64); DELETE ?id=; POST — проиграть на
устройстве (Push-директива draw_led_screen в живую сессию SN).
animations-media — выдача GIF-файлов каталога (FileResponse, GET-only).
Ветки перенесены дословно из legacy-замыкания legacy_api
(server_emulator/runtime.py, mount_ui); guard и маппинг ошибок
обеспечивает диспетчер реестра ui/api/__init__.py.
"""
from aiohttp import web

from ...animations import ANIMATION_MODELS, play_on_device

from . import route


@route("animations", methods=("GET", "PUT", "DELETE", "POST"))
async def animations(request, runtime, app):
    if request.method == 'GET':
        return web.json_response({'animations': runtime.animations.list(),
                                  'models': ANIMATION_MODELS})
    value = await request.json()
    if request.method == 'PUT':
        entry = runtime.animations.put(value.get('animation') or value, media_b64=value.get('media_base64'))
        return web.json_response({'saved': entry['id']})
    if request.method == 'DELETE':
        runtime.animations.delete(request.query.get('id'))
        return web.json_response({'deleted': request.query.get('id')})
    if request.method == 'POST':  # проиграть на устройстве
        result = await play_on_device(
            runtime, value.get('id'), value.get('sn') or '')
        return web.json_response(result)


@route("animations-media", methods=("GET",))
async def animations_media(request, runtime, app):
    if request.method != 'GET':
        raise web.HTTPMethodNotAllowed(request.method, ["GET"])
    path = runtime.animations.media_path(request.query.get('file') or '')
    return web.FileResponse(path, headers={'Cache-Control': 'no-cache'})
