"""Quasar-конфиг: слои + инлайн-узлы ($select/$bool/$string/$number/$list/$param)."""
import json

import pytest
from aiohttp import ClientSession

from server_emulator.quasar_config import (PlatformStore, QuasarConfigError, compose,
                                           context_for, inline_type,
                                           merge_patch, resolve_param, resolve_source,
                                           resolve_tree)
from server_emulator.runtime import Runtime

SELECTOR = {
    "$name": "Подписка", "$hint": "Состояние подписки",
    "$select": "Включена",
    "Включена": {"enabled": True, "mode": "subscription", "ttl": 0,
                 "statusUrl": "https://plus.yandex.ru/module-brick?deviceId={{device_id}}"},
    "Выключена (invalid_subscription_user)": {"enabled": False, "mode": "subscription", "ttl": 0,
                                              "disabled_reason": "invalid_subscription_user"},
}
COMMON = {"config": {"device_config": {"name": "{{device_name}}"}},
          "tags": {"$name": "Теги", "$list": ["base"]},
          "subscription": SELECTOR,
          "alice_pro_subscription": {"$name": "Alice Pro", "$bool": True}}


class FakeRuntime:
    def __init__(self, tmp_path, users=None, devices=None, sessions=None):
        self.identities = type("I", (), {"data": {"users": users or [], "devices": devices or []}})()
        self.sessions = sessions or {}
        self.quasar_platforms = PlatformStore(tmp_path / "quasar")


def device_fixture():
    return {"sn": "SN-1", "device_ids": ["id-1"], "name": "Кухня", "user_id": "u1",
            "profile": {"platform": "chiron"}, "room": "Кухня", "fields": {}}


def test_merge_patch_rfc7396():
    assert merge_patch({"a": {"b": 1, "c": 2}}, {"a": {"b": 3, "c": None}}) == {"a": {"b": 3}}
    assert merge_patch({"a": 1}, {"a": {"deep": True}}) == {"a": {"deep": True}}
    assert merge_patch(None, {"x": 1}) == {"x": 1}


def test_inline_node_types_and_validation():
    assert inline_type(SELECTOR) == "$select"
    assert inline_type({"$bool": True}) == "$bool"
    assert inline_type({"просто": "данные"}) is None
    with pytest.raises(QuasarConfigError, match="один \\$-ключ"):
        inline_type({"$bool": True, "$select": "x"})
    with pytest.raises(QuasarConfigError, match="лишние"):
        inline_type({"$bool": True, "чужой": 1})  # не селектор: рядом только $name/$hint
    assert inline_type({"$select": "x", "Вариант": 1, "$name": "Имя"}) == "$select"  # варианты рядом ок


def test_inline_resolution_selector_bool_list_param(tmp_path):
    runtime = FakeRuntime(tmp_path, users=[{"id": "u1", "name": "Аня"}],
                          sessions={"c1": {"device_sn": "SN-1"}})
    device = device_fixture()
    context = context_for(runtime, device)
    body = resolve_tree({"subscription": SELECTOR}, runtime, device, context)
    assert body["subscription"]["enabled"] is True
    assert body["subscription"]["statusUrl"].endswith("deviceId=id-1")  # {{device_id}} в варианте
    assert resolve_tree({"$bool": False}, runtime, device, context) is False
    assert resolve_tree({"$string": "Владелец: {{user.name}}"}, runtime, device, context) == "Владелец: Аня"
    assert resolve_tree({"$list": ["a"]}, runtime, device, context) == ["a"]
    # параметры-заглушки
    assert resolve_param("device.sn", runtime, device, context) == "SN-1"
    assert resolve_param("session.sn", runtime, device, context) == "SN-1"
    assert resolve_param("user.name", runtime, device, context) == "Аня"
    # пути реестра через $param: поля и локация пользователя
    runtime2 = FakeRuntime(tmp_path, users=[{"id": "u1", "name": "Аня",
                                             "location": {"latitude": 55.7, "longitude": 37.6},
                                             "fields": {"wifi_hash": "abc123"}}])
    assert resolve_param("user.fields.wifi_hash", runtime2, device, context) == "abc123"
    assert resolve_param("user.location.latitude", runtime2, device, context) == 55.7
    with pytest.raises(QuasarConfigError, match="не найден"):
        resolve_param("future.thing", runtime, device, context)
    # вложенный инлайн: вариант содержит $param
    nested = {"$select": "on", "on": {"owner": {"$param": "user.name"}}}
    assert resolve_tree(nested, runtime, device, context) == {"owner": "Аня"}
    with pytest.raises(QuasarConfigError, match="вариант"):
        resolve_tree({"$select": "нетТакого", "есть": {}}, runtime, device, context)


