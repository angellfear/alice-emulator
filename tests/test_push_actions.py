"""Push-действия: два канала доставки — Push без квитанции и квитируемый
SyncPush{ExecuteDirective} (ответ SyncPushResponse.ExecuteDirectiveResponse).
push_tts: Push @@mm_semantic_frame → колонка возвращает фрейм TextInput'ом →
реплика с этим текстом через голосовой конвейер (сценарий system.push_tts)."""
import asyncio
import types
from pathlib import Path

import pytest

from server_emulator.actions import CATALOG, EXECUTE_ACTIONS, execute_message, push_message
from server_emulator.runtime import Runtime, _on_sync_push_response
from server_emulator.schema import load_type
from server_emulator.voice import Conversation
from tests.conftest import listen, types as message_types

SCHEMAS = Path(__file__).resolve().parent.parent / "server_emulator" / "schemas"


def _cls():
    return load_type(SCHEMAS, "alice-api.pb", "NAlice.NAliceApi.TServerMessage")


def _catalog_ids():
    return {a["id"] for a in CATALOG}


def test_execute_directive_wire_form():
    """Квитируемый кадр: SyncPush{ExecuteDirective{Directive, ResponsePolicy.OnFinishedPolicy}}."""
    cls = _cls()
    message = execute_message(cls, "clear_queue")
    push = message.Event.SyncPush
    assert push.PushId and push.DeduplicationPushId
    directive = push.SyncRequest.ExecuteDirective.Directive
    assert directive.Name == "clear_queue" and directive.Type == "client_action"
    assert push.SyncRequest.ExecuteDirective.ResponsePolicy.HasField("OnFinishedPolicy")
    parsed = cls.FromString(message.SerializeToString())  # бинарный roundtrip
    assert parsed.Event.SyncPush.SyncRequest.ExecuteDirective.Directive.Name == "clear_queue"
    assert parsed.Event.SyncPush.SyncRequest.ExecuteDirective.ResponsePolicy.HasField("OnFinishedPolicy")


def test_push_update_config_wire_form_matches_sample():
    """push_update_config — обычный Push по образцу настоящего сервера: одна директива
    client_action без payload и IsLedSilent, PushIds, DeduplicationPushId, RequestId."""
    cls = _cls()
    message = cls.FromString(push_message(cls, "push_update_config", {}).SerializeToString())
    assert message.Event.WhichOneof("Event") == "Push"
    push = message.Event.Push
    assert message.Event.Header.MessageId and message.Event.Header.Ack
    assert len(push.PushIds) == 1 and push.DeduplicationPushId
    assert len(push.Directives) == 1
    directive = push.Directives[0]
    assert directive.Type == "client_action" and directive.Name == "push_update_config"
    assert not directive.IsLedSilent and not directive.HasField("Payload")
    assert push.AnalyticsMetaInfo.RequestId
    with pytest.raises(ValueError):
        push_message(cls, "push_update_config", {"x": 1})


def test_execute_player_pause_payload():
    """player_pause — директива образца паузы (media-pause): payload {smooth: false}; параметры действия не принимаются."""
    cls = _cls()
    message = execute_message(cls, "player_pause")
    payload = message.Event.SyncPush.SyncRequest.ExecuteDirective.Directive.Payload
    assert payload.fields["smooth"].bool_value is False
    with pytest.raises(ValueError):
        execute_message(cls, "player_pause", {"smooth": True})
    with pytest.raises(ValueError):
        execute_message(cls, "sound_set_level")  # косметика не квитируется


def test_push_tts_wire_form():
    """push_tts — Push с директивой server_action @@mm_semantic_frame и текстом фрейма."""
    cls = _cls()
    message = push_message(cls, "push_tts", {"text": " Привет "})
    directive = message.Event.Push.Directives[0]
    assert directive.Name == "@@mm_semantic_frame" and directive.Type == "server_action"
    frame = directive.Payload.fields["typed_semantic_frame"].struct_value
    text = frame.fields["tts_semantic_frame"].struct_value.fields["text"].string_value
    assert text == "Привет"
    with pytest.raises(ValueError):
        push_message(cls, "push_tts", {"text": "   "})
    with pytest.raises(ValueError):
        push_message(cls, "push_tts", {})


def test_catalog_channels():
    """Квитируемые действия — подмножество каталога; push_tts и косметика — Push."""
    assert EXECUTE_ACTIONS == {"player_pause", "clear_queue"}
    assert EXECUTE_ACTIONS <= _catalog_ids()
    for action in ("push_tts", "push_update_config", "sound_set_level", "start_bluetooth", "draw_led_screen"):
        assert action in _catalog_ids() and action not in EXECUTE_ACTIONS


