"""TASK-058: вкладка «WS» — реестр обработчиков, колбэки → поставщики данных, отправка.

Колбэки проверяются тем же маршрутом, что и voice.py (aapi_handlers.route); поставщики —
сценарии с триггером kind: provide, пользовательский перекрывает встроенный; без поставщика —
умолчание колбэка (тихое подтверждение). Реестр — источник builtin и счётчики.
"""
import asyncio
import json
from pathlib import Path

import pytest

from server_emulator import aapi_handlers
from server_emulator.runtime import ROOT, Runtime
from server_emulator.schema import load_type

SN = "1928374650"

SERVER = load_type(ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")


def make_runtime(tmp_path):
    """Колонка с владельцем — как в test_timesync_scenario.make_runtime."""
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'Alex'})
    runtime.identities.mutate({'operation': 'device', 'sn': SN, 'user_id': 'owner', 'device_ids': []})
    return runtime


def service_ctx(runtime, purpose='', callback='', name='@@mm_typed_callback'):
    """Контекст колбэка — те же поля, что собирает voice.py (service_event_fields)."""
    return {'input': {'message_id': 'msg-1', 'request_id': 'req-1', 'dialog_id': 'dlg-1',
                      'sn': SN, 'text': '', 'event_name': name, 'purpose': purpose,
                      'callback': callback, 'frame': '', 'disable_output_speech': True,
                      'timezone': 'Asia/Tashkent'},
            'clock': {'now': __import__('datetime').datetime(2026, 11, 1, 12, 0)}, 'sequence': 1}


async def dispatch(runtime, ctx):
    """Тот же путь, что в ветке server_action в voice.py: маршрут колбэка → handle_event,
    без поставщика — умолчание (тихое подтверждение); счётчики события."""
    name = ctx['input']['event_name']
    provide = aapi_handlers.route(name, {k: ctx['input'].get(k) for k in ('callback', 'purpose', 'frame')})
    aapi_handlers.count(name)
    sent = []

    async def send(data):
        sent.append(data)

    handled = False
    if provide:
        report = await runtime.dialog.handle_event(provide, ctx, 'dlg-1', send)
        handled = bool(report.get('handled'))
    if not handled:
        report = await runtime.dialog.handle_event(name, ctx, 'dlg-1', send)
        handled = bool(report.get('handled'))
    if handled:
        aapi_handlers.count(name, handled=True)
        return report, sent, provide
    if provide:  # умолчание колбэка — тихое подтверждение (Dialog.quiet_ack)
        message = await runtime.dialog.quiet_ack(ctx)
        await send(b"AAPI" + message.SerializeToString())
        aapi_handlers.count(name, handled=True)
        return {'handled': True, 'scenario': None, 'reason': 'callback_default_ack'}, sent, provide
    runtime.callback_misses.note(name, sn=ctx['input']['sn'],
                                 example=json.dumps(ctx['input'], ensure_ascii=False, default=str))
    return report, sent, provide


def test_route_table_maps_callbacks():
    """Маршруты: информеры/timesync по фильтрам, прочие — тихое подтверждение, таймер/агент — свои события."""
    fields = {'callback': 'NAlice.TGetInformersTypedCallback', 'purpose': '', 'frame': ''}
    assert aapi_handlers.route('@@mm_typed_callback', fields) == 'informers.requested'
    fields = {'callback': '', 'purpose': 'get_informers', 'frame': ''}
    assert aapi_handlers.route('@@mm_typed_callback', fields) == 'informers.requested'
    fields = {'callback': '', 'purpose': 'sync_timezone', 'frame': ''}
    assert aapi_handlers.route('@@mm_typed_callback', fields) == 'timesync.requested'
    fields = {'callback': '', 'purpose': 'collect_upper_shutter', 'frame': ''}
    assert aapi_handlers.route('@@mm_typed_callback', fields) == 'callbacks.ack'
    assert aapi_handlers.route('@@timer_fired', None) == 'timer_fired.requested'
    assert aapi_handlers.route('@@unknown_event', None) is None


def test_registry_lists_builtin_handlers(tmp_path):
    """Реестр: встроенные обработчики с источником builtin, условие включения текстом."""
    runtime = make_runtime(tmp_path)
    entries = aapi_handlers.registry(runtime.settings)
    builtin = [e for e in entries if e['source'] == 'builtin']
    assert builtin and all(e['source'] == 'builtin' for e in builtin)
    unclaimed = next(e for e in entries if e['name'] == 'on_unclaimed')
    assert 'привяз' in unclaimed['enabled_text'] and unclaimed['trigger'] in ('TextInput', 'VoiceInput')


async def test_counters_grow_after_event(tmp_path):
    """Счётчики в памяти: колбэк пришёл, обработан, время последнего."""
    runtime = make_runtime(tmp_path)
    try:
        before = aapi_handlers.counters('@@mm_typed_callback')['seen']
        report, sent, provide = await dispatch(runtime, service_ctx(runtime, purpose='sync_timezone'))
        assert report['handled'] and report['scenario'] == 'system.timesync'
        after = aapi_handlers.counters('@@mm_typed_callback')
        assert after['seen'] == before + 1 and after['handled'] >= 1 and after['last'] > 0
    finally:
        await runtime.close()


