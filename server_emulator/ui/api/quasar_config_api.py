"""Домены quasar_platforms, quasar_composed и quasar_source: слои Quasar-конфига (RF-03).

quasar_platforms — редактор «Переопределение» sync-конфигов: общий common
(тело, не удаляется) + платформы-патчи (чистый merge-patch в файлах) +
templates.json (токены/инлайн-узлы/источники значений). quasar_composed —
итоговый конфиг устройства после всех слоёв + происхождение каждого значения
(только чтение). quasar_source — источник конфига устройства (TASK-052):
generated | direct | custom, генерация образца и получение с оригинального
сервера по запомненным в памяти заголовкам колонки. Guard и маппинг ошибок
обеспечивает диспетчер реестра ui/api/__init__.py.
"""
import copy

from aiohttp import web

from ...quasar_config import account_layer, compose, poll_period_warnings
from ...quasar_replace import preview_replacements, validate_rules
from ...upstream import UpstreamError, forward_headers
from ...upstream.forward import decoded_json

from . import route


def _sync_body(runtime, device):
    """Ответ get_sync_info, который собрал бы compose (для action: generate)."""
    state = device.get('state') or {}
    common, platform_patch = runtime.quasar_platforms.resolve_files(device)
    if common is None:
        raise ValueError('Устройство на legacy-конфиге state.quasar — генерация недоступна')
    account_overrides, account_name = account_layer(runtime, device)
    body = compose(common, platform_patch, state.get('quasar_overrides'),
                   runtime, device, account_overrides=account_overrides, account_name=account_name)
    result = {'status': 'ok', **body}
    group = runtime.quasar.group_payload(device)
    if group:
        result['group'] = group
    return result


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


@route("quasar_source", methods=("GET", "PUT", "POST"))
async def quasar_source(request, runtime, app):
    """Источник конфига устройства (TASK-052).

    GET ?sn= → {mode, direct_last, custom, replace, transparent, can_fetch}; PUT {sn, mode?, custom?,
    replace?, transparent?} — сохранение режима, своего ответа, замен regexp «Напрямую» (TASK-063) и/или
    галочки «И в прозрачном маршруте» (TASK-065, bool) в state устройства; сохранённые replace сразу
    прогоняются по последнему оригинальному ответу (TASK-103): в direct_last обновляются body_sent/replaced
    с отметкой preview или replace_error, до ближайшего запроса колонки
    (под runtime.lock; custom — JSON-объект с объектом config; replace — [{enabled, pattern, replace}],
    невалидный regex — 400 с номером строки, ничего не сохраняется); POST {sn, action} — generate (ответ
    compose, ничего не сохраняет) и fetch (запрос оригинальному серверу с
    запомненными заголовками колонки; обмен — в «Журнал», ничего не сохраняет).
    """
    def device_of(identifier):
        device = next((d for d in runtime.identities.data['devices']
                       if d.get('sn') == identifier), None)
        if device is None:
            raise ValueError('Устройство не найдено в реестре')
        return device

    if request.method == 'GET':
        device = device_of(request.query.get('sn', ''))
        state = device.get('state') or {}
        return web.json_response({'mode': state.get('quasar_source') or 'generated',
                                  'direct_last': state.get('quasar_direct_last'),
                                  'custom': state.get('quasar_custom'),
                                  'replace': state.get('quasar_direct_replace') or [],
                                  'transparent': state.get('quasar_direct_transparent') is True,
                                  'can_fetch': device['sn'] in runtime.quasar_sync_headers})
    value = await request.json()
    device = device_of(value.get('sn', ''))
    sn = device['sn']
    if request.method == 'PUT':
        mode = value.get('mode')
        if mode is not None and mode not in ('generated', 'direct', 'custom'):
            raise ValueError('Источник: generated | direct | custom')
        custom = value.get('custom')
        if 'custom' in value and custom is not None and (
                not isinstance(custom, dict) or not isinstance(custom.get('config'), dict)):
            raise ValueError('Свой ответ: JSON-объект с объектом config')
        rules = validate_rules(value['replace']) if 'replace' in value else None
        transparent = value.get('transparent')
        if 'transparent' in value and not isinstance(transparent, bool):
            raise ValueError('transparent: true | false')
        async with runtime.lock:
            device = device_of(sn)  # свежая запись: пока ждали lock, state могли переписать
            state = copy.deepcopy(device.get('state') or {})
            if mode == 'generated':
                state.pop('quasar_source', None)  # отсутствие поля — тоже generated
            elif mode is not None:
                state['quasar_source'] = mode
            if 'custom' in value:
                if custom is None:
                    state.pop('quasar_custom', None)
                else:
                    state['quasar_custom'] = custom
            if rules is not None:
                if rules:
                    state['quasar_direct_replace'] = rules
                else:
                    state.pop('quasar_direct_replace', None)
                # TASK-103: правила прогоняются по сохранённому оригиналу сразу — «После замен»
                # в UI обновляется без запроса колонки; прошлая отметка (реальная или превью) не актуальна
                last = state.get('quasar_direct_last')
                if isinstance(last, dict) and isinstance(last.get('body'), dict):
                    for key in ('body_sent', 'replaced', 'preview', 'replace_error'):
                        last.pop(key, None)
                    preview = preview_replacements(rules, last['body'])
                    if preview and 'error' in preview:
                        last['replace_error'] = preview['error']
                    elif preview:
                        last.update(body_sent=preview['body'], replaced=preview['applied'], preview=True)
            if 'transparent' in value:
                if transparent:
                    state['quasar_direct_transparent'] = True
                else:
                    state.pop('quasar_direct_transparent', None)  # отсутствие поля — выключено
            runtime.identities.mutate({'operation': 'state', 'sn': sn, 'state': state})
        return web.json_response({'saved': sn})
    action = value.get('action')
    if action == 'generate':
        return web.json_response(_sync_body(runtime, device))
    if action == 'fetch':
        saved = runtime.quasar_sync_headers.get(sn)
        if not saved:
            return web.json_response(
                {'error': 'quasar_no_sync_headers',
                 'hint': 'Колонка ещё не запрашивала конфиг после запуска сервера — '
                         'дождитесь её опроса или перезагрузите колонку'}, status=409)
        try:
            status, headers, payload = await runtime.upstream.forwarder.http(
                'GET', runtime.upstream.target_for_host('quasar.yandex.net', '/get_sync_info',
                                                        query=saved['query']),
                saved['headers'], b'', journal=runtime.upstream.recording(sn, handled=True),  # обработчик get_sync_info у эмулятора есть
                host='quasar.yandex.net',
                path='/get_sync_info', device_sn=sn)
        except UpstreamError as exc:
            return web.json_response({'error': 'upstream_unavailable', 'detail': str(exc)[:200]}, status=502)
        body = decoded_json(headers, payload) if status == 200 else None
        return web.json_response({'status': status, 'body': body})
    raise ValueError('action: generate | fetch')