def _receipt(client, message_id, status="ExecutionCompleted"):
    """Квитанция колонки: SyncPushResponse с RefMessageId нашего кадра."""
    message = client()
    message.Event.Header.MessageId = "resp-1"
    message.Event.Header.RefMessageId = message_id
    exec_status = message.Event.SyncPushResponse.SyncResponse.ExecuteDirectiveResponse.ExecutionStatus
    getattr(exec_status, status).SetInParent()
    return message


async def _runtime_with_session(tmp_path, sent):
    async def capture(data):
        sent.append(data)
    runtime = Runtime(tmp_path)
    runtime.sessions["c1"] = {"device_sn": "SN-E", "connection_id": "c1", "user_id": "u",
                              "send": capture}
    return runtime


async def test_execute_receipt_completed(tmp_path):
    """Квитанция Completed — в запись действия и в action_log; кадр — SyncPush."""
    sent = []
    runtime = await _runtime_with_session(tmp_path, sent)
    client, server = message_types()
    task = asyncio.ensure_future(runtime.push_action("SN-E", "clear_queue", {}))
    while not sent:
        await asyncio.sleep(0.02)
    frame = server.FromString(sent[0][4:])
    assert frame.Event.WhichOneof("Event") == "SyncPush"
    conversation = types.SimpleNamespace(runtime=runtime, trace_context={"sn": "SN-E"})
    assert await _on_sync_push_response(conversation, _receipt(client, frame.Event.Header.MessageId),
                                        "SyncPushResponse") is True
    item = await asyncio.wait_for(task, 5)
    assert item["delivery"] == "sent" and item["execution"] == "completed"
    assert runtime.action_log[-1]["execution"] == "completed"
    await runtime.close()


@pytest.mark.parametrize("status,expected", [
    ("ExecutionStarted", "started"), ("ExecutionFailed", "failed")])
async def test_execute_receipt_statuses(tmp_path, status, expected):
    sent = []
    runtime = await _runtime_with_session(tmp_path, sent)
    client, server = message_types()
    task = asyncio.ensure_future(runtime.push_action("SN-E", "player_pause", {}))
    while not sent:
        await asyncio.sleep(0.02)
    conversation = types.SimpleNamespace(runtime=runtime, trace_context={"sn": "SN-E"})
    receipt = _receipt(client, server.FromString(sent[0][4:]).Event.Header.MessageId, status)
    await _on_sync_push_response(conversation, receipt, "SyncPushResponse")
    item = await asyncio.wait_for(task, 5)
    assert item["execution"] == expected
    await runtime.close()


async def test_execute_receipt_timeout(tmp_path, monkeypatch):
    """Нет квитанции за отведённое время — execution остаётся unconfirmed."""
    monkeypatch.setattr("server_emulator.runtime.EXECUTE_TIMEOUT_S", 0.1)
    sent = []
    runtime = await _runtime_with_session(tmp_path, sent)
    item = await runtime.push_action("SN-E", "clear_queue", {})
    assert item["delivery"] == "sent" and item["execution"] == "unconfirmed"
    assert runtime.execute_waiters == {}  # ожидание убрано
    await runtime.close()


async def test_cosmetic_stays_push_without_receipt(tmp_path):
    """Косметика — обычный Push: ответ сразу, без ожидания квитанции."""
    sent = []
    runtime = await _runtime_with_session(tmp_path, sent)
    _client, server = message_types()
    item = await runtime.push_action("SN-E", "sound_set_level", {"level": 5})
    assert item["delivery"] == "sent" and item["execution"] == "unconfirmed"
    frame = server.FromString(sent[0][4:])
    assert frame.Event.WhichOneof("Event") == "Push"
    assert frame.Event.Push.Directives[0].Payload.fields["new_percent_level"].number_value == 50
    await runtime.close()


