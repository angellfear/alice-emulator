"""B03: единый resolver устройств — collect_device_id, pending с source/ip,
header/sync mismatch → закрытие соединения, миграция колонок unassigned."""
import asyncio
import json

import pytest
from aiohttp import ClientSession
from aiohttp import web

from server_emulator.identity import IdentityRegistry, collect_device_id
from server_emulator.state_store import StateStore

OWNER = {"users": [{"id": "u1", "name": "A", "profile": {}},
                   {"id": "u2", "name": "B", "profile": {}}],
         "devices": [
             {"sn": "SN-1", "user_id": "u1", "device_ids": ["dev-a", "dev-b"], "profile": {}, "state": {}},
             {"sn": "SN-2", "user_id": "u1", "device_ids": ["dev-c"], "profile": {}, "state": {}},
             {"sn": "SN-3", "user_id": "u2", "device_ids": ["dev-d"], "profile": {}, "state": {}},
         ],
         "unassigned": []}


def registry(tmp_path, data=OWNER):
    store = StateStore(tmp_path / "emulator.sqlite3")
    store.save_identity_data(data)
    return IdentityRegistry(tmp_path, store)


# --- collect_device_id --------------------------------------------------------


def test_collect_empty_and_single_source():
    device_id, report = collect_device_id(("query.device_id", None), ("header.x", ""))
    assert device_id == ""
    assert report == {"sources": {}, "ids": [], "mismatch": False, "resolved_from": None}
    device_id, report = collect_device_id(("query.device_id", " dev-x "))
    assert device_id == "dev-x"
    assert report["resolved_from"] == "query.device_id"
    assert not report["mismatch"]


def test_collect_priority_and_mismatch_report():
    device_id, report = collect_device_id(
        ("query.device_id", "dev-q"),
        ("header.x-yaquasar-device-id", "dev-h"),
        ("header.x-uprx-device-id", "dev-q"),
    )
    assert device_id == "dev-q"  # приоритет первого источника
    assert report["mismatch"] is True
    assert report["ids"] == ["dev-q", "dev-h"]
    assert report["resolved_from"] == "query.device_id"


def test_collect_same_id_from_two_sources_is_not_mismatch():
    _, report = collect_device_id(("sync.Application.DeviceId", "dev-1"),
                                  ("header.x-uprx-device-id", "dev-1"))
    assert report["mismatch"] is False


# --- observe: get_sync регистрирует устройство без владельца -------------------


def test_observe_unknown_registers_unbound_device(tmp_path):
    identities = registry(tmp_path)
    device = identities.observe("dev-new", source="query.device_id", ip="192.0.2.107", register=True)
    # колонка представилась по get_sync: сразу в списке, без владельца
    assert device and device["sn"] == "dev-new" and device["user_id"] is None
    assert device["device_ids"] == ["dev-new"]
    assert identities.data["unassigned"] == []


def test_observe_repeat_keeps_single_device_and_refreshes_activity(tmp_path):
    identities = registry(tmp_path)
    identities.observe("dev-new", source="header.x-uprx-device-id", ip="192.0.2.11", register=True)
    identities.observe("dev-new", source="sync.Application.DeviceId", ip="192.0.2.12", register=True)
    assert len([d for d in identities.data["devices"] if d["sn"] == "dev-new"]) == 1
    assert identities.activity["dev-new"]["ip"] == "192.0.2.12"  # последний наблюдённый


def test_observe_known_device_does_not_create_rows(tmp_path):
    identities = registry(tmp_path)
    device = identities.observe("dev-a", source="query.device_id", ip="1.2.3.4", register=True)
    assert device["sn"] == "SN-1"
    assert identities.data["unassigned"] == []
    assert len(identities.data["devices"]) == 3  # известных не дублируем


def test_two_owners_and_two_devices_resolve_independently(tmp_path):
    identities = registry(tmp_path)
    assert identities.resolve("dev-a")["sn"] == "SN-1"
    assert identities.resolve("dev-b")["sn"] == "SN-1"   # два устройства одного владельца
    assert identities.resolve("dev-c")["sn"] == "SN-2"
    assert identities.resolve("dev-d")["user_id"] == "u2"  # второй владелец
    assert identities.resolve("dev-unknown") is None


def test_alias_hijack_to_other_sn_rejected(tmp_path):
    identities = registry(tmp_path)
    with pytest.raises(ValueError, match="уже привязан"):
        identities.mutate({"operation": "device", "sn": "SN-3", "user_id": "u2",
                           "device_ids": ["dev-d", "dev-a"], "name": "X"})


# --- store: аддитивная миграция колонок unassigned ---------------------------


def test_old_db_without_source_ip_columns_migrated(tmp_path):
    db_path = tmp_path / "emulator.sqlite3"
    store = StateStore(db_path)
    store.close()
    # откатываем к старой схеме без source/ip, как в БД до B03
    import sqlite3
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE unassigned_old(device_id TEXT PRIMARY KEY, first_seen REAL, last_seen REAL)")
    db.execute("DROP TABLE unassigned")
    db.execute("ALTER TABLE unassigned_old RENAME TO unassigned")
    db.execute("INSERT INTO unassigned VALUES('dev-legacy', 1.0, 2.0)")
    db.commit()
    db.close()
    store = StateStore(db_path)  # повторное открытие добавляет колонки
    data = store.load_identity_data()
    entry = next(d for d in data["unassigned"] if d["device_id"] == "dev-legacy")
    assert entry["source"] is None and entry["ip"] is None
    assert entry["first_seen"] == 1.0
    store.save_identity_data(data)  # roundtrip с новыми полями
    assert store.load_identity_data()["unassigned"][0]["device_id"] == "dev-legacy"
    store.close()


