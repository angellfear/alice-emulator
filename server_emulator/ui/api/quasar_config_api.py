"""Домены quasar_platforms, quasar_rules, quasar_composed и quasar_source: конфиг Quasar.

quasar_platforms — обзор раздела «Конфиг колонок»: платформы встроенного шаблона, платформы
с заменами, счётчики; GET ?builtin=<платформа> — встроенный шаблон (только чтение),
GET ?compose=<платформа> — итог без устройства (шаблон → общие → платформа) с происхождением;
POST {action: create|rename, platform, to?} и DELETE ?platform= — файлы замен платформ.
quasar_rules — замены слоя: GET ?layer=common|platform:<имя>|device:<SN> → {rules, inherited},
PUT {layer, rules} — проверка и сохранение списка целиком; сохранение замен и источника
шлёт push_update_config колонкам слоя с живой сессией (pushed — их SN). quasar_composed — итоговый
конфиг устройства с происхождением значений. quasar_source — источник конфига устройства:
generated | direct | custom, генерация образца и получение с оригинального сервера по
запомненным в памяти заголовкам колонки. Guard и маппинг ошибок — ui/api/__init__.py.
"""
import copy

from aiohttp import web

from ...quasar_config import (count_keys, device_platform, generated_base, generated_platforms,
                              poll_period_warnings)
from ...quasar_replace import effective_direct_replace, preview_replacements, validate_rules
from ...quasar_rules import compose_config
from ...quasar_rules import validate_rules as validate_quasar_rules
from ...upstream import UpstreamError
from ...upstream.forward import decoded_json

from . import route


def _device(runtime, sn):
    device = next((d for d in runtime.identities.data['devices'] if d.get('sn') == sn), None)
    if device is None:
        raise ValueError('Устройство не найдено в реестре')
    return device


def _sync_body(runtime, device):
    """Ответ get_sync_info, который собрал бы эмулятор (для action: generate)."""
    state = device.get('state') or {}
    body = compose_config(runtime, device_platform(device), device,
                          device_rules=state.get('quasar_overrides'))
    result = {'status': 'ok', **body}
    security = runtime.quasar.glagol_security_with_autogen(device)  # как в ответе колонке
    if security:
        # образец показывается в UI: приватный ключ — маской, как секреты в остальном API
        security = {**security, 'server_private_key': runtime.masked_secret(security['server_private_key'])}
        result['glagol'] = {'security': security}
    group = runtime.quasar.group_payload(device)
    if group:
        result['group'] = group
    return result


async def _push_config(runtime, sns):
    """push_update_config колонкам с живой WS-сессией: колонка сама запросит
    get_sync_info. Возвращает SN, которым push доставлен."""
    pushed = []
    for sn in sns:
        item = await runtime.push_action(sn, 'push_update_config', {})
        if item.get('delivery') == 'sent':
            pushed.append(sn)
    return pushed


def _layer_devices(runtime, kind, name):
    """SN устройств, чей конфиг меняет слой."""
    devices = runtime.identities.data['devices']
    if kind == 'device':
        return [name]
    if kind == 'platform':
        return [d['sn'] for d in devices if device_platform(d) == name]
    return [d['sn'] for d in devices]


@route("quasar_platforms", methods=("GET", "POST", "DELETE"))
async def quasar_platforms(request, runtime, app):
    store = runtime.quasar_rules
    if request.method == 'GET':
        platform = request.query.get('builtin')
        if platform is not None:  # встроенный шаблон; имя вне quasar_defaults/platforms — общий дефолт
            body = generated_base(platform)
            return web.json_response({'platform': platform, 'config': body, 'keys': count_keys(body)})
        name = request.query.get('compose')
        if name is not None:  # итог без устройства: шаблон → общие → замены платформы
            trace = {}
            body = compose_config(runtime, name, None, trace=trace, render=False)
            return web.json_response({'platform': name, 'config': body, 'origins': trace})
        return web.json_response({
            'builtin_platforms': generated_platforms(),
            'common_rules': len(store.layer_rules('common')[0]),
            'platforms': [{'platform': p, 'rules': len(store.layer_rules(p)[0])}
                          for p in store.platforms()],
            'devices': len(runtime.identities.data['devices'])})
    if request.method == 'POST':
        value = await request.json()
        action, name = value.get('action'), value.get('platform', '')
        async with runtime.lock:
            if action == 'create':
                store.create_platform(name)
            elif action == 'rename':
                store.rename_platform(name, value.get('to', ''))
                name = value.get('to')
            else:
                raise ValueError('action: create | rename')
        return web.json_response({'platform': name})
    if request.method == 'DELETE':
        async with runtime.lock:
            removed = store.remove_platform(request.query.get('platform', ''))
        if not removed:
            raise ValueError('Платформа не найдена')
        return web.json_response({'removed': request.query['platform']})
    raise web.HTTPMethodNotAllowed(request.method, ['GET', 'POST', 'DELETE'])