def test_run_whitelist_json_and_env(tmp_path):
    runtime = FakeRuntime(tmp_path)
    scripts = tmp_path / "quasar" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "hello.bat").write_text("@echo {\"ok\": true}\n", encoding="utf-8")
    (scripts / "name.bat").write_text("@echo %QDEVICE_ID%", encoding="utf-8")
    outside = tmp_path / "outside.bat"
    outside.write_text("@echo bad", encoding="utf-8")
    device = device_fixture()
    context = context_for(runtime, device)
    assert resolve_source({"run": "hello.bat"}, runtime, device, context) == {"ok": True}
    assert resolve_source({"run": "name.bat"}, runtime, device, context) == "id-1"
    with pytest.raises(QuasarConfigError, match="вне белого списка"):
        resolve_source({"run": str(outside)}, runtime, device, context)


def test_store_pure_files_and_default(tmp_path):
    store = PlatformStore(tmp_path / "quasar")
    assert store.load_common() is None and store.resolve_files(device_fixture()) == (None, None)
    store.ensure_common()
    assert isinstance(store.load_common(), dict)
    with pytest.raises(QuasarConfigError):
        store.remove("common")
    store.save("chiron", {"config": {"device_config": {"platform": "chiron"}}})
    store.save("yandexstation_2", {"config": {"device_config": {"platform": "yandexstation_2"}}})
    assert [m["platform"] for m in store.list()] == ["chiron", "yandexstation_2"]
    assert store.list()[0]["default"] is True  # первая по алфавиту
    assert store.platform_for({"profile": {}}) == "chiron"
    with pytest.raises(QuasarConfigError):
        store.save("common", {})
    with pytest.raises(QuasarConfigError):
        store.save("bad", None)


def test_compose_layers_and_selector_switch(tmp_path):
    runtime = FakeRuntime(tmp_path, users=[{"id": "u1", "name": "U"}])
    device = device_fixture()
    platform = {"config": {"device_config": {"platform": "chiron"}},
                "tags": {"$list": ["chiron-tag"]}}
    body = compose(COMMON, platform, [
        {"path": "subscription", "source": {"$select": "Выключена (invalid_subscription_user)"}},
        {"path": "extra.lamp", "source": 42},
        {"path": "alice_pro_subscription", "source": {"delete": True}},
    ], runtime, device)
    assert body["config"]["device_config"]["platform"] == "chiron"
    assert body["config"]["device_config"]["name"] == "Кухня"
    assert body["subscription"]["enabled"] is False            # переключение селектора
    assert body["subscription"]["disabled_reason"] == "invalid_subscription_user"
    assert body["tags"] == ["chiron-tag"]                       # платформа сменила список
    assert "alice_pro_subscription" not in body                 # delete-override
    assert body["extra"]["lamp"] == 42
    assert COMMON["subscription"]["$select"] == "Включена"      # исходник не изменён


def test_compose_trace_origins(tmp_path):
    runtime = FakeRuntime(tmp_path, users=[{"id": "u1", "name": "U"}])
    device = device_fixture()
    platform = {"config": {"device_config": {"platform": "chiron"}, "extra": {"deep": 1}},
                "gone": None}
    trace = {}
    body = compose(COMMON, platform, [
        {"path": "subscription", "source": {"$select": "Выключена (invalid_subscription_user)"}},
        {"path": "extra.lamp", "source": 42},
    ], runtime, device, trace=trace, platform_label="chiron")
    assert body["subscription"]["enabled"] is False
    assert trace["config.device_config.platform"] == "платформа chiron"
    assert trace["config.extra.deep"] == "платформа chiron"
    assert trace["gone"] == "платформа chiron (удалено)"
    assert trace["subscription"] == "переопределение: выбор «Выключена (invalid_subscription_user)»"
    assert trace["extra.lamp"] == "переопределение устройства"
    assert trace["tags"] == "шаблон «Теги»"                    # платформа перезаписала узел $list
    assert "config.device_config.name" in trace                # {{device_name}} из общего
    assert trace["config.device_config.name"].startswith("динамика {{device_name}}")


