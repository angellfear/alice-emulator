"""TASK-054: «Запись» — опция устройства (off | on | misses), журнал трафика по устройствам.

Только локальные серверы: «настоящий Яндекс» — локальная aiohttp-заглушка (см. test_device_route_modes).
"""
import asyncio
import json
import sqlite3
import uuid
from pathlib import Path

from aiohttp import ClientSession, WSMsgType, web

from server_emulator.runtime import Runtime
from server_emulator.schema import load_type
from server_emulator.server import create_app
from server_emulator.state_store import StateStore
from server_emulator.upstream.journal import TrafficJournal
from tests.conftest import listen
from tests.test_device_route_modes import alias_upstream, registry, start_upstream

API = {"X-Emulator": "1"}
DEVICE = {"Host": "quasar.yandex.net", "X-YaQuasarDeviceId": "rc-id"}
CLIENT = "NAlice.NAliceApi.TClientMessage"
SERVER = "NAlice.NAliceApi.TServerMessage"


def client_frame(event, message_id=None):
    """AAPI-кадр колонки: event — SynchronizeStateRequest | Ping | …"""
    message = load_type("server_emulator/schemas", "alice-api.pb", CLIENT)()
    message.Event.Header.MessageId = message_id or uuid.uuid4().hex
    getattr(message.Event, event).SetInParent()
    if event == "SynchronizeStateRequest":
        message.Event.SynchronizeStateRequest.Application.DeviceId = "w-id"
    return b"AAPI" + message.SerializeToString()


def server_frame(event, ref=None):
    message = load_type("server_emulator/schemas", "alice-api.pb", SERVER)()
    message.Event.Header.MessageId = uuid.uuid4().hex
    if ref:
        message.Event.Header.RefMessageId = ref
    getattr(message.Event, event).SetInParent()
    return b"AAPI" + message.SerializeToString()


def parsed(data, name):
    return load_type("server_emulator/schemas", "alice-api.pb", name).FromString(data[4:])


async def ws_records(runtime, closed=True, wait=0.0):
    """Записи WS из журнала (после flush); closed — дождаться записи закрытия."""
    items = []
    for _ in range(60):
        await runtime.upstream.journal.flush()
        items = [i for i in runtime.upstream.journal.list(limit=500)["items"] if i["kind"] == "ws"]
        if not closed or any(i["event"] == "close" for i in items):
            break
        await asyncio.sleep(0.05)
    return items


async def start(tmp_path, sn="SN-RC", device_id="rc-id", mode="emulate"):
    runtime = Runtime(tmp_path)
    registry(runtime, sn, device_id)
    calls = []
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    runtime.settings["default_route_mode"] = mode
    runner, url = await listen(create_app(runtime=runtime))
    return runtime, calls, upstream_runner, runner, url


async def stop(runtime, upstream_runner, runner):
    await upstream_runner.cleanup()
    await runner.cleanup()
    await runtime.close()


def set_record(runtime, sn, mode):
    runtime.identities.mutate({"operation": "record", "sn": sn, "mode": mode})


# --- маршрут и миграция -----------------------------------------------------------------

