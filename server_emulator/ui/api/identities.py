"""Домен identities: реестр идентичностей (пользователи/устройства) .

GET — слепок реестра + сводка активных сессий (без служебных ключей
runtime/send/ws/transport) + activity («виден/последний IP» для таблицы устройств);
POST — мутация привязки/удаления под runtime.lock, затем затронутым WS-сессиям рвётся
соединение (1012 «переподключись») в фоне — ответ не ждёт колонку.
"""
import copy

from aiohttp import web

from . import route


def _session_public(session):
    # capabilities.raw — bytes протокольного кадра (кладём в сессию
    # для диагностики); в JSON-ответе достаточно длины (bytes) и printable-превью —
    # с raw сериализация падала и вкладка «Устройства» не грузилась
    row = {k: v for k, v in session.items() if k not in ('runtime', 'send', 'ws', 'transport')}
    caps = row.get('capabilities')
    if isinstance(caps, dict):
        row['capabilities'] = {name: {k: v for k, v in entry.items() if k != 'raw'}
                               for name, entry in caps.items() if isinstance(entry, dict)}
    return row


@route("identities", methods=("GET", "POST"))
async def identities(request, runtime, app):
    if request.method == 'GET':
        # activity — оперативная сводка «виден/последний IP» для таблицы устройств;
        # приватный ключ glagol.security — секрет: наружу только маска (как секреты расширений)
        data = {**runtime.identities.public(),
                'sessions': [_session_public(s) for s in runtime.sessions.values()],
                'activity': copy.deepcopy(runtime.identities.activity)}
        for device in data.get('devices', []):
            security = (device.get('state') or {}).get('glagol_security')
            if isinstance(security, dict) and security.get('private_key'):
                security['private_key'] = runtime.masked_secret(security['private_key'])
        return web.json_response(data)
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
    async with runtime.lock:
        result=runtime.identities.mutate(value)
        runtime.refresh_profiles()
    # Закрытие — после мутации и в фоне: ответ не ждёт колонку, а переподключение
    # уже видит новую идентичность
    runtime.closer.close([(s['ws'], s.get('transport')) for s in affected if s.get('ws')],
                         1012, b'Binding changed, reconnect')
    return web.json_response(result)


@route("route_mode", methods=("PUT",))
async def route_mode(request, runtime, app):
    """Маршрут одного устройства: PUT {sn, mode}. Соединение закрывается (1012,
    колонка переподключается сама) только если меняется обслуживание открытого WS:
    переход в «Прозрачно» или из него."""
    value = await request.json()
    return web.json_response(await runtime.set_device_route_mode(value.get('sn'), value.get('mode')))


@route("record_mode", methods=("PUT",))
async def record_mode(request, runtime, app):
    """Опция «Запись» одного устройства: PUT {sn, mode: off|on|misses}.
    Соединение не закрывается — опция читается на каждом обмене и действует сразу."""
    value = await request.json()
    return web.json_response(await runtime.set_device_record_mode(value.get('sn'), value.get('mode')))


@route("fetch_state", methods=("POST",))
async def fetch_state(request, runtime, app):
    """Опрос состояния колонки: POST {sn} → SyncPush{GetDeviceState}, ожидание
    SyncPushResponse (≤ 5 с). Итог возвращается и сохраняется в devices.state.fetch_state_last.
    Частота ограничена настройкой fetch_state.min_interval_s: чаще — 429; колонка не
    ответила — 504; опрос вытеснен новым — 409; нет живой WS-сессии — 400."""
    from ...runtime import FetchStateSuperseded, FetchStateThrottled, FetchStateTimeout  # локально: runtime тянет весь эмулятор
    value = await request.json()
    try:
        return web.json_response(await runtime.fetch_state(str(value.get('sn') or '')))
    except FetchStateThrottled as exc:
        return web.json_response({'error': str(exc), 'code': 'fetch_state_throttled',
                                  'retry_after_s': exc.retry_after_s}, status=429)
    except FetchStateTimeout as exc:
        return web.json_response({'error': str(exc), 'code': 'fetch_state_timeout'}, status=504)
    except FetchStateSuperseded as exc:
        return web.json_response({'error': str(exc), 'code': 'fetch_state_superseded'}, status=409)


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


@route("device_apps", methods=("PUT",))
async def device_apps(request, runtime, app):
    """Ручной список установленных приложений колонки: PUT {sn, apps: [имена]}.
    Данные пользователя (device.state.apps), не протокол: сценарий app.open и промпт
    модели/агента читают их. Остальные ключи state сохраняются (read-modify-write
    под runtime.lock, как у fetch_state_last)."""
    value = await request.json()
    sn = str(value.get('sn') or '')
    apps = value.get('apps')
    if not isinstance(apps, list) or any(not isinstance(a, str) or not a.strip() for a in apps):
        raise ValueError('apps: список непустых строк')
    async with runtime.lock:
        device = next((d for d in runtime.identities.data['devices'] if d['sn'] == sn), None)
        if device is None:
            raise ValueError('SN не найден')
        state = copy.deepcopy(device.get('state') or {})
        state['apps'] = [str(a).strip() for a in apps]
        runtime.identities.mutate({'operation': 'state', 'sn': sn, 'state': state})
    return web.json_response({'sn': sn, 'apps': state['apps']})
