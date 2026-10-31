"""Агент (движок graph) в голосовом тракте: тред колонки, подтверждение «да» раньше сценариев,
scenario_run — эффект сценария без подтверждения."""
import asyncio
import json

from server_emulator import mcp_hub
from server_emulator.runtime import Runtime
from server_emulator.voice import Conversation
from tests.conftest import types


def _responses(sent, server):
    result = []
    for raw in sent:
        if raw.startswith(b"AAPI"):
            message = server.FromString(raw[4:])
            if message.Event.WhichOneof("Event") == "AliceResponse":
                result.append(message.Event.AliceResponse)
    return result


def _home(tmp_path, replies):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True)
    runtime.settings["model"]["stub_text"] = ""
    runtime.identities.mutate({"operation": "user", "id": "owner", "name": "O"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-T", "user_id": "owner",
                               "device_ids": ["dev-t"], "profile": {"aapi_model": True}})
    device = next(d for d in runtime.identities.data["devices"] if d["sn"] == "SN-T")
    runtime.identities.mutate({"operation": "state", "sn": "SN-T",
                               "state": dict(device["state"], light={"кухня": {"on": False}})})
    runtime.settings["device_context"] = {"sn": "SN-T", "user_id": "owner"}
    payloads = []

    async def provider(kind, payload):
        payloads.append(json.loads(json.dumps(payload)))
        return json.dumps({"choices": [{"message": replies[len(payloads) - 1]}], "usage": {}}).encode(), "application/json"
    runtime.provider = provider
    return runtime, payloads


async def _say(conv, client, text, message_id):
    incoming = client()
    incoming.Event.Header.MessageId = message_id
    incoming.Event.TextInput.Request.Event.Text = text
    await conv.receive(incoming, "TextInput")
    await asyncio.wait_for(conv.task, 5)


def _call(name, arguments="{}", call_id="c1", content=""):
    return {"content": content, "tool_calls": [{"id": call_id, "type": "function",
                                                "function": {"name": name, "arguments": arguments}}]}


async def test_voice_confirmation_yes_goes_to_agent_not_scenarios(tmp_path, monkeypatch):
    runtime, payloads = _home(tmp_path, [_call("ha__lock", content="Закрою замок."), {"content": "Закрыла."}])
    mcp_hub.save_config(runtime, {"mcpServers": {"ha": {"type": "http", "url": "http://ha.invalid/mcp"}}})
    runtime.settings["tools"] = [{"name": "ha__lock", "server_id": "ha", "server_tool": "lock", "enabled": True,
                                  "risk": "external", "inputSchema": {"type": "object", "properties": {}}}]
    called = []

    async def request(runtime, server, method, params, timeout=30, server_id=None):
        called.append(params)
        return {"content": [{"type": "text", "text": "locked"}], "isError": False}
    monkeypatch.setattr(mcp_hub, "request", request)
    client, server = types()
    sent = []

    async def send(data):
        sent.append(data)
    conv = Conversation(runtime, server, send, trace_context={"sn": "SN-T"})
    await _say(conv, client, "закрой замок", "m1")
    first = _responses(sent, server)[-1]
    assert "Подтвердите" in first.Response.Cards[0].Text and called == []
    assert runtime.pending_calls[-1]["thread_id"] == "voice:SN-T"
    await _say(conv, client, "да", "m2")
    assert _responses(sent, server)[-1].Response.Cards[0].Text == "Закрыла." and len(called) == 1
    assert not await runtime.agent.has_pending(runtime, "voice:SN-T")
    await conv.shutdown()
    await runtime.close()


async def test_voice_scenario_run_applies_effect_without_confirmation(tmp_path):
    arguments = json.dumps({"text": "включи свет на кухне"}, ensure_ascii=False)
    runtime, payloads = _home(tmp_path, [_call("scenario_run", arguments), {"content": "Включила свет и чайник."}])
    client, server = types()
    sent = []

    async def send(data):
        sent.append(data)
    conv = Conversation(runtime, server, send, trace_context={"sn": "SN-T"})
    # фраза не сценарная целиком — уходит агенту, тот сам запускает сценарий света
    await _say(conv, client, "сделай уютно: свет на кухне и чайник", "m1")
    response = _responses(sent, server)[-1]
    assert response.Response.Cards[0].Text == "Включила свет и чайник."
    device = next(d for d in runtime.identities.data["devices"] if d["sn"] == "SN-T")
    assert device["state"]["light"]["кухня"]["on"] is True
    assert runtime.pending_calls == []
    await conv.shutdown()
    await runtime.close()


async def test_voice_history_survives_reconnect(tmp_path):
    runtime, payloads = _home(tmp_path, [{"content": "Запомнила."}, {"content": "Вы говорили про синий."}])
    client, server = types()

    async def send(data):
        pass
    conv = Conversation(runtime, server, send, trace_context={"sn": "SN-T"})
    await _say(conv, client, "мой любимый цвет синий", "m1")
    await conv.shutdown()
    conv = Conversation(runtime, server, send, trace_context={"sn": "SN-T"})  # переподключение колонки
    await _say(conv, client, "какой мой любимый цвет", "m2")
    texts = [m.get("content") for m in payloads[-1]["messages"]]
    assert "мой любимый цвет синий" in texts and "Запомнила." in texts
    await conv.shutdown()
    await runtime.close()
