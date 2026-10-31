"""Домены params и param_values: каталог определений параметров и значения владельцев (RF-03).

params — определения (docs/contracts/params-db.md): GET список с типами и
сферами; PUT определение; DELETE ?key= (вместе со значениями).
param_values — значения владельца: GET ?owner=user:id|device:sn|server
(секреты замаскированы); PUT {owner,key,value} с валидацией по определению
("__KEEP__" — сохранить секрет). Ветки перенесены дословно из
legacy-замыкания legacy_api (server_emulator/runtime.py, mount_ui); guard
и маппинг ошибок обеспечивает диспетчер реестра ui/api/__init__.py.
"""
from aiohttp import web

from ...params import PARAM_TYPES

from . import route


@route("params", methods=("GET", "PUT", "DELETE"))
async def params(request, runtime, app):
    # Каталог определений параметров (docs/contracts/params-db.md):
    # GET список; PUT определение; DELETE ?key= (вместе со значениями)
    if request.method == 'GET':
        return web.json_response({'defs': runtime.quasar_params.defs(),
                                  'types': PARAM_TYPES,
                                  'scopes': ['user', 'device', 'server']})
    if request.method == 'PUT':
        value = await request.json()
        async with runtime.lock:
            item = runtime.quasar_params.def_upsert(value.get('definition') or value)
        return web.json_response({'saved': item['key'], 'definition': item})
    if request.method == 'DELETE':
        async with runtime.lock:
            runtime.quasar_params.def_remove(request.query.get('key', ''))
        return web.json_response({'removed': request.query['key']})
    raise web.HTTPMethodNotAllowed(request.method, ['GET', 'PUT', 'DELETE'])


@route("param_values", methods=("GET", "PUT"))
async def param_values(request, runtime, app):
    # Значения владельца: GET ?owner=user:id|device:sn|server (секреты замаскированы);
    # PUT {owner,key,value} с валидацией по определению ("__KEEP__" — сохранить секрет)
    if request.method == 'GET':
        return web.json_response({'owner': request.query.get('owner', ''),
                                  'values': runtime.quasar_params.values(request.query.get('owner', ''))})
    if request.method == 'PUT':
        value = await request.json()
        async with runtime.lock:
            saved = runtime.quasar_params.set_value(value.get('owner', ''),
                                                    value.get('key', ''), value.get('value'))
        return web.json_response({'owner': value.get('owner'), 'key': value.get('key'),
                                  'value': saved})
    raise web.HTTPMethodNotAllowed(request.method, ['GET', 'PUT'])
