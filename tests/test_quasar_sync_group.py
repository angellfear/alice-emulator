"""TASK-048: стереопары в реестре (groups) и ключ group в /get_sync_info, /glagol/device_list."""
import pytest
from aiohttp import ClientSession

from server_emulator.identity import IdentityRegistry
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from server_emulator.state_store import StateStore
from tests.conftest import listen

HEADERS = {"Host": "quasar.yandex.net"}
PAIR = [{"sn": "SN-1", "role": "leader"}, {"sn": "SN-2", "role": "follower"}]


def seed(identities):
    for owner in ("alice", "bob"):
        identities.mutate({"operation": "user", "id": owner, "name": owner})
    for sn, owner, alias in (("SN-1", "alice", "id-1"), ("SN-2", "alice", "id-2"),
                             ("SN-3", "alice", "id-3"), ("SN-B", "bob", "id-b")):
        identities.mutate({"operation": "device", "sn": sn, "user_id": owner, "device_ids": [alias],
                           "name": sn, "profile": {"platform": "yandexstation"}})
        identities.mutate({"operation": "state", "sn": sn, "state": {
            "quasar": {"config": {"device_config": {"name": sn}}, "tags": []}}})


def registry(tmp_path):
    identities = IdentityRegistry(tmp_path, StateStore(tmp_path / "emulator.sqlite3"))
    seed(identities)
    return identities


# --- реестр ---------------------------------------------------------------------


def test_group_create_update_delete_and_persistence(tmp_path):
    identities = registry(tmp_path)
    data = identities.mutate({"operation": "group", "name": "Пара", "secret": "s3cr3t", "devices": PAIR})
    group = data["groups"][0]
    assert group["id"] == 1 and group["secret"] == "s3cr3t" and group["name"] == "Пара"
    assert identities.group_of("SN-1")["id"] == 1 and identities.group_of("SN-3") is None
    # изменение по id: секрет сохраняется, роли можно поменять местами
    swapped = [{"sn": "SN-1", "role": "follower"}, {"sn": "SN-2", "role": "leader"}]
    identities.mutate({"operation": "group", "id": 1, "name": "Пара 2", "devices": swapped})
    updated = identities.group_of("SN-1")
    assert updated["name"] == "Пара 2" and updated["secret"] == "s3cr3t"
    assert {m["sn"]: m["role"] for m in updated["devices"]} == {"SN-1": "follower", "SN-2": "leader"}
    # группы переживают перезагрузку реестра и обычные мутации
    identities.mutate({"operation": "user", "id": "alice", "name": "Alice"})
    reopened = IdentityRegistry(tmp_path, StateStore(tmp_path / "emulator.sqlite3"))
    assert reopened.group_of("SN-2")["name"] == "Пара 2"
    reopened.mutate({"operation": "group_delete", "id": 1})
    assert reopened.data["groups"] == []
    with pytest.raises(ValueError, match="не найдена"):
        reopened.mutate({"operation": "group_delete", "id": 1})


def test_group_validation(tmp_path):
    identities = registry(tmp_path)

    def create(devices, **extra):
        return identities.mutate({"operation": "group", "name": "G", "devices": devices, **extra})

    with pytest.raises(ValueError, match="ровно 2"):
        create(PAIR[:1])
    with pytest.raises(ValueError, match="ровно один leader"):
        create([{"sn": "SN-1", "role": "leader"}, {"sn": "SN-2", "role": "leader"}])
    with pytest.raises(ValueError, match="роль"):
        create([{"sn": "SN-1", "role": "leader"}, {"sn": "SN-2", "role": "master"}])
    with pytest.raises(ValueError, match="различаться"):
        create([{"sn": "SN-1", "role": "leader"}, {"sn": "SN-1", "role": "follower"}])
    with pytest.raises(ValueError, match="одному владельцу"):
        create([{"sn": "SN-1", "role": "leader"}, {"sn": "SN-B", "role": "follower"}])
    with pytest.raises(ValueError, match="не найден"):
        create([{"sn": "SN-1", "role": "leader"}, {"sn": "NOPE", "role": "follower"}])
    assert identities.data["groups"] == []
    create(PAIR)
    with pytest.raises(ValueError, match="другой группе"):
        create([{"sn": "SN-2", "role": "leader"}, {"sn": "SN-3", "role": "follower"}])
    with pytest.raises(ValueError, match="не найдена"):
        identities.mutate({"operation": "group", "id": 99, "name": "G", "devices": PAIR})


def test_group_dissolves_with_device_or_owner_change(tmp_path):
    identities = registry(tmp_path)
    identities.mutate({"operation": "group", "name": "G", "devices": PAIR})
    identities.mutate({"operation": "device_delete", "sn": "SN-2"})
    assert identities.data["groups"] == []  # без второго устройства пары нет
    identities.mutate({"operation": "group", "name": "G", "devices": [
        {"sn": "SN-1", "role": "leader"}, {"sn": "SN-3", "role": "follower"}]})
    identities.mutate({"operation": "user_delete", "id": "alice"})  # устройства без владельца
    assert identities.data["groups"] == []


# --- HTTP -----------------------------------------------------------------------


async def get(client, url, path):
    async with client.get(url + path, headers=HEADERS) as response:
        return response.status, await response.json()


async def test_sync_info_and_device_list_carry_group(tmp_path):
    runtime = Runtime(tmp_path)
    seed(runtime.identities)
    runtime.identities.mutate({"operation": "group", "name": "groupname", "secret": "s3cr3t", "devices": PAIR})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            status, body = await get(client, url, "/get_sync_info?device_id=id-1")
            assert status == 200
            assert body["group"] == {"id": 1, "name": "groupname", "secret": "s3cr3t", "devices": [
                {"id": "id-1", "platform": "yandexstation", "role": "leader"},
                {"id": "id-2", "platform": "yandexstation", "role": "follower"}]}
            status, body = await get(client, url, "/get_sync_info?device_id=id-2")
            assert body["group"]["id"] == 1 and body["group"]["devices"][1]["role"] == "follower"
            # вне группы — без ключа (и свой владелец, и чужой)
            for alias in ("id-3", "id-b"):
                status, body = await get(client, url, "/get_sync_info?device_id=" + alias)
                assert status == 200 and "group" not in body
            # device_list: group.devices с ролями только у участников пары
            status, body = await get(client, url, "/glagol/device_list?device_id=id-1")
            by_id = {d["id"]: d for d in body["devices"]}
            assert set(by_id) == {"id-1", "id-2", "id-3"}
            expected = [{"id": "id-1", "platform": "yandexstation", "role": "leader"},
                        {"id": "id-2", "platform": "yandexstation", "role": "follower"}]
            assert by_id["id-1"]["group"] == {"devices": expected}
            assert by_id["id-2"]["group"] == {"devices": expected}
            assert "group" not in by_id["id-3"]
    finally:
        await runner.cleanup()
        await runtime.close()