async def test_migration_record_route_to_proxy_plus_record(tmp_path):
    """record → proxy + record «Включено» (устройство и default, без своего маршрута)."""
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    for sn, alias in (("SN-A", "a-id"), ("SN-B", "b-id"), ("SN-C", "c-id")):
        runtime.identities.mutate({"operation": "device", "sn": sn, "user_id": "u", "device_ids": [alias]})
    runtime.identities.mutate({"operation": "route_mode", "sn": "SN-C", "mode": "emulate"})
    await runtime.close()

    store = StateStore(tmp_path / "emulator.sqlite3")
    data = store.load_identity_data()
    next(d for d in data["devices"] if d["sn"] == "SN-A")["route_mode"] = "record"  # прежний маршрут «Запись»
    store.save_identity_data(data)
    settings = dict(store.load_settings())
    settings["default_route_mode"] = "record"
    store.save_settings(settings)
    store.db.execute("DELETE FROM meta WHERE key='record_migration'")  # состояние до TASK-054
    store.db.commit()
    store.close()

    migrated = Runtime(tmp_path)
    try:
        assert migrated.settings["default_route_mode"] == "proxy"
        a, b, c = (migrated.identities.resolve(i) for i in ("a-id", "b-id", "c-id"))
        assert (a["route_mode"], a["record"]) == ("proxy", "on")
        assert (b["route_mode"], b["record"]) == (None, "on")  # наследовал default record
        assert (c["route_mode"], c["record"]) == ("emulate", "off")  # свой маршрут — запись не включается
    finally:
        await migrated.close()
    again = Runtime(tmp_path)  # однократно: повторная загрузка ничего не меняет
    try:
        assert again.settings["default_route_mode"] == "proxy"
        assert again.identities.resolve("b-id")["record"] == "on"
        assert again.identities.resolve("c-id")["record"] == "off"
    finally:
        await again.close()