def _layer(runtime, layer):
    """('common'|'platform'|'device', имя) по строке слоя."""
    if layer == 'common':
        return 'common', 'common'
    kind, _, name = str(layer or '').partition(':')
    if kind == 'platform' and name:
        runtime.quasar_rules.check_platform(name)
        return 'platform', name
    if kind == 'device' and name:
        return 'device', name
    raise ValueError('layer: common | platform:<имя> | device:<SN>')


@route("quasar_rules", methods=("GET", "PUT"))
async def quasar_rules(request, runtime, app):
    """Замены слоя: поставляемый набор ⊕ правки state (флаги shipped/modified, снятые
    правила — removed с исходником base для «сбросить к поставляемому»). inherited —
    правила того же слоя из расширений (только чтение)."""
    store = runtime.quasar_rules
    if request.method == 'GET':
        kind, name = _layer(runtime, request.query.get('layer'))
        if kind == 'device':
            device = _device(runtime, name)
            platform = device_platform(device)
            rules = (device.get('state') or {}).get('quasar_overrides') or []
            return web.json_response({'layer': f'device:{name}', 'platform': platform,
                                      'rules': rules, 'inherited': []})
        inherited = [{'origin': ext_id, 'rules': rules} for ext_id, rules in store.extension_rules(name)]
        return web.json_response({'layer': request.query['layer'],
                                  'rules': store.layer_rules(name)[1],
                                  'inherited': inherited})
    value = await request.json()
    kind, name = _layer(runtime, value.get('layer'))
    rules = validate_quasar_rules(value.get('rules'))
    async with runtime.lock:
        if kind == 'device':
            device = _device(runtime, name)
            state = copy.deepcopy(device.get('state') or {})
            if rules:
                state['quasar_overrides'] = rules
            else:
                state.pop('quasar_overrides', None)
            runtime.identities.mutate({'operation': 'state', 'sn': name, 'state': state})
        else:
            store.save(name, rules)
    pushed = await _push_config(runtime, _layer_devices(runtime, kind, name))
    return web.json_response({'saved': value.get('layer'), 'rules': rules, 'pushed': pushed})


@route("quasar_composed", methods=("GET",))
async def quasar_composed(request, runtime, app):
    # Итоговый конфиг устройства после всех слоёв + происхождение каждого значения.
    if request.method != 'GET':
        raise web.HTTPMethodNotAllowed(request.method, ['GET'])
    device = _device(runtime, request.query.get('device', ''))
    state = device.get('state') or {}
    platform = device_platform(device)
    trace = {}
    errors = []
    render = request.query.get('raw') != '1'  # raw=1 — параметры {{ключ}}, db/run маркерами
    try:
        body = compose_config(runtime, platform, device, device_rules=state.get('quasar_overrides'),
                              trace=trace, render=render, errors=errors)
    except Exception as exc:
        return web.json_response({'error': str(exc) or 'ошибка сборки конфига', 'platform': platform})
    return web.json_response({'platform': platform, 'config': body,
                              'origins': trace, 'warnings': poll_period_warnings(body),
                              'errors': errors})


@route("quasar_source", methods=("GET", "PUT", "POST"))
async def quasar_source(request, runtime, app):
    """Источник конфига устройства.

    GET ?sn= → {mode, direct_last, custom, replace, transparent, can_fetch}; PUT {sn, mode?, custom?,
    replace?, transparent?} — сохранение режима, своего ответа, замен regexp «Напрямую» и/или
    галочки «И в прозрачном маршруте» (bool) в state устройства; сохранённые replace сразу
    прогоняются по последнему оригинальному ответу: в direct_last обновляются body_sent/replaced
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
        mode = state.get('quasar_source') or 'generated'
        return web.json_response({'mode': mode,
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
                # правила прогоняются по сохранённому оригиналу сразу — «После замен»
                # в UI обновляется без запроса колонки; прошлая отметка (реальная или превью) не актуальна
                last = state.get('quasar_direct_last')
                if isinstance(last, dict) and isinstance(last.get('body'), dict):
                    for key in ('body_sent', 'replaced', 'preview', 'replace_error'):
                        last.pop(key, None)
                    # превью — по полному списку замен: поставляемая защита + правила устройства
                    preview = preview_replacements(effective_direct_replace(rules), last['body'])
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
        return web.json_response({'saved': sn, 'pushed': await _push_config(runtime, [sn])})
    action = value.get('action')
    if action == 'generate':
        return web.json_response(_sync_body(runtime, device))
    if action == 'push':  # принудительный перезапрос конфига — колонка сделает get_sync_info
        item = await runtime.push_action(sn, 'push_update_config', {})
        if item.get('delivery') == 'no_session':
            return web.json_response({'error': 'push_no_session',
                                      'hint': 'Нет живой WS-сессии устройства — перезапрос не отправлен'}, status=409)
        return web.json_response(item)
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
