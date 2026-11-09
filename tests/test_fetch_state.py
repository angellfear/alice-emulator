"""SyncPush-цикл: опрос состояния колонки (fetch_state) и обработчик SyncPushResponse.

Кадр проверяется по дескриптору (roundtrip), живой цикл — на настоящем WS: сервер
шлёт SyncPush{GetDeviceState}, фикстурный WS-клиент отвечает SyncPushResponse
(EnvironmentState — возможности, DeviceState — громкость). Гибридный релей —
tests/test_ws_hybrid.py (кадр наверх не уходит).
"""
import asyncio
import time

import pytest
from aiohttp import ClientSession

from server_emulator.runtime import (ROOT, FetchStateSuperseded, FetchStateThrottled,
                                     FetchStateTimeout, Runtime, sync_push_message)
from server_emulator.schema import load_type, schemas_dir, station_file
from server_emulator.server import create_app
from tests.conftest import listen, types

SN = "SN-FS"


def _alarm_capability():
    """EnvironmentState-возможности в RawCapability: будильник 7:30 пн/пт и таймер,
    запущенный 300 с назад на 600 с (остаток ~300 с)."""
    cap = load_type(schemas_dir(), station_file(), "NAlice.TAlarmCapability")()
    alarm = cap.State.Alarms.add()
    alarm.AlarmId = "alarm-1"
    alarm.LocalTime.Hours, alarm.LocalTime.Minutes = 7, 30
    alarm.ActivationDays.add().Weekday = 1  # MONDAY
    alarm.ActivationDays.add().Weekday = 5  # FRIDAY
    timer = cap.State.Timers.add()
    timer.TimerId = "timer-1"
    timer.Duration = 600
    timer.StartTimestamp = int(time.time()) - 300
    return cap.SerializeToString()


def _response(client_type, level=5):
    """SyncPushResponse колонки: DeviceState (громкость 5 из 10) + EnvironmentState
    с AlarmCapabilityType (21)."""
    message = client_type()
    message.Event.Header.MessageId = "syncpush-resp"
    inner = message.Event.SyncPushResponse.SyncResponse.GetDeviceStateResponse
    inner.DeviceState.SoundLevel = level
    inner.DeviceState.SoundMaxLevel = 10
    holder = inner.EnvironmentState.Endpoints.add().AnyCapabilities.add()
    holder.CapabilityType = 21  # AlarmCapabilityType (station_state)
    holder.RawCapability = _alarm_capability()
    return message


async def _next_event(ws, server_type, name, limit=8):
    """Следующий AAPI-кадр колонке с событием name (прочие кадры пропускаются)."""
    for _ in range(limit):
        packet = await asyncio.wait_for(ws.receive(), 3)
        if not packet.data.startswith(b"AAPI"):
            continue
        message = server_type.FromString(packet.data[4:])
        if message.Event.WhichOneof("Event") == name:
            return message
    raise AssertionError("кадр " + name + " не пришёл")


async def _station_ws(runtime, client_type):
    """Живая WS-сессия колонки: устройство SN представилось (dev-fs), sync отвечен."""
    runtime.identities.mutate({"operation": "user", "id": "owner", "name": "O"})
    runtime.identities.mutate({"operation": "device", "sn": SN, "user_id": "owner",
                               "device_ids": ["dev-fs"]})
    runner, url = await listen(create_app(runtime=runtime))
    client = ClientSession()
    ws = await client.ws_connect(url + "/uni.ws")
    sync = client_type()
    sync.Event.Header.MessageId = "sync"
    sync.Event.SynchronizeStateRequest.Application.DeviceId = "dev-fs"
    await ws.send_bytes(b"AAPI" + sync.SerializeToString())
    await asyncio.wait_for(ws.receive(), 3)  # SynchronizeStateResponse
    return runner, client, ws


def test_sync_push_frame_build():
    """SyncPush собирается по протоколу: PushId/DeduplicationPushId — uuid, GetDeviceState
    с пустыми опциями в кадре, Header{MessageId, Ack}."""
    _client, server_type = types()
    message = sync_push_message(ROOT / "schemas")
    assert message.Event.WhichOneof("Event") == "SyncPush"
    assert len(message.Event.Header.MessageId) == 32 and message.Event.Header.Ack > 0
    push = message.Event.SyncPush
    assert len(push.PushId) == 32 and len(push.DeduplicationPushId) == 32
    state = push.SyncRequest.GetDeviceState
    assert state.HasField("GetDeviceStateOptions") and state.HasField("GetEnvironmentStateOptions")
    again = server_type.FromString(message.SerializeToString())  # пустые опции остались в кадре
    assert again.Event.SyncPush.SyncRequest.GetDeviceState.HasField("GetDeviceStateOptions")
    assert again.Event.SyncPush.SyncRequest.GetDeviceState.HasField("GetEnvironmentStateOptions")


