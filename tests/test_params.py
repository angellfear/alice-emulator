"""Параметры: стор (каталог/значения/секреты/цепочка), админ-API, кабинет /me + роли."""
import json

import pytest
from aiohttp import ClientSession

from server_emulator.params import ParamStore, ParamError
from server_emulator.runtime import Runtime


@pytest.fixture
def store(tmp_path):
    runtime = Runtime(directory=tmp_path)
    yield runtime.quasar_params
    runtime.store.close()


def test_defs_crud_and_validation(store):
    store.def_upsert({"key": "server.timezone", "scope": "server", "type": "string",
                      "title": "Часовой пояс", "user_editable": False, "default_value": "Asia/Tashkent"})
    assert store.def_get("server.timezone")["default_value"] == "Asia/Tashkent"
    with pytest.raises(ParamError, match="server."):
        store.def_upsert({"key": "tz", "scope": "server", "type": "string"})  # префикс не тот
    with pytest.raises(ParamError, match="options"):
        store.def_upsert({"key": "user.scheme", "scope": "user", "type": "select"})  # select без вариантов
    with pytest.raises(ParamError, match="type"):
        store.def_upsert({"key": "user.x", "scope": "user", "type": "картина"})
    store.def_remove("server.timezone")
    assert store.def_get("server.timezone") is None


def test_values_validation_and_secret_masking(store):
    store.def_upsert({"key": "user.api_token", "scope": "user", "type": "secret", "title": "Токен"})
    store.def_upsert({"key": "user.volume", "scope": "user", "type": "number",
                      "options": {"min": 0, "max": 10}, "title": "Громкость"})
    store.set_value("user:u1", "user.api_token", "s3cr3t")
    assert store.values("user:u1")["user.api_token"]["value"] == "__KEEP__"  # маска
    assert store.raw_value("user:u1", "user.api_token") == "s3cr3t"
    store.set_value("user:u1", "user.api_token", "__KEEP__")  # маска не затирает
    assert store.raw_value("user:u1", "user.api_token") == "s3cr3t"
    with pytest.raises(ParamError, match="вне"):
        store.set_value("user:u1", "user.volume", 99)
    with pytest.raises(ParamError, match="не определён"):
        store.set_value("user:u1", "user.unknown", 1)


def test_resolve_chain_device_user_server_default(store):
    store.def_upsert({"key": "server.timezone", "scope": "server", "type": "string",
                      "default_value": "Asia/Tashkent"})
    store.def_upsert({"key": "user.wifi_hash", "scope": "user", "type": "string"})
    store.set_value("server", "server.timezone", "UTC")
    store.set_value("user:u1", "server.timezone", "Asia/Omsk")       # пользователь перекрывает сервер
    store.set_value("user:u1", "user.wifi_hash", "hash-1")
    store.set_value("device:SN1", "user.wifi_hash", "hash-device")   # устройство перекрывает пользователя
    assert store.resolve("server.timezone") == "UTC"
    assert store.resolve("server.timezone", user_id="u1") == "Asia/Omsk"
    assert store.resolve("user.wifi_hash", user_id="u1") == "hash-1"
    assert store.resolve("user.wifi_hash", user_id="u1", sn="SN1") == "hash-device"
    with pytest.raises(KeyError):
        store.resolve("user.wifi_hash")  # без владельца и значения — нет
    store.def_upsert({"key": "user.city", "scope": "user", "type": "string", "default_value": "Пермь"})
    assert store.resolve("user.city") == "Пермь"  # default_value


def test_seed_idempotent_with_legacy(tmp_path):
    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({"operation": "user", "id": "u1", "name": "U",
                               "location": {"latitude": 55.7, "longitude": 37.6},
                               "fields": {"wifi_hash": "abc"}})
    runtime.quasar_params.seed(runtime.identities.data)   # повторный сид — ничего не ломает
    runtime.quasar_params.seed(runtime.identities.data)
    assert runtime.quasar_params.raw_value("user:u1", "user.wifi_hash") == "abc"
    assert runtime.quasar_params.raw_value("user:u1", "user.location.latitude") == 55.7
    # $param читает стор: шаблонная сборка получает значение параметра
    from server_emulator.quasar_config import compose
    class R:
        identities = runtime.identities
        quasar_platforms = runtime.quasar_platforms
        quasar_params = runtime.quasar_params
        sessions = {}
    device = {"sn": "SN1", "device_ids": ["SN1"], "name": "N", "user_id": "u1",
              "profile": {}, "fields": {}}
    body = compose({"wifi": {"$param": "user.wifi_hash"}}, None, [], R, device)
    assert body == {"wifi": "abc"}
    runtime.store.close()


