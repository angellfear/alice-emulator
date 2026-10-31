"""Домен misses: очередь «Эмулятор → Без ответа» — то, на что у эмулятора не нашлось обработчика.

GET → {http, phrases, callbacks, aapi}; DELETE ?kind=http|phrase|callback|aapi&key=… ('*' — все этого вида).
HTTP-промахи хранит EndpointCatalog, остальное — журналы miss_journal в Runtime; здесь только общий вход.
"""
from aiohttp import web

from ...endpoints import system_entries
from . import route


@route("misses", methods=("GET", "DELETE"))
async def misses(request, runtime, app):
    if request.method == 'GET':
        runtime.endpoints.reload()
        system = system_entries(runtime.quasar.ROUTES, runtime.updates.settings()['endpoints'])
        return web.json_response({'http': runtime.endpoints.public(system)['misses'],
                                  'phrases': runtime.phrase_misses.public(),
                                  'callbacks': runtime.callback_misses.public(),
                                  'aapi': runtime.aapi_misses.public()})
    stores = {'http': runtime.endpoints.forget_miss, 'phrase': runtime.phrase_misses.forget,
              'callback': runtime.callback_misses.forget, 'aapi': runtime.aapi_misses.forget}
    kind, key = request.query.get('kind'), request.query.get('key', '')
    if kind not in stores or not key:
        raise ValueError('Нужны kind=' + '|'.join(stores) + ' и key')
    async with runtime.lock:
        stores[kind](None if key == '*' else key)
    return web.json_response({'forgotten': key, 'kind': kind})