async def test_fetch_state_full_cycle(tmp_path):
    """Полный цикл по WS: SyncPush уходит, ответ разбирается — будильники (время, дни),
    таймер с remaining, громкость из DeviceState, ассистент из sync-конфига; итог
    сохраняется в devices.state.fetch_state_last, промаха «Без ответа» нет."""
    runtime = Runtime(tmp_path)
    client_type, server_type = types()
    runner, client, ws = await _station_ws(runtime, client_type)
    try:
        poll = asyncio.ensure_future(runtime.fetch_state(SN))
        push = await _next_event(ws, server_type, "SyncPush")
        assert push.Event.SyncPush.SyncRequest.GetDeviceState.HasField("GetEnvironmentStateOptions")
        response = _response(client_type)
        response.Event.Header.RefMessageId = push.Event.Header.MessageId
        await ws.send_bytes(b"AAPI" + response.SerializeToString())
        result = await asyncio.wait_for(poll, 5)
        assert result["alarms"][0]["alarm_id"] == "alarm-1"
        assert result["alarms"][0]["time"] == {"hours": 7, "minutes": 30, "seconds": 0}
        assert result["alarms"][0]["weekdays"] == [1, 5]
        timer = result["timers"][0]
        assert timer["duration"] == 600 and 290 <= timer["remaining"] <= 300
        assert result["volume"] == {"level": 5, "max_level": 10, "muted": False, "percent": 50.0}
        assert result["player"] == {}
        assert result["assistant_name"] == "Алиса"  # spotter alisa из sync-конфига
        stored = next(d for d in runtime.identities.data["devices"]
                      if d["sn"] == SN)["state"]["fetch_state_last"]
        assert stored["ts"] == result["ts"] and stored["alarms"] == result["alarms"]
        assert stored["volume"] == result["volume"] and stored["assistant_name"] == "Алиса"
        assert runtime.aapi_misses.public() == []
        assert runtime.fetch_waiters == {}  # ожидание снято
    finally:
        await client.close()
        await runner.cleanup()
        await runtime.close()


async def test_fetch_state_timeout(tmp_path):
    """Колонка молчит — FetchStateTimeout, ожидание снято."""
    runtime = Runtime(tmp_path)
    client_type, server_type = types()
    runner, client, ws = await _station_ws(runtime, client_type)
    try:
        with pytest.raises(FetchStateTimeout):
            await runtime.fetch_state(SN, timeout=0.2)
        assert runtime.fetch_waiters == {}
        assert runtime.aapi_misses.public() == []  # кадр SyncPush — не промах колонки
    finally:
        await client.close()
        await runner.cleanup()
        await runtime.close()


async def test_fetch_state_superseded_by_new_poll(tmp_path):
    """Повторный опрос отменяет предыдущее ожидание: прежний вызов — FetchStateSuperseded,
    новый получает ответ."""
    runtime = Runtime(tmp_path)
    client_type, server_type = types()
    runner, client, ws = await _station_ws(runtime, client_type)
    try:
        first = asyncio.ensure_future(runtime.fetch_state(SN))
        await _next_event(ws, server_type, "SyncPush")
        second = asyncio.ensure_future(runtime.fetch_state(SN))
        push = await _next_event(ws, server_type, "SyncPush")
        response = _response(client_type)
        response.Event.Header.RefMessageId = push.Event.Header.MessageId
        await ws.send_bytes(b"AAPI" + response.SerializeToString())
        result = await asyncio.wait_for(second, 5)
        with pytest.raises(FetchStateSuperseded):
            await first
        assert result["volume"]["level"] == 5
        assert runtime.fetch_waiters == {}
    finally:
        await client.close()
        await runner.cleanup()
        await runtime.close()


async def test_fetch_state_throttling(tmp_path):
    """Троттлинг: повторный опрос раньше min_interval_s — FetchStateThrottled; 0 — без лимита."""
    runtime = Runtime(tmp_path)
    client_type, server_type = types()
    runner, client, ws = await _station_ws(runtime, client_type)
    try:
        poll = asyncio.ensure_future(runtime.fetch_state(SN))
        push = await _next_event(ws, server_type, "SyncPush")
        response = _response(client_type)
        response.Event.Header.RefMessageId = push.Event.Header.MessageId
        await ws.send_bytes(b"AAPI" + response.SerializeToString())
        await asyncio.wait_for(poll, 5)
        with pytest.raises(FetchStateThrottled) as exc:
            await runtime.fetch_state(SN)
        assert exc.value.retry_after_s > 0
        runtime.settings["fetch_state"]["min_interval_s"] = 0  # без лимита
        repeat = asyncio.ensure_future(runtime.fetch_state(SN))
        push = await _next_event(ws, server_type, "SyncPush")
        response = _response(client_type, level=7)
        response.Event.Header.RefMessageId = push.Event.Header.MessageId
        await ws.send_bytes(b"AAPI" + response.SerializeToString())
        result = await asyncio.wait_for(repeat, 5)
        assert result["volume"]["level"] == 7
    finally:
        await client.close()
        await runner.cleanup()
        await runtime.close()


