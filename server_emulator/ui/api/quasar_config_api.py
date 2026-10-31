"""Домены quasar_platforms и quasar_composed: слои Quasar-конфига и итоговая сборка (RF-03).

quasar_platforms — редактор «Переопределение» sync-конфигов: общий common
(тело, не удаляется) + платформы-патчи (чистый merge-patch в файлах) +
templates.json (токены/инлайн-узлы/источники значений). quasar_composed —
итоговый конфиг устройства после всех слоёв + происхождение каждого значения
(только чтение). Ветки перенесены дословно из legacy-замыкания legacy_api
(server_emulator/runtime.py, mount_ui); guard и маппинг ошибок обеспечивает
диспетчер реестра ui/api/__init__.py.
"""
from aiohttp import web

from ...quasar_config import account_layer, compose, poll_period_warnings

from . import route


@route("quasar_platforms", methods=("GET", "PUT", "DELETE"))
async def quasar_platforms(request, runtime, app):
    # Слои Quasar-конфига: общий common (тело, не удаляется) +
    # платформы-патчи (чистый merge-patch в файлах) + templates.json.
    if request.method == 'GET':
        name = request.query.get('platform')
        if name:
            item = runtime.quasar_platforms.load(name)
            if item is None:
                raise ValueError('Файл конфига не найден')
            return web.json_response(item)
        return web.json_response({
            'common': runtime.quasar_platforms.ensure_common(),
            'platforms': runtime.quasar_platforms.list(),
            'tokens': ['now', 'now_ms', 'now_iso', 'uuid', 'device_id', 'device_name',
                       'platform', 'user_id', 'user.name', 'user.location.*', 'user.fields.*',
                       'device.name', 'device.room', 'device.fields.*'],
            'inline': {'$select': 'выбранный вариант (ключи-варианты рядом = подписи)',
                       '$string/$number/$list': 'литерал (строки — с {{токенами}})',
                       '$bool': 'вкл/выкл', '$param': 'параметр-заглушка: device.sn, session.sn, user.name…',
                       '$name/$hint': 'подпись и пояснение для простого редактора'},
            'sources': {'expr': '{{шаблон}}', 'db': 'user.<путь> | device.<путь>',
                        'run': 'команда из state/quasar/scripts (белый список, таймаут 3 с)',
                        'delete': 'только в переопределении: удалить ключ',
                        'select': 'переключить селектор: {"$select": "вариант"}'}})
    if request.method == 'PUT':
        value = await request.json()
        async with runtime.lock:
            if 'common_file' in value:
                runtime.quasar_platforms.save_common(value['common_file'])
                return web.json_response({'saved': 'common'})
            item = value.get('platform_file') or {}
            runtime.quasar_platforms.save(item.get('platform', ''),
                                          item.get('patch'))
            return web.json_response({'saved': item.get('platform')})
    if request.method == 'DELETE':
        async with runtime.lock:
            removed = runtime.quasar_platforms.remove(request.query.get('platform', ''))
        if not removed:
            raise ValueError('Платформа не найдена (общий конфиг не удаляется)')
        return web.json_response({'removed': request.query['platform']})
    raise web.HTTPMethodNotAllowed(request.method, ['GET', 'PUT', 'DELETE'])


@route("quasar_composed", methods=("GET",))
async def quasar_composed(request, runtime, app):
    # Итоговый конфиг устройства после всех слоёв + происхождение
    # каждого значения (для просмотрщика в «Переопределении»).
    if request.method != 'GET':
        raise web.HTTPMethodNotAllowed(request.method, ['GET'])
    sn = request.query.get('device', '')
    device = next((d for d in runtime.identities.data['devices']
                   if d.get('sn') == sn), None)
    if device is None:
        raise ValueError('Устройство не найдено в реестре')
    state = device.get('state') or {}
    common, platform_patch = runtime.quasar_platforms.resolve_files(device)
    platform_name = runtime.quasar_platforms.platform_for(device)
    if common is None:  # legacy: полный конфиг в state.quasar
        return web.json_response({'legacy': True, 'platform': None,
                                  'config': state.get('quasar', {}),
                                  'origins': {}, 'overrides':
                                  state.get('quasar_overrides') or []})
    trace = {}
    account_overrides, account_name = account_layer(runtime, device)
    try:
        body = compose(common, platform_patch, state.get('quasar_overrides'),
                       runtime, device, trace=trace, platform_label=platform_name,
                       account_overrides=account_overrides, account_name=account_name)
    except Exception as exc:
        return web.json_response({'error': str(exc) or 'ошибка сборки конфига',
                                  'platform': platform_name})
    return web.json_response({'legacy': False, 'platform': platform_name,
                              'config': body, 'origins': trace,
                              'warnings': poll_period_warnings(body),
                              'overrides': state.get('quasar_overrides') or []})
