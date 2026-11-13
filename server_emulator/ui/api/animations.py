"""Домены animations, animations-media, animations-platforms, animations-projects, animations-import.

animations — каталог LED-анимаций (JSON+GIF): GET список + реестр моделей;
PUT сохранение записи (с media_base64); DELETE ?id=; POST — проиграть на
устройстве (Push-директива draw_led_screen в живую сессию SN).
animations-media — выдача GIF-файлов каталога (FileResponse, GET-only).
animations-platforms — GET список платформ с их дисплеями для раздела «Анимации».
animations-projects — CRUD проектов анимаций (state/animations/<platform>/<id>.json):
GET список, GET/PUT/DELETE по id.
animations-import — POST импорт файла .led/.scled/.gif в проект.
"""
import base64
from aiohttp import web

from ...animations import (
    ANIMATION_MODELS, play_on_device, list_platforms, platform_has_displays,
    platform_displays,
)
from ...animation_storage import AnimationProjectStore
from ...animation_formats import parse_led_text, parse_scled_text, parse_scled_binary

from . import route


@route("animations", methods=("GET", "PUT", "DELETE", "POST"))
async def animations(request, runtime, app):
    if request.method == 'GET':
        return web.json_response({'animations': runtime.animations.list(),
                                  'models': ANIMATION_MODELS})
    value = await request.json()
    if request.method == 'PUT':
        entry = runtime.animations.put(value.get('animation') or value,
                                       media_b64=value.get('media_base64'))
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


@route("animations-platforms", methods=("GET",))
async def animations_platforms(request, runtime, app):
    """Список платформ с их дисплеями для раздела «Анимации»."""
    platforms = list_platforms()
    result = []
    for p in platforms:
        result.append({
            "platform": p["platform"],
            "title": p["title"],
            "displays": p["displays"],
        })
    return web.json_response({"platforms": result})


@route("animations-projects", methods=("GET", "PUT", "DELETE", "POST"))
async def animations_projects(request, runtime, app):
    """CRUD проектов анимаций.

    GET   ?platform= — список проектов платформы
    GET   ?platform=&id= — проект по id
    PUT   тело {platform, id, title, kind, size, fps, frames} — создать/заменить
    DELETE ?platform=&id= — удалить
    POST  тело {platform, id, title, kind, size, fps, frames} — создать/заменить (alias PUT)
    """
    store: AnimationProjectStore = runtime.animation_projects
    platform = request.query.get('platform') or ''

    if request.method == 'GET':
        project_id = request.query.get('id')
        if project_id:
            entry = store.get(platform, project_id)
            if entry is None:
                raise ValueError("Проект не найден")
            return web.json_response(entry)
        return web.json_response({'projects': store.list(platform)})

    if request.method in ('PUT', 'POST'):
        value = await request.json()
        if not platform:
            platform = value.get('platform', '')
        entry = store.put(platform, value,
                          media_b64=value.get('media_base64'))
        return web.json_response({'saved': entry['id']})
    if request.method == 'DELETE':
        project_id = request.query.get('id') or ''
        store.delete(platform, project_id)
        return web.json_response({'deleted': project_id})


@route("animations-import", methods=("POST",))
async def animations_import(request, runtime, app):
    """Импорт файла .led/.scled/.gif в проект.

    Тело JSON: {platform, id, title, kind, size, fps, media_base64, filename}
    media_base64 — содержимое файла (.led/.scled/.gif) в base64.
    """
    store: AnimationProjectStore = runtime.animation_projects
    value = await request.json()

    platform = value.get('platform')
    project_id = value.get('id')
    title = value.get('title') or ''
    kind = value.get('kind')
    size = value.get('size') or []
    fps = value.get('fps') or 30
    media_b64 = value.get('media_base64')
    filename = value.get('filename') or ''

    if not platform or not project_id:
        raise ValueError('platform и id обязательны')
    if not media_b64:
        raise ValueError('media_base64 обязателен')

    try:
        raw = base64.b64decode(media_b64, validate=True)
    except Exception:
        raise ValueError('media_base64: некорректная base64')

    entry = store.import_file(
        platform, project_id, title, kind, size, fps, raw, filename)
    return web.json_response({'saved': entry['id']})