async def test_push_tts_end_to_end(tmp_path):
    """push_tts: Push → колонка возвращает фрейм TextInput'ом → AliceResponse с репликой
    и озвучкой (поставщик tts-стаб, HasVoiceResponse как у фразового пути)."""
    from tests.test_voice_failures import make_ogg

    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, aapi_tts=True)
    ogg, tts = await make_ogg(), []
    sent = []

    async def provider(kind, payload=None, **kwargs):
        if kind == "tts":
            tts.append(payload["input"])
            return ogg, "audio/ogg"
        raise AssertionError("модель для push_tts не нужна")

    runtime.provider = provider
    client, server = message_types()
    async def capture(data):
        sent.append(data)
    runtime.sessions["c1"] = {"device_sn": "SN-T", "connection_id": "c1", "user_id": "u",
                              "send": capture}
    conv = Conversation(runtime, server, capture, trace_context={"sn": "SN-T"})
    push = push_message(server, "push_tts", {"text": "Проверка связи"})
    item = await runtime.push_action("SN-T", "push_tts", {"text": "Проверка связи"})
    assert item["delivery"] == "sent" and item["execution"] == "unconfirmed"  # Push без квитанции

    # колонка выполняет server_action из Push: TextInput с тем же payload
    back = client()
    back.Event.Header.MessageId = "m2"
    back.Event.TextInput.Header.RequestId = "r2"
    action = back.Event.TextInput.Request.Event
    action.Type = 6
    action.Name = push.Event.Push.Directives[0].Name
    action.Payload.CopyFrom(push.Event.Push.Directives[0].Payload)
    await conv.receive(back, "TextInput")
    await asyncio.wait_for(conv.task, 10)
    events = [server.FromString(raw[4:]).Event for raw in sent if raw.startswith(b"AAPI")]
    replies = [e.AliceResponse for e in events if e.WhichOneof("Event") == "AliceResponse"]
    assert replies and replies[0].VoiceResponse.OutputSpeech.Text == "Проверка связи"
    assert replies[0].VoiceResponse.HasVoiceResponse is True
    assert tts == ["Проверка связи"]  # озвучка реплики
    assert any(e.WhichOneof("Event") == "TtsSpeak" for e in events)
    await conv.shutdown()
    await runtime.close()


async def test_push_tts_without_voice_settings(tmp_path):
    """aapi_tts выключен — карточка-текст уходит без аудио, поставщик синтеза не зовётся."""
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, aapi_tts=False)
    tts = []
    sent = []

    async def provider(kind, payload=None, **kwargs):
        if kind == "tts":
            tts.append(payload["input"])
            return b"", "audio/ogg"
        raise AssertionError("модель для push_tts не нужна")

    runtime.provider = provider
    client, server = message_types()
    async def capture(data):
        sent.append(data)
    runtime.sessions["c1"] = {"device_sn": "SN-T", "connection_id": "c1", "user_id": "u",
                              "send": capture}
    conv = Conversation(runtime, server, capture, trace_context={"sn": "SN-T"})
    push = push_message(server, "push_tts", {"text": "Тихо"})
    back = client()
    back.Event.Header.MessageId = "m3"
    back.Event.TextInput.Header.RequestId = "r3"
    action = back.Event.TextInput.Request.Event
    action.Type = 6
    action.Name = push.Event.Push.Directives[0].Name
    action.Payload.CopyFrom(push.Event.Push.Directives[0].Payload)
    await conv.receive(back, "TextInput")
    await asyncio.wait_for(conv.task, 10)
    events = [server.FromString(raw[4:]).Event for raw in sent if raw.startswith(b"AAPI")]
    replies = [e.AliceResponse for e in events if e.WhichOneof("Event") == "AliceResponse"]
    assert replies and replies[0].VoiceResponse.OutputSpeech.Text == "Тихо"
    assert replies[0].VoiceResponse.HasVoiceResponse is False
    assert tts == [] and not any(e.WhichOneof("Event") == "TtsSpeak" for e in events)
    await conv.shutdown()
    await runtime.close()


async def test_mcp_send_action_receipt(tmp_path):
    """MCP-инструмент: квитируемое действие получает статус, косметика — сразу."""
    sent = []
    runtime = await _runtime_with_session(tmp_path, sent)
    client, server = message_types()
    from server_emulator.actions import send_action
    cosmetic = await send_action(runtime, "SN-E", "sound_quiter", {})
    assert cosmetic["delivery"] == "sent" and cosmetic.get("origin") == "mcp"
    task = asyncio.ensure_future(send_action(runtime, "SN-E", "clear_queue", {}))
    while len(sent) < 2:
        await asyncio.sleep(0.02)
    conversation = types.SimpleNamespace(runtime=runtime, trace_context={"sn": "SN-E"})
    receipt = _receipt(client, server.FromString(sent[1][4:]).Event.Header.MessageId)
    await _on_sync_push_response(conversation, receipt, "SyncPushResponse")
    item = await asyncio.wait_for(task, 5)
    assert item["execution"] == "completed" and item.get("origin") == "mcp"
    await runtime.close()


# --- «Прозрачно»: доставка в мостовое соединение -------------------------------------

async def _fake_uniproxy(received):
    """Локальный «Яндекс» для моста: WS /uni.ws копит кадры колонки (client_to_server)."""
    from aiohttp import WSMsgType, web

    async def ws(request):
        socket = web.WebSocketResponse(autoping=False)
        await socket.prepare(request)
        async for msg in socket:
            if msg.type == WSMsgType.BINARY:
                received.append(bytes(msg.data))
        return socket

    app = web.Application()
    app.router.add_get("/uni.ws", ws)
    runner, url = await listen(app)
    return runner, int(url.rsplit(":", 1)[1])