@pytest.mark.asyncio
async def test_get_sync_info_inline_end_to_end(tmp_path):
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-1', 'user_id': 'u1',
                               'device_ids': ['id-1'], 'profile': {'platform': 'chiron'}})
    runtime.quasar_platforms.save_common(COMMON)
    runtime.quasar_platforms.save("chiron", {"config": {"device_config": {"platform": "chiron"}}})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/get_sync_info?device_id=id-1",
                                  headers={"Host": "quasar.local"}) as response:
                body = await response.json()
                assert response.status == 200
                assert body["subscription"]["enabled"] is True
                assert body["subscription"]["statusUrl"].endswith("deviceId=id-1")
                assert body["alice_pro_subscription"] is True
            device = runtime.identities.resolve('id-1')
            state = dict(device['state'])
            state['quasar_overrides'] = [{"path": "subscription",
                                          "source": {"$select": "Выключена (invalid_subscription_user)"}}]
            runtime.identities.mutate({'operation': 'state', 'sn': 'SN-1', 'state': state})
            async with client.get(url + "/get_sync_info?device_id=id-1",
                                  headers={"Host": "quasar.local"}) as response:
                assert (await response.json())["subscription"]["enabled"] is False
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_get_sync_info_legacy_without_layers(tmp_path):
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-1', 'user_id': 'u1', 'device_ids': ['id-1']})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/get_sync_info?device_id=id-1",
                                  headers={"Host": "quasar.local"}) as response:
                assert response.status == 409
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_quasar_platforms_api_overview(tmp_path):
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/quasar_platforms") as response:
                body = await response.json()
                assert isinstance(body["common"], dict) and "$select" in body["inline"]
            async with client.put(url + "/emulator/api/quasar_platforms",
                                  headers={"X-Emulator": "1"}, json={"common_file": COMMON}) as response:
                assert (await response.json())["saved"] == "common"
            async with client.put(url + "/emulator/api/quasar_platforms", headers={"X-Emulator": "1"},
                                  json={"platform_file": {"platform": "chiron", "patch": {"a": 1}}}) as response:
                assert (await response.json())["saved"] == "chiron"
            async with client.delete(url + "/emulator/api/quasar_platforms?platform=common",
                                     headers={"X-Emulator": "1"}) as response:
                assert response.status >= 400
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_quasar_composed_api(tmp_path):
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-1', 'user_id': 'u1',
                               'device_ids': ['id-1'], 'profile': {'platform': 'chiron'}})
    runtime.quasar_platforms.save_common(COMMON)
    runtime.quasar_platforms.save("chiron", {"config": {"device_config": {"platform": "chiron"}}})
    state = runtime.identities.resolve('id-1')['state']
    state['quasar_overrides'] = [{"path": "extra.lamp", "source": 42}]
    runtime.identities.mutate({'operation': 'state', 'sn': 'SN-1', 'state': state})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/quasar_composed?device=SN-1") as response:
                body = await response.json()
                assert response.status == 200
                assert body["platform"] == "chiron" and body["legacy"] is False
                assert body["config"]["extra"]["lamp"] == 42
                assert body["origins"]["config.device_config.platform"] == "платформа chiron"
                assert body["origins"]["extra.lamp"] == "переопределение устройства"
                assert "динамика" in body["origins"]["config.device_config.name"]
                assert body["origins"]["subscription"].startswith("шаблон")
            async with client.get(url + "/emulator/api/quasar_composed?device=NOPE") as response:
                assert response.status == 400
    finally:
        await runner.cleanup()
        await runtime.close()
