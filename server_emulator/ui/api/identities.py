"""Домен identities: реестр идентичностей (пользователи/устройства) (RF-03).

GET — слепок реестра + сводка активных сессий (без служебных ключей
runtime/send/ws) + activity («виден/последний IP» для таблицы устройств) +
default_route_mode (TASK-044: эффективный режим устройства без собственного);
POST — мутация привязки/удаления: затронутым WS-сессиям рвётся соединение
(1012 «переподключись»), затем мутация под runtime.lock.
"""
import asyncio
import copy

from aiohttp import web

from . import route


@route("identities", methods=("GET", "POST"))
async def identities(request, runtime, app):
    if request.method == 'GET':
        # activity — оперативная сводка «виден/последний IP» для таблицы устройств
        return web.json_response({**runtime.identities.public(),
                                  'sessions': [{k:v for k,v in s.items() if k not in ('runtime','send','ws')} for s in runtime.sessions.values()],
                                  'activity': copy.deepcopy(runtime.identities.activity),
                                  'default_route_mode': runtime.settings.get('default_route_mode', 'emulate')})
    if request.method != 'POST':
        raise web.HTTPMethodNotAllowed(request.method, ['GET', 'POST'])
    value = await request.json()
    # Смена привязки/удаление пользователя или устройства рвут WS-сессии
    # затронутых устройств (1012 «переподключись», как при смене режима):
    # колонка переподключается и подхватывает новую идентичность сама.
    if value.get('operation') in ('device', 'pair', 'device_delete'):
        current = next((d for d in runtime.identities.data['devices'] if d['sn'] == value.get('sn')), None)
        aliases = set(value.get('device_ids') or []) | set((current or {}).get('device_ids', []))
        if value.get('operation') == 'pair':
            aliases |= {value.get('device_id')}  # pairing делает DeviceId алиасом SN
        if value.get('operation') == 'device_delete':
            aliases |= {value.get('sn')}
        affected = [s for s in runtime.sessions.values()
                    if s.get('device_sn') == value.get('sn') or s.get('device_id') in aliases]
    elif value.get('operation') == 'user_delete':
        owned = [d for d in runtime.identities.data['devices'] if d.get('user_id') == value.get('id')]
        sns = {d['sn'] for d in owned} | {i for d in owned for i in d.get('device_ids', [])}
        affected = [s for s in runtime.sessions.values()
                    if s.get('user_id') == value.get('id') or s.get('device_sn') in sns or s.get('device_id') in sns]
    else:
        affected = []
    sockets = [s['ws'] for s in affected if s.get('ws')]
    if sockets:
        await asyncio.gather(*(w.close(code=1012, message=b'Binding changed, reconnect') for w in sockets))
    async with runtime.lock:
        result=runtime.identities.mutate(value)
        runtime.refresh_profiles()
        return web.json_response(result)


@route("route_mode", methods=("PUT",))
async def route_mode(request, runtime, app):
    """Маршрут одного устройства (TASK-044): PUT {sn, mode} — открытые соединения
    этого устройства закрываются (1012), колонка переподключается сама."""
    value = await request.json()
    return web.json_response(await runtime.set_device_route_mode(value.get('sn'), value.get('mode')))


@route("record_mode", methods=("PUT",))
async def record_mode(request, runtime, app):
    """Опция «Запись» одного устройства (TASK-054): PUT {sn, mode: off|on|misses}.
    Соединение не закрывается — опция читается на каждом обмене и действует сразу."""
    value = await request.json()
    return web.json_response(await runtime.set_device_record_mode(value.get('sn'), value.get('mode')))


@route("user_login", methods=("GET", "PUT"))
async def user_login(request, runtime, app):
    # Вход пользователя реестра в кабинет /me: GET ?user=<id> → логин (без хэша);
    # PUT {user, name, password?} — создать/сменить. Без auth.json (открытый режим)
    # вход не включён: первый админ — локальной командой python -m server_emulator.auth
    auth = runtime.auth
    if request.method == 'GET':
        principal = auth.user_login(request.query.get('user', ''))
        return web.json_response({'enabled': auth.enabled, 'name': principal['name'] if principal else None})
    value = await request.json()
    user_id = value.get('user')
    if not any(u['id'] == user_id for u in runtime.identities.data['users']):
        raise ValueError('Пользователь не найден')
    if not auth.enabled:
        raise ValueError('Вход отключён: сначала создайте администратора командой python -m server_emulator.auth')
    principal = auth.set_user_login(user_id, value.get('name'), value.get('password') or None)
    return web.json_response({'enabled': True, 'name': principal['name']})