async def test_fetch_state_api(tmp_path):
    """API fetch_state: нет сессии — 400; чаще min_interval_s — 429 (retry_after_s);
    нет обработчика у действия — не бывает: маршрут в реестре."""
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        runtime.identities.mutate({"operation": "user", "id": "owner", "name": "O"})
        runtime.identities.mutate({"operation": "device", "sn": SN, "user_id": "owner",
                                   "device_ids": ["dev-fs"]})
        async with ClientSession() as client:
            async with client.post(url + "/emulator/api/fetch_state", json={"sn": SN},
                                   headers={"X-Emulator": "1"}) as response:
                assert response.status == 400  # колонка не в сети
            device = next(d for d in runtime.identities.data["devices"] if d["sn"] == SN)
            state = dict(device.get("state") or {})
            state["fetch_state_last"] = {"ts": time.time(), "alarms": [], "timers": [],
                                         "volume": {}, "player": {}, "assistant_name": ""}
            runtime.identities.mutate({"operation": "state", "sn": SN, "state": state})
            async with client.post(url + "/emulator/api/fetch_state", json={"sn": SN},
                                   headers={"X-Emulator": "1"}) as response:
                assert response.status == 429
                body = await response.json()
                assert body["code"] == "fetch_state_throttled" and body["retry_after_s"] > 0
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_sync_push_response_routed_to_waiter(tmp_path):
    """Обработчик SyncPushResponse: ожидание fetch_state получает ответ; квитанция
    ExecuteDirective (другой канал push) его не будит; ответ без ожидающего — тоже
    обработан; промахов «Без ответа» нет."""
    runtime = Runtime(tmp_path)
    client_type, server_type = types()
    sent = []
    from server_emulator.voice import Conversation
    conversation = Conversation(runtime, server_type, sent.append,
                                trace_context={"session": "s1", "sn": SN})
    try:
        message = _response(client_type)
        future = asyncio.get_running_loop().create_future()
        runtime.fetch_waiters[SN] = future
        assert await conversation.receive(message, "SyncPushResponse") is True
        assert await asyncio.wait_for(future, 1) is message
        assert sent == []  # это ответ на НАШ push — колонке ничего не уходит
        receipt = client_type()
        receipt.Event.SyncPushResponse.SyncResponse.ExecuteDirectiveResponse.SetInParent()
        pending = asyncio.get_running_loop().create_future()
        runtime.fetch_waiters[SN] = pending
        assert await conversation.receive(receipt, "SyncPushResponse") is True
        assert not pending.done()  # квитанция GetDeviceState не отвечает
        assert runtime.aapi_misses.public() == []  # событие никогда не в «Без ответа»
    finally:
        await runtime.close()


async def test_fetch_state_assistant_mapping(tmp_path):
    """Имя ассистента: spotter переводится (alisa→Алиса, yasmina→Ясмина), неизвестное —
    как есть."""
    runtime = Runtime(tmp_path)
    runtime.settings["fetch_state"]["min_interval_s"] = 0  # опросы подряд без троттлинга
    client_type, server_type = types()
    runner, client, ws = await _station_ws(runtime, client_type)
    try:
        runtime.identities.mutate({"operation": "user_quasar_overrides", "id": "owner",
                                   "quasar_overrides": [{"path": "config.account_config.spotter",
                                                         "source": "yasmina"}]})
        poll = asyncio.ensure_future(runtime.fetch_state(SN))
        push = await _next_event(ws, server_type, "SyncPush")
        response = _response(client_type)
        response.Event.Header.RefMessageId = push.Event.Header.MessageId
        await ws.send_bytes(b"AAPI" + response.SerializeToString())
        result = await asyncio.wait_for(poll, 5)
        assert result["assistant_name"] == "Ясмина"
        runtime.identities.mutate({"operation": "user_quasar_overrides", "id": "owner",
                                   "quasar_overrides": [{"path": "config.account_config.spotter",
                                                         "source": "anna"}]})
        repeat = asyncio.ensure_future(runtime.fetch_state(SN))
        push = await _next_event(ws, server_type, "SyncPush")
        response = _response(client_type)
        response.Event.Header.RefMessageId = push.Event.Header.MessageId
        await ws.send_bytes(b"AAPI" + response.SerializeToString())
        result = await asyncio.wait_for(repeat, 5)
        assert result["assistant_name"] == "anna"  # неизвестный spotter — как есть
    finally:
        await client.close()
        await runner.cleanup()
        await runtime.close()
