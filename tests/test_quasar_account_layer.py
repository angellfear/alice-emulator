"""TASK-047: слой аккаунта (quasar_overrides пользователя) между платформой и устройством."""
import pytest
from aiohttp import ClientSession

from server_emulator.identity import IdentityRegistry
from server_emulator.quasar_config import (QuasarConfigError, account_layer, compose,
                                           validate_account_overrides)
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from server_emulator.state_store import StateStore
from tests.conftest import listen

HEADERS = {"Host": "quasar.yandex.net"}
COMMON = {"config": {"account_config": {"spotter": "alisa", "jingle": True},
                     "device_config": {"name": "x"}, "system_config": {}}}
SPOTTER = "config.account_config.spotter"


def user_overrides(user_id, items):
    return {"operation": "user_quasar_overrides", "id": user_id, "quasar_overrides": items}


async def make_runtime(tmp_path):
    runtime = Runtime(directory=tmp_path)
    for user in ("alice", "bob"):
        runtime.identities.mutate({"operation": "user", "id": user, "name": user.title()})
    for sn, user, alias in (("SN-1", "alice", "id-1"), ("SN-2", "alice", "id-2"), ("SN-B", "bob", "id-b")):
        runtime.identities.mutate({"operation": "device", "sn": sn, "user_id": user, "device_ids": [alias],
                                   "profile": {"platform": "chiron"}})
    runtime.identities.mutate({"operation": "device", "sn": "SN-U", "user_id": None, "device_ids": ["id-u"],
                               "profile": {"platform": "chiron"}})
    runtime.quasar_platforms.save_common(COMMON)
    runtime.quasar_platforms.save("chiron", {"config": {"device_config": {"platform": "chiron"}}})
    return runtime


async def sync(client, url, alias):
    async with client.get(url + "/get_sync_info?device_id=" + alias, headers=HEADERS) as response:
        return response.status, await response.json()


@pytest.mark.asyncio
async def test_user_override_reaches_all_user_devices(tmp_path):
    runtime = await make_runtime(tmp_path)
    runtime.identities.mutate(user_overrides("alice", [{"path": SPOTTER, "source": "jarvis"}]))
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            for alias in ("id-1", "id-2"):
                status, body = await sync(client, url, alias)
                assert status == 200
                assert body["config"]["account_config"]["spotter"] == "jarvis"
            _, other = await sync(client, url, "id-b")  # чужой пользователь не затронут
            assert other["config"]["account_config"]["spotter"] == "alisa"
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_device_override_stronger_than_user(tmp_path):
    runtime = await make_runtime(tmp_path)
    runtime.identities.mutate(user_overrides("alice", [{"path": SPOTTER, "source": "jarvis"}]))
    state = dict(runtime.identities.resolve("id-2")["state"])
    state["quasar_overrides"] = [{"path": SPOTTER, "source": "computer"}]
    runtime.identities.mutate({"operation": "state", "sn": "SN-2", "state": state})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            _, first = await sync(client, url, "id-1")
            _, second = await sync(client, url, "id-2")
            assert first["config"]["account_config"]["spotter"] == "jarvis"
            assert second["config"]["account_config"]["spotter"] == "computer"
    finally:
        await runner.cleanup()
        await runtime.close()


def test_path_outside_account_config_is_error(tmp_path):
    with pytest.raises(QuasarConfigError):
        validate_account_overrides([{"path": "config.device_config.name", "source": "x"}])
    with pytest.raises(QuasarConfigError):
        validate_account_overrides([{"path": "config.account_config", "source": {}}])
    with pytest.raises(QuasarConfigError):
        validate_account_overrides([{"path": "subscription", "source": 1}])
    identities = IdentityRegistry(tmp_path, StateStore(tmp_path / "emulator.sqlite3"))
    identities.mutate({"operation": "user", "id": "u", "name": "U"})
    with pytest.raises(ValueError, match="вне config.account_config"):
        identities.mutate(user_overrides("u", [{"path": "config.system_config.x", "source": 1}]))
    with pytest.raises(ValueError, match="не найден"):
        identities.mutate(user_overrides("zzz", []))
    # compose сам тоже не пропускает путь вне account_config (в обход реестра)
    runtime = type("R", (), {"identities": identities, "sessions": {}})()
    with pytest.raises(QuasarConfigError):
        compose(COMMON, None, None, runtime, None,
                account_overrides=[{"path": "config.device_config.name", "source": "x"}])


@pytest.mark.asyncio
async def test_unassigned_device_has_no_account_layer(tmp_path):
    runtime = await make_runtime(tmp_path)
    runtime.identities.mutate(user_overrides("alice", [{"path": SPOTTER, "source": "jarvis"}]))
    unbound = runtime.identities.resolve("id-u")
    assert account_layer(runtime, unbound) == ([], None)
    assert account_layer(runtime, runtime.identities.resolve("id-1")) == (
        [{"path": SPOTTER, "source": "jarvis"}], "Alice")
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            status, _ = await sync(client, url, "id-u")
            assert status == 403  # без владельца ответа нет вовсе
            async with client.get(url + "/emulator/api/quasar_composed?device=SN-U") as response:
                body = await response.json()
            assert body["config"]["config"]["account_config"]["spotter"] == "alisa"
            assert "аккаунт" not in " ".join(body["origins"].values())
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_trace_signs_account_layer(tmp_path):
    runtime = await make_runtime(tmp_path)
    runtime.identities.mutate(user_overrides("alice", [
        {"path": SPOTTER, "source": "jarvis"},
        {"path": "config.account_config.jingle", "source": {"delete": True}}]))
    state = dict(runtime.identities.resolve("id-2")["state"])
    state["quasar_overrides"] = [{"path": SPOTTER, "source": "computer"}]
    runtime.identities.mutate({"operation": "state", "sn": "SN-2", "state": state})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/quasar_composed?device=SN-1") as response:
                body = await response.json()
            origins = body["origins"]
            assert origins[SPOTTER] == "аккаунт Alice"
            assert origins["config.account_config.jingle"].startswith("аккаунт Alice")
            assert "jingle" not in body["config"]["config"]["account_config"]
            async with client.get(url + "/emulator/api/quasar_composed?device=SN-2") as response:
                body = await response.json()
            assert body["config"]["config"]["account_config"]["spotter"] == "computer"
            assert body["origins"][SPOTTER] == "переопределение устройства"
    finally:
        await runner.cleanup()
        await runtime.close()


def test_storage_survives_reload_rename_and_delete(tmp_path):
    identities = IdentityRegistry(tmp_path, StateStore(tmp_path / "emulator.sqlite3"))
    identities.mutate({"operation": "user", "id": "u", "name": "U"})
    data = identities.mutate(user_overrides("u", [{"path": SPOTTER, "source": "jarvis"}]))
    assert data["users"][0]["quasar_overrides"] == [{"path": SPOTTER, "source": "jarvis"}]
    identities.mutate({"operation": "user", "id": "u", "name": "Новое имя"})  # правка имени слой не теряет
    reopened = IdentityRegistry(tmp_path, StateStore(tmp_path / "emulator.sqlite3"))
    assert reopened.data["users"][0]["quasar_overrides"] == [{"path": SPOTTER, "source": "jarvis"}]
    reopened.mutate(user_overrides("u", []))
    assert "quasar_overrides" not in reopened.data["users"][0]
    reopened.mutate(user_overrides("u", [{"path": SPOTTER, "source": 1}]))
    reopened.mutate({"operation": "user_delete", "id": "u"})
    reopened.mutate({"operation": "user", "id": "u", "name": "Снова"})
    assert "quasar_overrides" not in reopened.data["users"][0]