async def test_record_is_not_a_route_mode(tmp_path):
    runtime = Runtime(tmp_path)
    registry(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession(headers=API) as client:
            async with client.put(url + "/emulator/api/route_mode", json={"sn": "SN-UP", "mode": "record"}) as response:
                assert response.status == 400
            async with client.put(url + "/emulator/api/mode", json={"mode": "record"}) as response:
                assert response.status == 400
            async with client.put(url + "/emulator/api/record_mode", json={"sn": "SN-UP", "mode": "turbo"}) as response:
                assert response.status == 400
            async with client.put(url + "/emulator/api/record_mode", json={"sn": "SN-NO", "mode": "on"}) as response:
                assert response.status == 400  # SN не найден
            async with client.put(url + "/emulator/api/record_mode", json={"sn": "SN-UP", "mode": "misses"}) as response:
                assert await response.json() == {"sn": "SN-UP", "mode": "misses"}
            async with client.get(url + "/emulator/api/identities") as response:
                device = (await response.json())["devices"][0]
            assert device["record"] == "misses"
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_record_survives_restart_and_device_edit(tmp_path):
    runtime = Runtime(tmp_path)
    registry(runtime)
    set_record(runtime, "SN-UP", "on")
    # правка устройства (имя) не сбрасывает опцию
    runtime.identities.mutate({"operation": "device", "sn": "SN-UP", "name": "Кухня", "user_id": "u",
                               "device_ids": ["up-id"]})
    assert runtime.identities.record_mode("SN-UP") == "on"
    await runtime.close()
    fresh = Runtime(tmp_path)
    try:
        assert fresh.identities.record_mode("SN-UP") == "on"
        assert fresh.identities.record_mode(None) == "off" and fresh.identities.record_mode("SN-NO") == "off"
    finally:
        await fresh.close()


# --- HTTP ---------------------------------------------------------------------------------

async def test_off_writes_nothing_in_any_route(tmp_path):
    runtime, calls, upstream_runner, runner, url = await start(tmp_path)
    try:
        async with ClientSession() as client:
            for mode in ("emulate", "hybrid", "proxy"):
                runtime.settings["default_route_mode"] = mode
                for path in ("/check_updates", "/no/handler"):
                    async with client.get(url + path, headers=DEVICE):
                        pass
        await runtime.upstream.journal.flush()
        assert runtime.upstream.journal.list()["total"] == 0
        assert runtime.upstream.journal.groups() == []
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_on_records_emulator_and_upstream_answers(tmp_path):
    runtime, calls, upstream_runner, runner, url = await start(tmp_path)
    set_record(runtime, "SN-RC", "on")
    try:
        async with ClientSession() as client:
            # эмуляция: обработанный запрос и промах пишутся с source=emulator
            async with client.get(url + "/check_updates", headers=DEVICE) as response:
                assert response.status == 200
            async with client.get(url + "/no/handler?x=1", headers=DEVICE) as response:
                assert response.status == 404
            runtime.settings["default_route_mode"] = "proxy"
            async with client.get(url + "/check_updates", headers=DEVICE) as response:
                assert (await response.json())["from"] == "upstream"
        await runtime.upstream.journal.flush()
        items = {(i["source"], i["path"]): i for i in runtime.upstream.journal.list()["items"]}
        handled = items[("emulator", "/check_updates")]
        miss = items[("emulator", "/no/handler?x=1")]
        up = items[("upstream", "/check_updates")]
        assert handled["unhandled"] == 0 and miss["unhandled"] == 1 and up["unhandled"] == 0
        assert miss["status"] == 404 and handled["device_sn"] == "SN-RC" and handled["host"] == "quasar.yandex.net"
        assert handled["key"] == "GET quasar.yandex.net /check_updates" and miss["key"] == "GET quasar.yandex.net /no/handler"
        card = runtime.upstream.journal.item(miss["id"])
        assert json.loads(card["response_body"])["error"] == "emulator_handler_not_found"
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_misses_in_emulate_hybrid_and_proxy(tmp_path):
    runtime, calls, upstream_runner, runner, url = await start(tmp_path)
    set_record(runtime, "SN-RC", "misses")
    journal = runtime.upstream.journal
    try:
        async with ClientSession() as client:
            async def hit(path):
                async with client.get(url + path, headers=DEVICE):
                    pass
                await journal.flush()
                return [(i["source"], i["path"]) for i in journal.list()["items"]]

            # эмуляция: пишется только промах (его ответ — 404 эмулятора)
            runtime.settings["default_route_mode"] = "emulate"
            await hit("/check_updates")
            assert await hit("/no/one") == [("emulator", "/no/one")]
            # гибрид: обработанное — локально и не пишется; промах, ушедший наверх, пишется
            runtime.settings["default_route_mode"] = "hybrid"
            await hit("/check_updates")
            assert await hit("/no/two") == [("upstream", "/no/two"), ("emulator", "/no/one")]
            # прокси: запрос, для которого у эмулятора есть обработчик, идёт наверх, но не пишется;
            # без обработчика — пишется (запрос всё равно идёт в Яндекс)
            runtime.settings["default_route_mode"] = "proxy"
            await hit("/check_updates")
            assert await hit("/no/three") == [("upstream", "/no/three"), ("upstream", "/no/two"),
                                              ("emulator", "/no/one")]
            assert all(i["unhandled"] == 1 for i in journal.list()["items"])
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_local_handler_is_side_effect_free(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        handler = runtime.dispatcher.local_handler
        assert handler("quasar.yandex.net", "GET", "/check_updates") is True
        assert handler("quasar.yandex.net", "GET", "/glagol/token") is True
        assert handler("rpc.alice.yandex.net", "GET", "/gproxy/get_app_catalog") is True
        assert handler("quasar.yandex.net", "GET", "/unknown") is False
        assert handler("quasar.yandex.net", "POST", "/check_updates") is False
        assert runtime.endpoints.misses() == {}  # ни note_miss, ни других следов
    finally:
        await runtime.close()


async def test_direct_sync_info_follows_record_option(tmp_path):
    """TASK-052 «Напрямую»: запись — по опции устройства; у get_sync_info обработчик есть,
    поэтому «Нет обработчиков» его не пишет."""
    runtime, calls, upstream_runner, runner, url = await start(tmp_path)
    state = dict(runtime.identities.resolve("rc-id")["state"], quasar_source="direct")
    runtime.identities.mutate({"operation": "state", "sn": "SN-RC", "state": state})
    try:
        async with ClientSession() as client:
            for mode, expected in (("off", 0), ("misses", 0), ("on", 1)):
                set_record(runtime, "SN-RC", mode)
                async with client.get(url + "/get_sync_info?device_id=rc-id", headers=DEVICE) as response:
                    assert response.status == 200
                await runtime.upstream.journal.flush()
                assert runtime.upstream.journal.list()["total"] == expected
        assert runtime.upstream.journal.list()["items"][0]["source"] == "upstream"
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_group_keeps_ten_records_and_counts_all(tmp_path):
    runtime, calls, upstream_runner, runner, url = await start(tmp_path)
    set_record(runtime, "SN-RC", "on")
    try:
        async with ClientSession() as client:
            for index in range(12):
                async with client.get(url + f"/check_updates?n={index}", headers=DEVICE):
                    pass
            async with client.get(url + "/no/handler", headers=DEVICE):
                pass
        await runtime.upstream.journal.flush()
        journal = runtime.upstream.journal
        assert journal.list()["total"] == 11  # 10 записей группы + 1 другого события
        groups = {g["key"]: g for entry in journal.groups() for g in entry["groups"]}
        assert groups["GET quasar.yandex.net /check_updates"]["count"] == 12  # счётчик не теряется при вытеснении
        assert groups["GET quasar.yandex.net /no/handler"]["count"] == 1
        items = journal.group_items("SN-RC", "GET quasar.yandex.net /check_updates")
        assert len(items) == 10
        assert items[0]["path"] == "/check_updates?n=11" and items[-1]["path"] == "/check_updates?n=2"  # старейшие вытеснены
        async with ClientSession(headers=API) as client:
            async with client.get(url + "/emulator/api/traffic?groups=1") as response:
                entry = (await response.json())["devices"][0]
            assert entry["device_sn"] == "SN-RC" and entry["record"] == "on"
            counts = {g["key"]: g["count"] for g in entry["groups"]}
            assert counts["GET quasar.yandex.net /check_updates"] == 12
            async with client.get(url + "/emulator/api/traffic?groups=1&unhandled=1") as response:
                filtered = (await response.json())["devices"][0]["groups"]
            assert [g["key"] for g in filtered] == ["GET quasar.yandex.net /no/handler"]
            async with client.get(url + "/emulator/api/traffic?groups=1&source=upstream") as response:
                assert (await response.json())["devices"] == []
            async with client.get(url + "/emulator/api/traffic?device_sn=SN-RC&key=GET quasar.yandex.net /check_updates") as response:
                assert len((await response.json())["items"]) == 10
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_emulator_records_mask_secret_headers(tmp_path):
    runtime, calls, upstream_runner, runner, url = await start(tmp_path)
    set_record(runtime, "SN-RC", "on")
    try:
        async with ClientSession() as client:
            async with client.get(url + "/check_updates",
                                  headers={**DEVICE, "Authorization": "OAuth emu-secret", "Cookie": "sid=emu-cookie"}):
                pass
        await runtime.upstream.journal.flush()
        item = runtime.upstream.journal.list()["items"][0]
        assert item["source"] == "emulator"
        card = runtime.upstream.journal.item(item["id"])
        headers = [list(h) for h in card["request_headers"]]
        assert ["Authorization", "OAuth ***"] in headers and ["Cookie", "***"] in headers
        raw = b"".join(path.read_bytes() for path in Path(tmp_path).glob("traffic.sqlite3*"))
        assert b"emu-secret" not in raw and b"emu-cookie" not in raw
    finally:
        await stop(runtime, upstream_runner, runner)


# --- WS ------------------------------------------------------------------------------------

WS_HEADERS = {"Host": "uniproxy.local", "x-uprx-device-id": "w-id"}


async def test_ws_emulator_audio_not_recorded_aapi_and_unknown_are(tmp_path):
    runtime, calls, upstream_runner, runner, url = await start(tmp_path, "SN-W", "w-id")
    set_record(runtime, "SN-W", "on")
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url + "/uni.ws", headers=WS_HEADERS) as ws:
                sync = client_frame("SynchronizeStateRequest")
                await ws.send_bytes(sync)
                reply = (await asyncio.wait_for(ws.receive(), 3)).data
                assert parsed(reply, SERVER).Event.WhichOneof("Event") == "SynchronizeStateResponse"
                await ws.send_bytes(b"\x00\x00\x00\x07" + b"audio-secret-bytes")  # звук: канал + данные
                await ws.send_bytes(b"\x00\x00\x00\x07" + b"more")
                await ws.send_str("not-aapi-text")  # неизвестный кадр
                await ws.send_bytes(client_frame("Ping"))  # событие без обработчика
                await asyncio.sleep(0.3)
        items = await ws_records(runtime)
        events = [(i["event"], i["key"]) for i in items]
        assert ("open", "ws open") in events and ("close", "ws close") in events
        assert ("aapi", "ws → Event.SynchronizeStateRequest") in events
        assert ("aapi", "ws ← Event.SynchronizeStateResponse") in events
        assert ("aapi", "ws → Event.Ping") in events
        assert ("unknown", "ws unknown →") in events
        ping = next(i for i in items if i["key"] == "ws → Event.Ping")
        syn = next(i for i in items if i["key"] == "ws → Event.SynchronizeStateRequest")
        response = next(i for i in items if i["key"] == "ws ← Event.SynchronizeStateResponse")
        assert ping["unhandled"] == 1 and syn["unhandled"] == 0
        assert response["ref_message_id"] == syn["message_id"] and response["source"] == "emulator"
        assert all(i["device_sn"] == "SN-W" for i in items)
        journal = runtime.upstream.journal
        card = journal.item(next(i["id"] for i in items if i["event"] == "unknown"))
        assert card["request_body"] == b"not-aapi-text"
        # звук данными не пишется: ни в записях, ни в файле; в закрытии — только счётчики
        close = journal.item(next(i["id"] for i in items if i["event"] == "close"))
        assert (close["audio_in_frames"], close["audio_in_bytes"]) == (2, 30)
        for item in items:
            card = journal.item(item["id"])
            assert b"audio-secret" not in (card["request_body"] or b"") + (card["response_body"] or b"")
        # карточка AAPI — текстовый вид protobuf
        async with ClientSession(headers=API) as client:
            async with client.get(url + f"/emulator/api/traffic?item={ping['id']}") as response:
                body = (await response.json())["request_body"]
            assert body["encoding"] == "protobuf" and "Ping" in body["text"]
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_ws_emulator_misses_records_only_unhandled_events(tmp_path):
    runtime, calls, upstream_runner, runner, url = await start(tmp_path, "SN-W", "w-id")
    set_record(runtime, "SN-W", "misses")
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url + "/uni.ws", headers=WS_HEADERS) as ws:
                await ws.send_bytes(client_frame("SynchronizeStateRequest"))
                await asyncio.wait_for(ws.receive(), 3)
                await ws.send_str("not-aapi-text")
                await ws.send_bytes(client_frame("Ping"))
                await asyncio.sleep(0.3)
        items = await ws_records(runtime, closed=False)
        assert [(i["event"], i["key"]) for i in items] == [("aapi", "ws → Event.Ping")]
        assert items[0]["unhandled"] == 1
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_record_option_change_applies_without_reconnect(tmp_path):
    runtime, calls, upstream_runner, runner, url = await start(tmp_path, "SN-W", "w-id")
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url + "/uni.ws", headers=WS_HEADERS) as ws:
                await ws.send_bytes(client_frame("Ping"))  # запись выключена
                await asyncio.sleep(0.2)
                await runtime.upstream.journal.flush()
                assert runtime.upstream.journal.list()["total"] == 0
                async with client.put(url + "/emulator/api/record_mode", json={"sn": "SN-W", "mode": "misses"},
                                      headers=API) as response:
                    assert response.status == 200
                assert not ws.closed  # соединение не закрывалось
                await ws.send_bytes(client_frame("Ping"))  # то же соединение — уже пишется
                await asyncio.sleep(0.2)
                await runtime.upstream.journal.flush()
                assert [i["key"] for i in runtime.upstream.journal.list()["items"]] == ["ws → Event.Ping"]
                set_record(runtime, "SN-W", "off")
                await ws.send_bytes(client_frame("Ping"))
                await asyncio.sleep(0.2)
                await runtime.upstream.journal.flush()
                assert runtime.upstream.journal.list()["total"] == 1
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_ws_bridge_records_by_option(tmp_path):
    """Прокси: звук — только счётчик; AAPI и неизвестные кадры пишутся; в «Нет обработчиков» —
    события без обработчика и ответы на них по RefMessageId."""
    runtime = Runtime(tmp_path)
    registry(runtime, "SN-W", "w-id")
    upstream_app = web.Application()

    async def uniproxy(request):
        socket = web.WebSocketResponse(autoping=False)
        await socket.prepare(request)
        async for msg in socket:
            if msg.type != WSMsgType.BINARY or not msg.data.startswith(b"AAPI"):
                continue
            incoming = parsed(msg.data, CLIENT)
            event = incoming.Event.WhichOneof("Event")
            if event == "SynchronizeStateRequest":  # ответ на событие с обработчиком
                await socket.send_bytes(server_frame("SynchronizeStateResponse", incoming.Event.Header.MessageId))
            elif event == "Ping":  # ответ на событие без обработчика + посторонний push без ссылки
                await socket.send_bytes(server_frame("LogAck", incoming.Event.Header.MessageId))
                await socket.send_bytes(server_frame("GoAway"))
        return socket

    upstream_app.router.add_route("GET", "/uni.ws", uniproxy)
    upstream_runner, port = await start_upstream_app(upstream_app)
    alias_upstream(runtime, port)
    runtime.settings["default_route_mode"] = "proxy"
    runner, url = await listen(create_app(runtime=runtime))
    journal = runtime.upstream.journal

    async def session(record):
        set_record(runtime, "SN-W", record)
        async with ClientSession() as client:
            async with client.ws_connect(url + "/uni.ws", headers=WS_HEADERS) as ws:
                await ws.send_bytes(client_frame("SynchronizeStateRequest"))
                await asyncio.wait_for(ws.receive(), 3)
                await ws.send_bytes(b"\x00\x00\x00\x09audio-bridge-secret")
                await ws.send_bytes(client_frame("Ping"))
                await asyncio.wait_for(ws.receive(), 3)
                await asyncio.wait_for(ws.receive(), 3)
        for _ in range(60):
            await journal.flush()
            if record != "on" or any(i["event"] == "close" for i in journal.list(limit=500)["items"]):
                break
            await asyncio.sleep(0.05)
        items = [journal.item(i["id"]) for i in journal.list(limit=500)["items"]]  # карточки: до очистки
        journal.forget()
        return items

    try:
        assert await session("off") == []
        keys = [i["key"] for i in await session("misses")]
        assert sorted(keys) == ["ws ← Event.LogAck", "ws → Event.Ping"]  # без sync-пары и без GoAway
        items = await session("on")
        keys = {i["key"] for i in items}
        assert {"ws open", "ws close", "ws → Event.SynchronizeStateRequest", "ws ← Event.SynchronizeStateResponse",
                "ws → Event.Ping", "ws ← Event.LogAck", "ws ← Event.GoAway"} <= keys
        assert all(i["source"] == "upstream" and i["device_sn"] == "SN-W" for i in items)
        close = next(i for i in items if i["event"] == "close")
        assert close["audio_in_frames"] == 1 and close["audio_in_bytes"] == 4 + len(b"audio-bridge-secret")
        for card in items:
            assert b"audio-bridge" not in (card["request_body"] or b"") + (card["response_body"] or b"")
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()


