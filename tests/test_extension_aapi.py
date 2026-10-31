"""TASK-059: обработчики AAPI и отправка на устройство из расширений (sdk 1.3.0).

Расширение ставится из локальной папки (режим разработки), aapi/*.py регистрирует
обработчики через sdk.aapi_handler / sdk.on_event. Проверяется регистрация с источником
extension:<id>, ответ колонке, permissions.aapi, снятие при выключении, наблюдатели и
send_push/send_directive.
"""
import asyncio
import json
from pathlib import Path

import pytest

from server_emulator import aapi_handlers, sdk
from server_emulator.extensions.manager import ExtensionManager
from server_emulator.runtime import ROOT, Runtime
from server_emulator.schema import load_type
from server_emulator.sdk import SDK_VERSION

SERVER = load_type(ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
SN = "1928374650"

MANIFEST = {
    "id": "greeter", "name": "Greeter", "version": "1.0.0",
    "description": "Обработчик AAPI из расширения.",
    "author": "Ivan <ivan@example.com>", "license": "MIT",
    "requires": {"emulator": ">=0", "sdk": f">={SDK_VERSION} <2.0.0"},
    "permissions": {"aapi": ["@@mm_custom"]},
}

GREET = '''
from server_emulator import sdk

@sdk.aapi_handler('@@mm_custom')
async def greet(event, ctx):
    await ctx.reply(sdk.say('привет от расширения'))
    return True

@sdk.on_event('@@mm_custom')
async def watch(event, ctx):
    seen.append(event['name'])
'''


def ext_folder(tmp_path, manifest_data=None, aapi=GREET):
    """Локальная папка расширения (режим разработки) с aapi/greet.py."""
    root = tmp_path / "greeter-ext"
    (root / "aapi").mkdir(parents=True)
    (root / "extension.json").write_text(
        json.dumps(manifest_data or MANIFEST, ensure_ascii=False), encoding="utf-8")
    (root / "aapi" / "greet.py").write_text(aapi, encoding="utf-8")
    return root


async def make_runtime(tmp_path, manifest_data=None, aapi=GREET):
    runtime = Runtime(tmp_path)
    sdk.bind_runtime(runtime)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'Alex'})
    runtime.identities.mutate({'operation': 'device', 'sn': SN, 'user_id': 'owner', 'device_ids': []})
    await runtime.extensions.install({"url": str(ext_folder(tmp_path, manifest_data, aapi))}, confirm=True)
    await runtime.extensions.enable("greeter")
    return runtime