@pytest.mark.asyncio
def test_resolve_view_sources_and_secret(store):
    """TASK-060: итог цепочки с уровнем-источником по каждому ключу; секрет — маской."""
    store.def_upsert({"key": "server.timezone", "scope": "server", "type": "string",
                      "default_value": "Asia/Tashkent"})
    store.def_upsert({"key": "user.wifi_hash", "scope": "user", "type": "secret"})
    store.def_upsert({"key": "device.room", "scope": "device", "type": "string"})
    store.set_value("server", "server.timezone", "UTC")
    store.set_value("user:u1", "user.wifi_hash", "s3cr3t")
    view = store.resolve_view(sn="SN1", user_id="u1")
    assert view["server.timezone"] == {"value": "UTC", "source": "server"}
    assert view["user.wifi_hash"] == {"value": "__KEEP__", "source": "user"}  # секрет — маской
    assert view["device.room"]["source"] == "default"
    store.set_value("device:SN1", "device.room", "Кухня")
    assert store.resolve_view(sn="SN1", user_id="u1")["device.room"] == {"value": "Кухня", "source": "device"}


async def test_params_admin_api(tmp_path):
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/params") as response:
                body = await response.json()
                keys = [d["key"] for d in body["defs"]]
                assert "user.wifi_hash" in keys and "device.room" in keys
            async with client.put(url + "/emulator/api/params", headers={"X-Emulator": "1"},
                                  json={"definition": {"key": "server.house_name", "scope": "server",
                                                       "type": "string", "title": "Имя дома",
                                                       "user_editable": False}}) as response:
                assert (await response.json())["saved"] == "server.house_name"
            async with client.put(url + "/emulator/api/param_values", headers={"X-Emulator": "1"},
                                  json={"owner": "server", "key": "server.house_name",
                                        "value": "Дом у реки"}) as response:
                assert response.status == 200
            async with client.get(url + "/emulator/api/param_values?owner=server") as response:
                assert (await response.json())["values"]["server.house_name"]["value"] == "Дом у реки"
            # TASK-060: для устройства — resolve=1 (значение + уровень-источник), секрет маской;
            # админ правит значение другого пользователя
            runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'Alex'})
            runtime.identities.mutate({'operation': 'device', 'sn': 'SN1', 'user_id': 'u1', 'device_ids': []})
            async with client.put(url + "/emulator/api/params", headers={"X-Emulator": "1"},
                                  json={"definition": {"key": "user.api_token", "scope": "user",
                                                       "type": "secret", "title": "Токен"}}) as response:
                assert response.status == 200
            async with client.put(url + "/emulator/api/param_values", headers={"X-Emulator": "1"},
                                  json={"owner": "user:u1", "key": "user.api_token", "value": "s3cr3t"}) as response:
                assert response.status == 200
            async with client.put(url + "/emulator/api/param_values", headers={"X-Emulator": "1"},
                                  json={"owner": "device:SN1", "key": "device.room", "value": "Кухня"}) as response:
                assert response.status == 200
            async with client.get(url + "/emulator/api/param_values?owner=device:SN1&resolve=1") as response:
                body = await response.json()
            assert body["resolved"]["device.room"] == {"value": "Кухня", "source": "device"}
            assert body["resolved"]["user.api_token"] == {"value": "__KEEP__", "source": "user"}  # маска
            assert "user.api_token" not in body["values"]  # values — только свои значения владельца
            async with client.put(url + "/emulator/api/param_values", headers={"X-Emulator": "1"},
                                  json={"owner": "user:u1", "key": "user.api_token", "value": "__KEEP__"}) as response:
                assert response.status == 200  # маска не затирает чужой секрет
            assert runtime.quasar_params.raw_value("user:u1", "user.api_token") == "s3cr3t"
    finally:
        await runner.cleanup()
        runtime.store.close()