# --- runtime: HTTP mismatch журналируется, WS mismatch закрывает --------------


def _ws_client_message(device_id=None):
    from server_emulator.schema import load_type
    import uuid
    cls = load_type("server_emulator/schemas", "alice-api.pb", "NAlice.NAliceApi.TClientMessage")
    message = cls()
    message.Event.Header.MessageId = uuid.uuid4().hex
    sync = message.Event.SynchronizeStateRequest
    sync.Application.DeviceId = device_id or ""
    return b"AAPI" + message.SerializeToString()


@pytest.mark.asyncio
async def test_ws_header_sync_mismatch_closes_connection(tmp_path):
    from server_emulator.server import create_app
    from server_emulator.runtime import Runtime

    runtime = Runtime(directory=tmp_path)
    runner = web.AppRunner(create_app(runtime=runtime))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        async with ClientSession() as client:
            async with client.ws_connect(f"http://127.0.0.1:{port}/uni.ws",
                                         headers={"x-uprx-device-id": "dev-header"}) as ws:
                await ws.send_bytes(_ws_client_message(device_id="dev-sync-other"))
                closed = await ws.receive(timeout=5)
                assert closed.type.name in ("CLOSE", "CLOSING", "CLOSED")
                assert ws.close_code == 1008
        assert any(r.get("kind") == "ws" and "device_id_report" in r for r in runtime.recent)
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_ws_header_sync_agreement_keeps_connection(tmp_path):
    from server_emulator.server import create_app
    from server_emulator.runtime import Runtime

    runtime = Runtime(directory=tmp_path)
    runner = web.AppRunner(create_app(runtime=runtime))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        async with ClientSession() as client:
            async with client.ws_connect(f"http://127.0.0.1:{port}/uni.ws",
                                         headers={"x-uprx-device-id": "dev-same"}) as ws:
                await ws.send_bytes(_ws_client_message(device_id="dev-same"))
                await asyncio.sleep(0.3)  # соединение живо: сервер не закрыл
                assert not ws.closed
                await ws.close()
        devices = {d["sn"]: d for d in runtime.identities.data["devices"]}
        # WS header/sync — представление устройства: регистрируется без владельца
        assert devices["dev-same"]["user_id"] is None
        assert runtime.identities.data["unassigned"] == []
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_http_device_id_mismatch_logged_and_priority_resolved(tmp_path):
    from server_emulator.server import create_app
    from server_emulator.runtime import Runtime
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    value = runtime.updates.settings()
    value["policy"] = "deny"
    runtime.updates.save(value)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/tv/api/v4/updates?device_id=dev-q",
                                   headers={"Host": "quasar.yandex.net", "X-YaQuasarDeviceId": "dev-h"},
                                   json={"installed_apps": []}) as response:
                assert response.status == 200  # расхождение не роняет HTTP
        mismatches = [r for r in runtime.recent if r.get("kind") == "http" and "device_id_report" in r]
        assert mismatches and mismatches[-1]["device_id_report"]["ids"] == ["dev-q", "dev-h"]
        # OTA-запрос с маркером тоже представление: dev-q — строка реестра без владельца
        devices = {d["sn"]: d for d in runtime.identities.data["devices"]}
        assert devices["dev-q"]["user_id"] is None
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_any_request_with_marker_populates_registry(tmp_path):
    """Любой запрос с маркером устройства наполняет таблицу: /get_sync_info и query
    прочих маршрутов, заголовки WS/sync, заголовки вызывающего /glagol/token,
    JSON X-Ya-Application у rpc-запросов. Цель токена (query чужого устройства)
    не регистрируется (v9 §8)."""
    from server_emulator.server import create_app
    from server_emulator.runtime import Runtime
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/get_sync_info?device_id=dev-sync",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 403  # без владельца — device_not_assigned
            async with client.get(url + "/check_updates?device_id=dev-other",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 200
            async with client.post(url + "/gproxy/push_events_batch",
                                   headers={"Host": "rpc.alice.yandex.net",
                                            "X-Ya-Application": '{"app_id":"ru.yandex.centaur","device_id":"dev-rpc"}'},
                                   json={}) as response:
                assert response.status == 404  # эндпоинт не реализован — маркер всё равно принят
            async with client.get(url + "/glagol/token?device_id=dev-target",
                                  headers={"Host": "quasar.yandex.net", "X-YaQuasarDeviceId": "dev-caller"}) as response:
                assert response.status == 403  # вызывающий без владельца — caller_not_assigned
        devices = {d["sn"]: d for d in runtime.identities.data["devices"]}
        # каждый маркер → строка в таблице, сразу и без владельца
        for sn in ("dev-sync", "dev-other", "dev-rpc", "dev-caller"):
            assert sn in devices and devices[sn]["user_id"] is None
        assert "dev-target" not in devices  # цель токена — resolve-only (v9 §8)
        assert runtime.identities.data["unassigned"] == []
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_client_ip_resolved_through_front_peer(tmp_path):
    """Встроенный режим: соединение приходит внутренним хопом с 127.0.0.1 —
    реальный IP колонки берётся у внедрённого client_peer (таблица пиров фронта)."""
    from server_emulator.server import create_app
    from server_emulator.runtime import Runtime
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.client_peer = lambda request: ("192.0.2.84", 54321)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/check_updates?device_id=dev-ip",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 200
        assert runtime.identities.activity["dev-ip"]["ip"] == "192.0.2.84"
    finally:
        await runner.cleanup()
        await runtime.close()