async def test_provider_builds_the_reply(tmp_path):
    """Ответ колонке собирает сценарий-поставщик: колбэк timesync → директива set_timezone
    (прежнее поведение; байт-в-байт фиксирует эталон tests/golden/scenarios.json)."""
    runtime = make_runtime(tmp_path)
    try:
        report, sent, provide = await dispatch(runtime, service_ctx(runtime, purpose='sync_timezone'))
        assert provide == 'timesync.requested'
        assert report['handled'] and report['scenario'] == 'system.timesync' and sent
        message = SERVER.FromString(sent[0][4:])
        response = message.Event.AliceResponse
        assert 'set_timezone' in [d.Name for d in response.Response.Directives]
    finally:
        await runtime.close()


async def test_user_provider_overrides_builtin(tmp_path):
    """Свой сценарий-поставщик с тем же событием перекрывает встроенный; колбэк тот же."""
    runtime = make_runtime(tmp_path)
    try:
        settings = {**runtime.settings, 'scenarios': [{
            'format_version': 2, 'id': 'user.timesync', 'version': '1.0.0', 'category': 'custom',
            'title': {'ru': 'Свой timesync'}, 'description': {'ru': ''}, 'priority': 5,
            'triggers': [{'kind': 'provide', 'event': 'timesync.requested'}],
            'flow': [{'id': 'reply', 'op': 'compose', 'reply': {'text': 'свой ответ', 'envelope': 'service'}},
                     {'id': 'done', 'op': 'finish'}]}]}
        async with runtime.lock:
            runtime.write('settings.json', settings)
            runtime.settings = runtime.settings_service.load()
        report, sent, provide = await dispatch(runtime, service_ctx(runtime, purpose='sync_timezone'))
        assert report['handled'] and report['scenario'] == 'user.timesync'
        response = SERVER.FromString(sent[0][4:]).Event.AliceResponse
        assert response.Response.Cards[0].Text == 'свой ответ'
    finally:
        await runtime.close()


async def test_no_provider_gets_default_ack(tmp_path):
    """Нет поставщика callbacks.ack (сценарий выключен) — умолчание колбэка: тихое подтверждение."""
    runtime = make_runtime(tmp_path)
    try:
        # у встроенного поставщика ставим enabled=False — каталог его не отдаёт
        runtime.write('settings.json', {**runtime.settings, 'builtin_disabled': ['system.service_ack']})
        runtime.settings = runtime.settings_service.load()
        report, sent, provide = await dispatch(runtime, service_ctx(runtime, purpose='collect_upper_shutter'))
        # поставщика нет — умолчание колбэка: тот же тихий ответ, что собирал system.service_ack
        assert provide == 'callbacks.ack' and report['reason'] == 'callback_default_ack' and len(sent) == 1
        response = SERVER.FromString(sent[0][4:]).Event.AliceResponse
        assert not response.VoiceResponse.HasVoiceResponse and not response.Response.Cards
    finally:
        await runtime.close()


async def test_unknown_callback_is_miss(tmp_path):
    """Колбэк без маршрута и сценария — «Без ответа» (как раньше)."""
    runtime = make_runtime(tmp_path)
    try:
        report, sent, provide = await dispatch(runtime, service_ctx(runtime, name='@@brand_new_event'))
        assert provide is None and not report['handled'] and not sent
        assert '@@brand_new_event' in runtime.callback_misses.items
    finally:
        await runtime.close()


async def test_ws_handlers_api(tmp_path):
    """GET api/ws_handlers: реестр с источниками и счётчиками; колбэки с поставщиками."""
    runtime = make_runtime(tmp_path)
    try:
        from server_emulator.ui.api.ws_handlers import ws_handlers as handler

        class Request:
            query = {}

        response = await handler(Request(), runtime, {})
        import json as json_module
        body = json_module.loads(response.body.decode())
        assert any(h['source'] == 'builtin' for h in body['handlers'])
        assert all('counters' in h for h in body['handlers'])
        informers = next(c for c in body['callbacks'] if c['provide'] == 'informers.requested')
        assert informers['provider']['id'] == 'informers.weather.widget'
        assert informers['overridden'] is False
        ack = next(c for c in body['callbacks'] if c['provide'] == 'callbacks.ack')
        assert ack['provider']['id'] == 'system.service_ack'
        # пользовательский поставщик перекрывает встроенный — overridden
        async with runtime.lock:
            runtime.write('settings.json', {**runtime.settings, 'scenarios': [{
                'format_version': 2, 'id': 'user.ack', 'version': '1.0.0', 'category': 'custom',
                'title': {'ru': 'Свой ack'}, 'description': {'ru': ''},
                'triggers': [{'kind': 'provide', 'event': 'callbacks.ack'}],
                'flow': [{'id': 'reply', 'op': 'compose', 'reply': {'text': '', 'card': False}},
                         {'id': 'done', 'op': 'finish'}]}]})
            runtime.settings = runtime.settings_service.load()
        response2 = await handler(Request(), runtime, {})
        body2 = json_module.loads(response2.body.decode())
        ack2 = next(c for c in body2['callbacks'] if c['provide'] == 'callbacks.ack')
        assert ack2['provider']['id'] == 'user.ack' and ack2['overridden'] is True
    finally:
        await runtime.close()
