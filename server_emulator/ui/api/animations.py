"""Домены раздела «Анимации».

animations — каталог GIF-кадров станций: GET список + реестр моделей; PUT сохранение записи
(с media_base64); DELETE ?id=; POST — проиграть запись каталога (draw_led_screen).
animations-media — выдача GIF каталога и GIF проектов (FileResponse, GET).
animations-platforms — GET платформы с колонками пользователя, их дисплеи и колонки.
animations-projects — проекты по платформам: GET ?platform=[&id=], PUT, DELETE ?platform=&id=.
animations-import — POST {platform, id, title, kind, filename, data_base64}: файл .led/.scled/.gif.
animations-export — GET ?platform=&id=: проект в родном формате (файл).
animations-play — POST {platform, id, sn}: проект на колонку.
"""
import base64
import binascii

from aiohttp import web

from ...animations import ANIMATION_MODELS, list_platforms, play_on_device, play_project
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
    result = await play_on_device(runtime, value.get('id'), value.get('sn') or '')
    return web.json_response(result)


@route("animations-media", methods=("GET",))
async def animations_media(request, runtime, app):
    path = runtime.animations.media_path(request.query.get('file') or '')
    return web.FileResponse(path, headers={'Cache-Control': 'no-cache'})


@route("animations-platforms", methods=("GET",))
async def animations_platforms(request, runtime, app):
    return web.json_response({'platforms': list_platforms(runtime.identities.data.get('devices', []))})


@route("animations-projects", methods=("GET", "PUT", "DELETE"))
async def animations_projects(request, runtime, app):
    store = runtime.animation_projects
    platform = request.query.get('platform') or ''
    if request.method == 'GET':
        project_id = request.query.get('id')
        if not project_id:
            return web.json_response({'projects': store.list(platform)})
        project = store.get(platform, project_id)
        if project is None:
            raise ValueError("Проект не найден")
        return web.json_response(project)
    if request.method == 'PUT':
        value = await request.json()
        entry = store.put(value.get('platform') or '', value)
        return web.json_response({'saved': entry['id']})
    project_id = request.query.get('id') or ''
    store.delete(platform, project_id)
    return web.json_response({'deleted': project_id})


@route("animations-import", methods=("POST",))
async def animations_import(request, runtime, app):
    value = await request.json()
    try:
        data = base64.b64decode(value.get('data_base64') or '', validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('data_base64: некорректная base64') from None
    entry = runtime.animation_projects.import_file(
        value.get('platform') or '', value.get('id'), value.get('title') or '',
        value.get('kind') or '', data, value.get('filename') or '')
    return web.json_response({'saved': entry['id'], 'frames': len(entry['frames'])})


@route("animations-export", methods=("GET",))
async def animations_export(request, runtime, app):
    name, content_type, data = runtime.animation_projects.export(
        request.query.get('platform') or '', request.query.get('id') or '')
    return web.Response(body=data, content_type=content_type.split(';')[0],
                        charset='utf-8' if 'charset' in content_type else None,
                        headers={'Content-Disposition': f'attachment; filename="{name}"'})


@route("animations-play", methods=("POST",))
async def animations_play(request, runtime, app):
    value = await request.json()
    result = await play_project(runtime, value.get('platform') or '', value.get('id') or '',
                                value.get('sn') or '', f"{request.scheme}://{request.host}")
    return web.json_response(result)
