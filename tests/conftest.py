"""Общие помощники и фикстуры тестов (RF-01).

Раньше фикстуры жили в тест-модулях и импортировались друг из друга
( listen — из test_emulator_runtime, types — из test_voice_pipeline и т.п.).
Теперь они здесь; pytest автоматически видит фикстуру `system`, функции
импортируются явно (`from tests.conftest import listen`).
Содержимое перенесено без изменений.
"""
import asyncio
import datetime
import os
import struct
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

from server_emulator.schema import SetupRequired, load_type, schemas_ready

ROOT = Path(__file__).resolve().parent.parent

_SCHEMAS = ROOT / 'server_emulator' / 'schemas'

# Протокола (schemas/*.pb) в репозитории нет: его извлекает protocol_import из libquasar_daemons.so или zip.
# Нет схем, но задан ALICA_PROTOCOL_SOURCE — собираем их один раз; иначе тесты, зависящие от протокола, пропускаются.
SKIP_REASON = ('нет протокола Алисы (schemas/*.pb): задайте ALICA_PROTOCOL_SOURCE=<libquasar_daemons.so или zip> '
               'или выполните python -m server_emulator.protocol_import <файл>')
if not schemas_ready() and os.environ.get('ALICA_PROTOCOL_SOURCE'):
    from server_emulator import protocol_import
    protocol_import.import_source(os.environ['ALICA_PROTOCOL_SOURCE'])
SCHEMAS_READY = schemas_ready()

# Модули, которым нужен протокол (WS/AAPI, сборка директив, фикстуры дескрипторов): без схем пропускаются.
# Остальные модули от схем не зависят и идут всегда; test_protocol_import проверяет сам режим «без схем».
PROTOCOL_MODULES = frozenset("""
test_agent test_agent_ops test_agent_voice test_alarms test_animations test_async_reply test_builtin_scenarios
test_cross_device test_device_resolver test_dialog_transport test_emulator_and_pcm test_emulator_fixtures
test_emulator_identity test_emulator_runtime test_executor test_extension_actions test_extension_catalogs
test_extensions_manager test_led_emotion test_light test_mcp_hub test_miss_journal test_normalize test_online_voice
test_probe test_quasar_backend test_registry_migration test_response_cards_actions test_routines test_scenario_dialog
test_scenario_edit test_scenario_golden test_scenario_json_power test_scenario_script test_scenario_voice test_sdk
test_simple_commands test_speech_trace test_standard_replies test_template_triggers test_timers test_timesync_scenario
test_voice_e2e test_voice_failures test_voice_pipeline test_weather test_ws_section
""".split())


def pytest_collection_modifyitems(config, items):
    if SCHEMAS_READY:
        return
    skip = pytest.mark.skip(reason=SKIP_REASON)
    for item in items:
        if item.module.__name__.rsplit('.', 1)[-1] in PROTOCOL_MODULES:
            item.add_marker(skip)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    """Модуль, которому протокол нужен уже при импорте, без схем пропускается, а не падает."""
    outcome = yield
    report = outcome.get_result()
    if report.failed and not SCHEMAS_READY and 'SetupRequired' in str(report.longrepr):
        report.outcome = 'skipped'
        report.longrepr = (str(getattr(collector, 'path', collector.nodeid)), 0, 'Skipped: ' + SKIP_REASON)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Новый тест вне PROTOCOL_MODULES, упавший на SetupRequired, — пропуск, а не ошибка."""
    outcome = yield
    report = outcome.get_result()
    if (report.failed and not SCHEMAS_READY and call.excinfo is not None
            and isinstance(call.excinfo.value, SetupRequired)):
        report.outcome = 'skipped'
        report.longrepr = (str(item.path), 0, 'Skipped: ' + SKIP_REASON)


async def listen(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    return runner, 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])


async def run_app(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


def types():
    return (load_type(_SCHEMAS, 'alice-api.pb', 'NAlice.NAliceApi.TClientMessage'),
            load_type(_SCHEMAS, 'alice-api.pb', 'NAlice.NAliceApi.TServerMessage'))


def page(flags, packet, seq):
    return b'OggS\0' + bytes([flags]) + struct.pack('<QII', 960 if seq else 0, 123, seq) + b'\0' * 4 + bytes([1, len(packet)]) + packet


async def _open_session(runtime, url, text_phrase, follow_up=None):
    client_type, server_type = types()
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-T', 'user_id': 'owner',
                               'device_ids': ['dev-t'], 'profile': {'aapi_model': True}})
    sync = client_type()
    sync.Event.Header.MessageId = "sync"
    sync.Event.SynchronizeStateRequest.Application.DeviceId = "dev-t"
    received = []
    async with ClientSession() as client:
        async with client.ws_connect(url + "/uni.ws") as ws:
            await ws.send_bytes(b"AAPI" + sync.SerializeToString())
            await asyncio.wait_for(ws.receive(), 3)
            async def send_phrase(phrase):
                message = client_type()
                message.Event.Header.MessageId = "m-" + phrase[:6]
                message.Event.TextInput.Header.RequestId = "rq"
                message.Event.TextInput.Header.DialogId = "dg1"
                message.Event.TextInput.Request.Event.Text = phrase
                await ws.send_bytes(b"AAPI" + message.SerializeToString())
                for _ in range(3):
                    packet = await asyncio.wait_for(ws.receive(), 3)
                    if packet.data.startswith(b"AAPI"):
                        reply = server_type.FromString(packet.data[4:])
                        received.append(reply)
                        if reply.Event.WhichOneof("Event") == "AliceResponse":
                            return reply
            first = await send_phrase(text_phrase)
            if follow_up:
                return first, await send_phrase(follow_up), ws
            return first, None, ws


CTX = {"input": {"request_id": "req-1", "message_id": "msg-1", "dialog_id": "dlg-x"},
       "clock": {"now": datetime.datetime(2026, 10, 23, 12, 0)}}


def step(sid, op, **kwargs):
    return dict(id=sid, op=op, **kwargs)