async def start_upstream_app(app):
    runner, url = await listen(app)
    return runner, int(url.rsplit(":", 1)[1])


# --- журнал: миграция старых записей ------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE exchanges(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
  direction TEXT, event TEXT, host TEXT, method TEXT, path TEXT, status INTEGER,
  device_sn TEXT, device_id TEXT, request_size INTEGER, response_size INTEGER,
  duration_ms REAL, source TEXT, error TEXT, close_code INTEGER,
  request_headers TEXT, response_headers TEXT,
  request_body BLOB, response_body BLOB, body_truncated INTEGER NOT NULL DEFAULT 0,
  session TEXT, frames_truncated INTEGER NOT NULL DEFAULT 0);
CREATE TABLE ws_frames(
  exchange_id INTEGER NOT NULL REFERENCES exchanges(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL, ts REAL NOT NULL, direction TEXT NOT NULL,
  binary INTEGER NOT NULL, size INTEGER NOT NULL, data BLOB);
"""


async def test_journal_migration_keys_groups_and_audio(tmp_path):
    import zstandard
    path = Path(tmp_path) / "traffic.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(OLD_SCHEMA)
    db.execute("PRAGMA user_version=1")  # журнал TASK-043: маскирование уже было, групп нет
    for index in range(14):  # 14 одинаковых событий одного устройства + 1 другое + без устройства
        db.execute("INSERT INTO exchanges(ts,kind,host,method,path,status,device_sn,source) "
                   "VALUES(?,?,?,?,?,?,?,?)", (100 + index, "http", "q.test", "GET", f"/same?i={index}", 200, "SN-1", "upstream"))
    db.execute("INSERT INTO exchanges(ts,kind,host,method,path,status,device_sn,source) "
               "VALUES(200,'http','q.test','POST','/other',200,'SN-1','upstream')")
    db.execute("INSERT INTO exchanges(ts,kind,host,method,path,status,source) "
               "VALUES(201,'http','q.test','GET','/anon',200,'upstream')")
    db.execute("INSERT INTO exchanges(ts,kind,direction,event,host,path,device_sn,session,source) "
               "VALUES(300,'ws','lifecycle','open','u.test','/uni.ws','SN-1','s1','upstream')")
    session_id = db.execute("SELECT id FROM exchanges WHERE kind='ws'").fetchone()[0]
    for sequence, (direction, binary, data) in enumerate([
            ("client_to_server", 1, b"\x00\x00\x00\x01audio"), ("server_to_client", 1, b"\x00\x00\x00\x01more-audio"),
            ("client_to_server", 1, b"AAPI\x0a\x00"), ("client_to_server", 0, b"text-frame")]):
        db.execute("INSERT INTO ws_frames VALUES(?,?,?,?,?,?,?)",
                   (session_id, sequence, 300.0, direction, binary, len(data), zstandard.compress(data)))
    db.commit()
    db.close()

    journal = TrafficJournal(path)
    try:
        groups = {(e["device_sn"], g["key"]): g for e in journal.groups() for g in e["groups"]}
        same = groups[("SN-1", "GET q.test /same")]
        assert same["count"] == 14  # прежнее число случаев сохранено
        assert len(journal.group_items("SN-1", "GET q.test /same")) == 10  # записей — не больше 10
        assert (None, "GET q.test /anon") in groups and ("SN-1", "ws open") in groups
        assert journal.list(device="SN-1")["total"] == 10 + 1 + 1
        card = journal.item(session_id)
        assert (card["audio_in_frames"], card["audio_out_frames"]) == (1, 1)  # звук старых кадров — только счётчики
        assert card["audio_in_bytes"] == len(b"\x00\x00\x00\x01audio")
        assert [f["data"] for f in card["frames"]] == [b"AAPI\x0a\x00", b"text-frame"]  # AAPI и текст остались
    finally:
        await journal.close()
    reopened = TrafficJournal(path)  # однократно: повторное открытие ничего не меняет
    try:
        assert reopened.list()["total"] == 13
    finally:
        await reopened.close()