def incoming_callback(name="@mm_custom_placeholder"):
    """TClientMessage: служебный server_action (Type=6), как в voice.py."""
    client = load_type(ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TClientMessage")()
    client.Event.Header.MessageId = "msg-1"
    client.Event.TextInput.Header.RequestId = "req-1"
    event = client.Event.TextInput.Request.Event
    event.Type = 6
    event.Name = name
    return client


async def dispatch_callback(runtime, incoming, name="@@mm_custom"):
    """Ветка server_action voice.py в объёме, нужном тесту: наблюдатели, обработчики
    расширений, сценарии. Возвращает (handled, sent)."""
    conversation = runtime.voice_conversations.get(SN) if hasattr(runtime, "voice_conversations") else None
    conversation = conversation or _ConversationStub(runtime)
    sent = []

    async def send(data):
        sent.append(data)

    conversation.send = send
    aapi_handlers.notify_observers(conversation, incoming, name)
    handled = False
    for handler in aapi_handlers.handlers(name, runtime.settings):
        if await handler(conversation, incoming, name):
            handled = True
            break
    if not handled:
        report = await runtime.dialog.handle_event(name, {"input": {"message_id": "msg-1", "sn": SN}},
                                                   "dlg-1", send)
        handled = bool(report.get("handled"))
    await asyncio.sleep(0.05)  # наблюдатели — фоновые задачи
    return handled, sent


class _ConversationStub:
    """Минимальный объект Conversation для адаптера sdk: runtime, trace_context, send."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.trace_context = {"sn": SN, "device_id": ""}
        self.send = lambda data: asyncio.sleep(0)


async def test_handler_registered_with_extension_source(tmp_path):
    runtime = await make_runtime(tmp_path)
    try:
        entries = [e for e in aapi_handlers.registry(runtime.settings)
                   if e["source"] == "extension:greeter"]
        assert entries and entries[0]["trigger"] == "@@mm_custom"
        assert entries[0]["name"] == "greet" and entries[0]["enabled"]
    finally:
        await runtime.close()


async def test_handler_replies_to_column(tmp_path):
    """Обработчик вызывается на своё событие и отвечает колонке через ctx.reply."""
    runtime = await make_runtime(tmp_path)
    try:
        handled, sent = await dispatch_callback(runtime, incoming_callback("@@mm_custom"))
        assert handled and sent
        message = SERVER.FromString(sent[0][4:])
        response = message.Event.AliceResponse
        assert response.Response.Cards[0].Text == "привет от расширения"
    finally:
        await runtime.close()


async def test_without_permission_not_registered(tmp_path):
    """Событие не объявлено в permissions.aapi — обработчик не регистрируется (warning)."""
    manifest = {**MANIFEST, "permissions": {}}
    runtime = await make_runtime(tmp_path, manifest_data=manifest)
    try:
        assert not [e for e in aapi_handlers.registry(runtime.settings)
                    if e["source"] == "extension:greeter"]
        assert any("permissions.aapi" in w["error"] for w in runtime.extensions.warnings)
    finally:
        await runtime.close()


async def test_disable_removes_handlers(tmp_path):
    """Выключение расширения снимает его обработчики из реестра."""
    runtime = await make_runtime(tmp_path)
    try:
        assert aapi_handlers.handlers("@@mm_custom", runtime.settings)
        await runtime.extensions.disable("greeter")
        assert not aapi_handlers.handlers("@@mm_custom", runtime.settings)
        assert not [e for e in aapi_handlers.registry(runtime.settings)
                    if e["source"].startswith("extension:")]
    finally:
        await runtime.close()


async def test_observer_sees_event_without_participating(tmp_path):
    """on_event вызывается для каждого события фоном и не отвечает колонке."""
    marker = tmp_path / "seen.txt"
    manifest = {**MANIFEST, "permissions": {"aapi": ["@@mm_custom"]},
                "settings": {"defaults": {"marker": str(marker)}}}
    observer = (
        "from pathlib import Path\n"
        "from server_emulator import sdk\n"
        "\n"
        "@sdk.on_event('@@mm_custom')\n"
        "async def watch(event, ctx):\n"
        "    Path(ctx.extension.get('marker')).write_text(event['name'], encoding='utf-8')\n")
    runtime = await make_runtime(tmp_path, manifest_data=manifest, aapi=observer)
    try:
        await dispatch_callback(runtime, incoming_callback("@@mm_custom"))
        assert marker.read_text(encoding="utf-8") == "@@mm_custom"
    finally:
        await runtime.close()


async def test_send_push_reaches_session_and_offline_raises(tmp_path):
    """sdk.send_push: push доходит до открытой WS-сессии; не в сети — SDKError."""
    runtime = await make_runtime(tmp_path)
    try:
        sent = []

        async def send(data):
            sent.append(data)

        runtime.sessions["conn-1"] = {"connection_id": "conn-1", "device_sn": SN,
                                      "user_id": "owner", "send": send}
        result = await sdk.send_push(SN, "sound_set_level", {"level": 5})
        assert result["delivery"] == "sent" and sent
        message = SERVER.FromString(sent[0][4:])
        assert message.Event.Push.Directives[0].Name == "sound_set_level"
        with pytest.raises(sdk.SDKError, match="не в сети"):
            await sdk.send_push("offline-sn", "sound_louder")
        with pytest.raises(sdk.SDKError):
            await sdk.send_push(SN, "not_in_catalog")
    finally:
        await runtime.close()


async def test_send_directive_from_scenario_context(tmp_path):
    """ctx.send_directive (сценарий) и sdk.send_directive: произвольная директива Push."""
    runtime = await make_runtime(tmp_path)
    try:
        sent = []

        async def send(data):
            sent.append(data)

        runtime.sessions["conn-1"] = {"connection_id": "conn-1", "device_sn": SN,
                                      "user_id": "owner", "send": send}
        from server_emulator.sdk import ScenarioContext
        await ScenarioContext(runtime).send_directive(SN, "custom_directive", {"value": 1})
        message = SERVER.FromString(sent[0][4:])
        directive = message.Event.Push.Directives[0]
        assert directive.Name == "custom_directive" and directive.Payload["value"] == 1
        with pytest.raises(sdk.SDKError, match="не в сети"):
            await sdk.send_directive("offline-sn", "custom_directive")
    finally:
        await runtime.close()