async def _bridge_setup(tmp_path):
    """Runtime + устройство в «Прозрачно» («Запись» включена); возвращает runtime."""
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-T", "user_id": "u",
                               "device_ids": ["t-id"]})
    runtime.identities.mutate({"operation": "route_mode", "sn": "SN-T", "mode": "proxy"})
    runtime.identities.mutate({"operation": "record", "sn": "SN-T", "mode": "on"})
    return runtime


async def test_push_action_reaches_column_through_bridge(tmp_path):
    """«Прозрачно»: сессий эмулятора нет — кадр инъекцией в мост (колонка получает
    server_to_client, «Яндекс» — копию), в журнале запись с source=emulator."""
    from aiohttp import ClientSession

    from server_emulator.server import create_app

    received = []
    upstream_runner, port = await _fake_uniproxy(received)
    runtime = await _bridge_setup(tmp_path)
    runtime.settings["aliases"] = {"uniproxy.alice.yandex.net": f"http://127.0.0.1:{port}"}
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            mine = await client.ws_connect(url + "/uni.ws",
                                           headers={"Host": "uniproxy.alice.yandex.net",
                                                    "x-uprx-device-id": "t-id"})
            await asyncio.sleep(0.2)  # мост открылся
            (connection,) = runtime.upstream.forwarder.connections.values()
            assert connection["device_sn"] == "SN-T"
            item = await runtime.push_action("SN-T", "draw_led_screen",
                                             {"frames": ["https://static-alice.s3.yandex.net/led-production/emotions/v3/joy.gif"]})
            assert item["delivery"] == "sent" and item["execution"] == "unconfirmed"
            assert item["connection_id"].startswith("bridge-")
            frame = await asyncio.wait_for(mine.receive(), 5)
            assert frame.type == 2  # binary — инъекция дошла до колонки
            message = _cls().FromString(frame.data[4:])
            assert message.Event.WhichOneof("Event") == "Push"
            assert message.Event.Push.Directives[0].Name == "draw_led_screen"
            await asyncio.sleep(0.2)
            assert not received  # косметика без SyncPush — наверх колонка ничего не отвечала
            await runtime.upstream.journal.flush()
            records = (await runtime.upstream.journal.list(device="SN-T"))["items"]
            injected = [r for r in records if r["source"] == "emulator" and r["direction"] == "server_to_client"]
            assert injected and "Push" in injected[0]["key"]
            await mine.close()
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()


async def test_execute_receipt_through_bridge(tmp_path):
    """Квитанция ExecuteDirective в «Прозрачно»: кадр колонки — копией наверх
    (передача прозрачна), ожидающему push_action — разобранное сообщение."""
    from aiohttp import ClientSession

    from server_emulator.server import create_app

    received = []
    upstream_runner, port = await _fake_uniproxy(received)
    runtime = await _bridge_setup(tmp_path)
    runtime.settings["aliases"] = {"uniproxy.alice.yandex.net": f"http://127.0.0.1:{port}"}
    runner, url = await listen(create_app(runtime=runtime))
    client_message, _server = message_types()
    try:
        async with ClientSession() as client:
            mine = await client.ws_connect(url + "/uni.ws",
                                           headers={"Host": "uniproxy.alice.yandex.net",
                                                    "x-uprx-device-id": "t-id"})
            await asyncio.sleep(0.2)
            task = asyncio.ensure_future(runtime.push_action("SN-T", "clear_queue", {}))
            frame = await asyncio.wait_for(mine.receive(), 5)  # инъекция SyncPush колонке
            sync_push = _cls().FromString(frame.data[4:])
            assert sync_push.Event.WhichOneof("Event") == "SyncPush"
            # колонка отвечает SyncPushResponse в мост (client_to_server)
            receipt = _receipt(client_message, sync_push.Event.Header.MessageId)
            await mine.send_bytes(b"AAPI" + receipt.SerializeToString())
            item = await asyncio.wait_for(task, 5)
            assert item["delivery"] == "sent" and item["execution"] == "completed"
            await asyncio.sleep(0.2)
            # копия кадра колонки ушла наверх — передача прозрачна
            assert any(isinstance(r, bytes) and r.startswith(b"AAPI")
                       and client_message.FromString(r[4:]).Event.WhichOneof("Event") == "SyncPushResponse"
                       for r in received)
            await mine.close()
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()


async def test_no_session_reason(tmp_path):
    """Честный отказ: устройство в «Прозрачно» без моста — reason transparent;
    устройство вне связи вовсе — reason offline."""
    runtime = await _bridge_setup(tmp_path)  # SN-T в «Прозрачно», моста нет
    item = await runtime.push_action("SN-T", "sound_set_level", {"level": 5})
    assert item["delivery"] == "no_session" and item["reason"] == "transparent"
    item = await runtime.push_action("SN-NOBODY", "sound_set_level", {"level": 5})
    assert item["delivery"] == "no_session" and item["reason"] == "offline"
    await runtime.close()
