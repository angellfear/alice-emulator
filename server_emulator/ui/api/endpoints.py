"""Домен endpoints: каталог захваченных эндпоинтов (RF-03).

GET список (с перечитыванием файлов — их могла написать утилита в живой
сервер); PUT {id,enabled} переключатель или PUT {endpoint} целиком;
DELETE ?id= — удаление. Ветка перенесена дословно из legacy-замыкания
legacy_api (server_emulator/runtime.py, mount_ui); guard и маппинг ошибок
обеспечивает диспетчер реестра ui/api/__init__.py.
"""
from aiohttp import web

from ...endpoints import system_entries
from . import route


@route("endpoints", methods=("GET", "PUT", "DELETE"))
async def endpoints(request, runtime, app):
    # Каталог захваченных эндпоинтов: GET список; PUT {id,enabled} переключатель
    # или PUT {endpoint} целиком; DELETE ?id= — удаление
    if request.method == 'GET' and 'script' in request.query:  # код обработчика (или заготовка)
        from ...endpoint_scripts import STUB
        code = runtime.endpoints.read_script(request.query['script']) if request.query['script'] else None
        return web.json_response({'code': code, 'stub': STUB})
    if request.method == 'GET':
        runtime.endpoints.reload()  # файлы могли написать утилитой в живой сервер
        return web.json_response(runtime.endpoints.public(
            system_entries(runtime.quasar.ROUTES, runtime.updates.settings()['endpoints'])))
    value = await request.json() if request.method == 'PUT' else None
    async with runtime.lock:
        if request.method == 'PUT' and set(value or {}) == {'id', 'enabled'}:
            return web.json_response(runtime.endpoints.toggle(value['id'], value['enabled']))
        if request.method == 'PUT':
            return web.json_response({'saved': runtime.endpoints.upsert(value.get('endpoint') or value, system_entries(
                runtime.quasar.ROUTES, runtime.updates.settings()['endpoints']), value.get('script'))['id']})
        if request.method == 'DELETE' and 'miss' in request.query:  # убрать зафиксированный промах ('*' — все)
            key = request.query['miss']
            runtime.endpoints.forget_miss(None if key == '*' else key)
            return web.json_response({'forgotten': key})
        if request.method == 'DELETE':
            removed = runtime.endpoints.remove(request.query.get('id', ''))
            if removed is None:
                raise ValueError('Эндпоинт не найден')
            return web.json_response({'removed': removed['id']})
    raise web.HTTPMethodNotAllowed(request.method, ['GET', 'PUT', 'DELETE'])


@route("endpoints-protobuf", methods=("POST",))
async def endpoints_protobuf(request, runtime, app):
    """{id, path, type?, body_base64, content_type} → {code, type, notice?}: скрипт, собирающий тело обратно.

    Есть импортированные схемы и тип известен (выбран или найден по пути /gproxy/<метод>) —
    поля по именам; иначе (или тело не совпало со схемой побайтно) — разбор без схемы.
    """
    import base64

    from ... import proto_schemas
    from ...protowire import _py, decode_named, to_source
    value = await request.json()
    data = base64.b64decode(value.get('body_base64') or '')
    if not data:
        raise ValueError('Тело пустое — разбирать нечего')
    content_type = str(value.get('content_type') or 'application/x-protobuf')
    name, notice = str(value.get('type') or '').strip(), None
    if proto_schemas.meta():
        if not name:
            method = str(value.get('path') or '').split('?')[0].removeprefix('/').removeprefix('gproxy/')
            name = proto_schemas.response_type(method, proto_schemas.names()) or ''
        if name:
            try:
                message = proto_schemas.decode_typed(name, data)
            except ValueError as exc:
                notice, name = str(exc), ''
            else:
                return web.json_response({'type': name, 'code': TYPED_SCRIPT.format(
                    id=value.get('id') or 'эндпоинта', type=name, size=len(data),
                    content_type=content_type, message=_py(message, 0))})
    elif name:
        raise ValueError('Схемы не импортированы — сначала «Импорт proto»')
    try:
        message = decode_named(data)
    except ValueError as exc:
        raise ValueError(f'Тело не разбирается как protobuf: {exc}') from None
    return web.json_response({'type': None, 'notice': notice, 'code': PROTOBUF_SCRIPT.format(
        id=value.get('id') or 'эндпоинта', size=len(data), content_type=content_type,
        message=to_source(message))})


@route("endpoints-proto-import", methods=("GET", "POST"))
async def endpoints_proto_import(request, runtime, app):
    """GET — сведения об импортированных схемах (+ имена типов); POST ?mode=update|replace&name= — тело: zip с .proto.

    update — новые файлы заменяют одноимённые, остальные остаются; replace — набор целиком из архива.
    """
    import asyncio
    import hashlib

    from ... import proto_schemas
    if request.method == 'GET':
        info = proto_schemas.meta()
        return web.json_response({'meta': info, 'types': proto_schemas.names() if info else []})
    proto_schemas.DIRECTORY.mkdir(parents=True, exist_ok=True)
    temp = proto_schemas.DIRECTORY / 'upload.tmp'
    digest, size = hashlib.sha256(), 0
    try:
        with temp.open('wb') as handle:  # мимо лимита client_max_size, потоком на диск
            async for chunk in request.content.iter_chunked(1 << 20):
                size += len(chunk)
                if size > PROTO_LIMIT:
                    raise ValueError('Архив больше 64 МБ')
                digest.update(chunk)
                handle.write(chunk)
        source, mode = request.query.get('name') or 'proto.zip', request.query.get('mode') or 'update'
        return web.json_response(await asyncio.get_running_loop().run_in_executor(
            None, proto_schemas.import_zip, temp, source, digest.hexdigest(), mode))
    finally:
        temp.unlink(missing_ok=True)


PROTO_LIMIT = 64 * 1024 ** 2

TYPED_SCRIPT = '''"""Ответ {id}: {type} — по импортированной схеме, разобран из тела ({size} байт).

Правьте значения по именам полей (формат protobuf JSON: int64 — строки, bytes — base64,
перечисления — имена). Сервер собирает двоичное тело по схеме.
"""
from server_emulator.endpoint_scripts import reply
from server_emulator.proto_schemas import encode_message

MESSAGE = {message}


async def handle(request, emulator):
    return reply(encode_message({type!r}, MESSAGE), content_type={content_type!r})
'''

PROTOBUF_SCRIPT = '''"""Ответ {id}: protobuf без схемы, разобран из тела ({size} байт).

Правьте значения: (номер поля, значение); вложенные сообщения — списки,
fixed32/fixed64 — числа с плавающей точкой в двоичном виде (подсказка в комментарии),
Struct({{...}}) — google.protobuf.Struct: обычный словарь (числа — float).
"""
from server_emulator.endpoint_scripts import reply
from server_emulator.protowire import Struct, encode, fixed32, fixed64

MESSAGE = {message}


async def handle(request, emulator):
    return reply(encode(MESSAGE), content_type={content_type!r})
'''
