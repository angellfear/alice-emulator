"""Замены аккаунта (quasar_overrides пользователя) между платформой и устройством."""
import pytest
from aiohttp import ClientSession

from server_emulator.identity import IdentityRegistry
from server_emulator.quasar_config import QuasarConfigError
from server_emulator.quasar_rules import RuleStore, account_rules, compose_config, validate_account_rules
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from server_emulator.state_store import StateStore
from tests.conftest import listen

HEADERS = {"Host": "quasar.yandex.net"}
COMMON = [{"name": "spotter", "path": "/config/account_config/spotter", "kind": "select",
           "value": "Значение", "options": {"Значение": "alisa"}},
          {"name": "jingle", "path": "/config/account_config/jingle", "kind": "select",
           "value": "Вкл", "options": {"Вкл": True, "Выкл": False}}]
SPOTTER = "/config/account_config/spotter"


def spotter_rule(value):
    return {"name": "spotter", "path": SPOTTER, "kind": "select",
            "value": "Значение", "options": {"Значение": value}}


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
    runtime.quasar_rules.save("common", COMMON)
    runtime.quasar_rules.save("chiron", [{"name": "platform", "path": "/config/device_config/platform",
                                          "kind": "select", "value": "Значение",
                                          "options": {"Значение": "chiron"}}])
    return runtime


async def sync(client, url, alias):
    async with client.get(url + "/get_sync_info?device_id=" + alias, headers=HEADERS) as response:
        return response.status, await response.json()


@pytest.mark.asyncio
async def test_user_override_reaches_all_user_devices(tmp_path):
    runtime = await make_runtime(tmp_path)
    runtime.identities.mutate(user_overrides("alice", [spotter_rule("jarvis")]))
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
    runtime.identities.mutate(user_overrides("alice", [spotter_rule("jarvis")]))
    state = dict(runtime.identities.resolve("id-2")["state"])
    state["quasar_overrides"] = [spotter_rule("computer")]
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
        validate_account_rules([{"name": "x", "path": "/config/device_config/name", "kind": "select",
                                 "value": "Значение", "options": {"Значение": "x"}}])
    with pytest.raises(QuasarConfigError):
        validate_account_rules([{"name": "x", "path": "/config/account_config", "kind": "delete"}])
    with pytest.raises(QuasarConfigError):
        validate_account_rules([{"name": "x", "path": "/subscription", "kind": "delete"}])
    identities = IdentityRegistry(tmp_path, StateStore(tmp_path / "emulator.sqlite3"))
    identities.mutate({"operation": "user", "id": "u", "name": "U"})
    with pytest.raises(ValueError, match="вне config.account_config"):
        identities.mutate(user_overrides("u", [{"name": "x", "path": "/config/system_config/x",
                                                 "kind": "select", "value": "Значение",
                                                 "options": {"Значение": 1}}]))
    with pytest.raises(ValueError, match="не найден"):
        identities.mutate(user_overrides("zzz", []))
    # сборка сама тоже не пропускает путь вне account_config (в обход реестра)
    runtime = type("R", (), {"identities": identities, "sessions": {},
                             "quasar_rules": RuleStore(tmp_path / "quasar")})()
    identities.data["users"][0]["quasar_overrides"] = [
        {"name": "x", "path": "/config/device_config/name", "kind": "select",
         "value": "Значение", "options": {"Значение": "x"}}]
    with pytest.raises(QuasarConfigError, match="account_config"):
        compose_config(runtime, "", {"sn": "SN", "device_ids": ["SN"], "user_id": "u"})


@pytest.mark.asyncio
async def test_unassigned_device_has_no_account_layer(tmp_path):
    runtime = await make_runtime(tmp_path)
    runtime.identities.mutate(user_overrides("alice", [spotter_rule("jarvis")]))
    unbound = runtime.identities.resolve("id-u")
    assert account_rules(runtime, unbound) == ([], None)
    rules = account_rules(runtime, runtime.identities.resolve("id-1"))[0]
    assert [{k: v for k, v in r.items() if k != "id"} for r in rules] == [
        {**spotter_rule("jarvis"), "enabled": True}]
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
        spotter_rule("jarvis"),
        {"name": "jingle", "path": "/config/account_config/jingle", "kind": "delete"}]))
    state = dict(runtime.identities.resolve("id-2")["state"])
    state["quasar_overrides"] = [spotter_rule("computer")]
    runtime.identities.mutate({"operation": "state", "sn": "SN-2", "state": state})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/quasar_composed?device=SN-1") as response:
                body = await response.json()
            origins = body["origins"]
            assert origins[SPOTTER] == "аккаунт Alice: spotter"
            assert origins["/config/account_config/jingle"].startswith("аккаунт Alice")
            assert "jingle" not in body["config"]["config"]["account_config"]
            async with client.get(url + "/emulator/api/quasar_composed?device=SN-2") as response:
                body = await response.json()
            assert body["config"]["config"]["account_config"]["spotter"] == "computer"
            assert body["origins"][SPOTTER] == "устройство: spotter"
    finally:
        await runner.cleanup()
        await runtime.close()


def test_storage_survives_reload_rename_and_delete(tmp_path):
    identities = IdentityRegistry(tmp_path, StateStore(tmp_path / "emulator.sqlite3"))
    identities.mutate({"operation": "user", "id": "u", "name": "U"})
    data = identities.mutate(user_overrides("u", [spotter_rule("jarvis")]))
    strip = lambda items: [{k: v for k, v in r.items() if k != "id"} for r in items]
    assert strip(data["users"][0]["quasar_overrides"]) == [{**spotter_rule("jarvis"), "enabled": True}]
    identities.mutate({"operation": "user", "id": "u", "name": "Новое имя"})  # правка имени слой не теряет
    reopened = IdentityRegistry(tmp_path, StateStore(tmp_path / "emulator.sqlite3"))
    assert strip(reopened.data["users"][0]["quasar_overrides"]) == [{**spotter_rule("jarvis"), "enabled": True}]
    reopened.mutate(user_overrides("u", []))
    assert "quasar_overrides" not in reopened.data["users"][0]
    reopened.mutate(user_overrides("u", [{"name": "x", "path": "/config/account_config/x", "kind": "select",
                                          "value": "Значение", "options": {"Значение": 1}}]))
    reopened.mutate({"operation": "user_delete", "id": "u"})
    reopened.mutate({"operation": "user", "id": "u", "name": "Снова"})
    assert "quasar_overrides" not in reopened.data["users"][0]
