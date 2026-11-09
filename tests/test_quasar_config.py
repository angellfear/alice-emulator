"""Quasar-конфиг: сборка шаблон → замены → динамика и ответ get_sync_info."""
import json
from pathlib import Path

import pytest
from aiohttp import ClientSession

import server_emulator

from server_emulator.quasar_config import (QuasarConfigError, context_for, count_keys, device_platform,
                                           generated_base, inline_type, merge_patch, resolve_param,
                                           resolve_source, resolve_tree)
from server_emulator.quasar_rules import RuleStore, compose_config
from server_emulator.runtime import Runtime

SELECTOR = {
    "$name": "Подписка", "$hint": "Состояние подписки",
    "$select": "Включена",
    "Включена": {"enabled": True, "mode": "subscription", "ttl": 0,
                 "statusUrl": "https://plus.yandex.ru/module-brick?deviceId={{device_id}}"},
    "Выключена (invalid_subscription_user)": {"enabled": False, "mode": "subscription", "ttl": 0,
                                              "disabled_reason": "invalid_subscription_user"},
}
OPTIONS = {k: v for k, v in SELECTOR.items() if not k.startswith("$")}
COMMON_RULES = [
    {"name": "Имя", "path": "/config/device_config/name", "kind": "template", "value": "{{device_name}}"},
    {"name": "Теги", "path": "/tags", "kind": "value", "value": ["base"]},
    {"name": "Подписка", "path": "/subscription", "kind": "select", "value": "Включена", "options": OPTIONS},
    {"name": "Alice Pro", "path": "/alice_pro_subscription", "kind": "bool", "value": True},
]
CHIRON_RULES = [{"name": "platform", "path": "/config/device_config/platform", "kind": "value", "value": "chiron"}]


class FakeRuntime:
    def __init__(self, tmp_path, users=None, devices=None, sessions=None):
        self.identities = type("I", (), {"data": {"users": users or [], "devices": devices or []}})()
        self.sessions = sessions or {}
        self.quasar_rules = RuleStore(tmp_path / "quasar")


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
    assert resolve_param("device.sn", runtime, device, context) == "SN-1"
    assert resolve_param("session.sn", runtime, device, context) == "SN-1"
    assert resolve_param("user.name", runtime, device, context) == "Аня"
    runtime2 = FakeRuntime(tmp_path, users=[{"id": "u1", "name": "Аня",
                                             "location": {"latitude": 55.7, "longitude": 37.6},
                                             "fields": {"wifi_hash": "abc123"}}])
    assert resolve_param("user.fields.wifi_hash", runtime2, device, context) == "abc123"
    assert resolve_param("user.location.latitude", runtime2, device, context) == 55.7
    with pytest.raises(QuasarConfigError, match="не найден"):
        resolve_param("future.thing", runtime, device, context)
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


def test_compose_rules_and_selector_switch(tmp_path):
    runtime = FakeRuntime(tmp_path, users=[{"id": "u1", "name": "U"}])
    runtime.quasar_rules.save("common", COMMON_RULES)
    runtime.quasar_rules.save("chiron", CHIRON_RULES + [
        {"name": "Теги", "path": "/tags", "kind": "value", "value": ["chiron-tag"]}])
    body = compose_config(runtime, "chiron", device_fixture(), device_rules=[
        {"path": "subscription", "source": {"$select": "Выключена (invalid_subscription_user)"}},  # прежний формат
        {"name": "Лампа", "path": "/extra/lamp", "kind": "value", "value": 42},
        {"name": "Pro", "path": "/alice_pro_subscription", "kind": "delete"},
    ])
    assert body["config"]["device_config"]["platform"] == "chiron"
    assert body["config"]["device_config"]["name"] == "Кухня"   # {{device_name}} подставлен
    assert body["subscription"]["enabled"] is False              # выбор варианта устройством
    assert body["subscription"]["disabled_reason"] == "invalid_subscription_user"
    assert body["tags"] == ["chiron-tag"]                         # платформа сильнее общих
    assert "alice_pro_subscription" not in body                   # удаление
    assert body["extra"]["lamp"] == 42
    assert body["config"]["system_config"]["uniProxyUrl"] == "ws://alica.lan/uni.ws"  # локальный адрес «Эмуляции»


def test_compose_disabled_rule_and_dotted_key(tmp_path):
    runtime = FakeRuntime(tmp_path)
    runtime.quasar_rules.save("common", [
        {"name": "off", "path": "/config/system_config/commandSpottersMode", "kind": "value",
         "value": "выкл", "enabled": False},
        {"name": "sdk", "path": "/config/system_config/com.yandex.io.sdk/flag", "kind": "bool", "value": True}])
    body = compose_config(runtime, "yandexstation", None, render=False)
    assert body["config"]["system_config"]["commandSpottersMode"] != "выкл"  # отключённая не применяется
    assert body["config"]["system_config"]["com.yandex.io.sdk"]["flag"] is True  # ключ с точкой


def test_compose_trace_origins(tmp_path):
    runtime = FakeRuntime(tmp_path, users=[{"id": "u1", "name": "U"}])
    runtime.quasar_rules.save("common", COMMON_RULES)
    runtime.quasar_rules.save("chiron", CHIRON_RULES + [
        {"name": "gone", "path": "/subscription", "kind": "delete"}])
    trace = {}
    compose_config(runtime, "chiron", device_fixture(), trace=trace,
                   device_rules=[{"name": "Лампа", "path": "/extra/lamp", "kind": "value", "value": 42}])
    assert trace["config.device_config.platform"] == "замены chiron: platform"
    assert trace["subscription"] == "замены chiron: gone (удалено)"
    assert trace["extra.lamp"] == "устройство: Лампа"
    assert trace["tags"] == "общие замены: Теги"
    assert trace["config.device_config.name"].startswith("динамика {{device_name}}")


def test_compose_account_and_device_order(tmp_path):
    """Устройство сильнее аккаунта, аккаунт сильнее платформы; аккаунт — только account_config."""
    account = [{"path": "config.account_config.spotter", "source": "аккаунт"}]
    runtime = FakeRuntime(tmp_path, users=[{"id": "u1", "name": "U", "quasar_overrides": account}])
    runtime.quasar_rules.save("chiron", [{"name": "s", "path": "/config/account_config/spotter",
                                          "kind": "value", "value": "платформа"}])
    device = device_fixture()
    rule = {"name": "s", "path": "/config/account_config/spotter", "kind": "value", "value": "устройство"}
    assert compose_config(runtime, "chiron", device, device_rules=[rule])["config"]["account_config"]["spotter"] == "устройство"
    assert compose_config(runtime, "chiron", device)["config"]["account_config"]["spotter"] == "аккаунт"
    runtime.identities.data["users"][0]["quasar_overrides"] = []
    assert compose_config(runtime, "chiron", device)["config"]["account_config"]["spotter"] == "платформа"
    runtime.quasar_rules.save("chiron", [])
    assert compose_config(runtime, "chiron", device)["config"]["account_config"]["spotter"] == "alisa"  # шаблон
    runtime.identities.data["users"][0]["quasar_overrides"] = [{"path": "config.device_config.x", "source": 1}]
    with pytest.raises(QuasarConfigError, match="account_config"):
        compose_config(runtime, "chiron", device)


def test_compose_render_false_keeps_params_and_markers(tmp_path):
    """Итог без устройства: параметр — {{ключ}} (не ошибка «не найден»), db/run — маркеры."""
    runtime = FakeRuntime(tmp_path)
    runtime.quasar_rules.save("common", [
        {"name": "hash", "path": "/config/account_config/user_wifi_config/wifi_hash", "kind": "param",
         "value": "user.wifi_hash"},
        {"name": "loc", "path": "/location", "kind": "db", "value": "user.location"},
        {"name": "Подписка", "path": "/subscription", "kind": "select", "value": "Включена", "options": OPTIONS}])
    trace = {}
    body = compose_config(runtime, "", None, trace=trace, render=False)
    assert body["config"]["account_config"]["user_wifi_config"]["wifi_hash"] == "{{user.wifi_hash}}"
    assert body["location"] == {"db": "user.location"}
    assert body["subscription"]["statusUrl"].endswith("{{device_id}}")  # токен внутри варианта не тронут
    assert trace["subscription"] == "общие замены: Подписка"
    with pytest.raises(QuasarConfigError, match="не найден"):  # с устройством параметр обязателен
        compose_config(runtime, "", device_fixture())


@pytest.mark.asyncio
async def test_get_sync_info_rules_end_to_end(tmp_path):
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-1', 'user_id': 'u1',
                               'device_ids': ['id-1'], 'profile': {'platform': 'chiron'}})
    runtime.quasar_rules.save("common", COMMON_RULES)
    runtime.quasar_rules.save("chiron", CHIRON_RULES)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/get_sync_info?device_id=id-1",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                body = await response.json()
                assert response.status == 200
                assert body["subscription"]["enabled"] is True
                assert body["subscription"]["statusUrl"].endswith("deviceId=id-1")
                assert body["alice_pro_subscription"] is True
                assert body["config"]["device_config"]["platform"] == "chiron"
            state = dict(runtime.identities.resolve('id-1')['state'])
            state['quasar_overrides'] = [{"name": "Подписка", "path": "/subscription", "kind": "select",
                                          "value": "Выключена (invalid_subscription_user)", "options": OPTIONS}]
            runtime.identities.mutate({'operation': 'state', 'sn': 'SN-1', 'state': state})
            async with client.get(url + "/get_sync_info?device_id=id-1",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert (await response.json())["subscription"]["enabled"] is False
            runtime.quasar_rules.save("common", [{"name": "bad", "path": "/x", "kind": "param",
                                                  "value": "no.such.param"}])
            async with client.get(url + "/get_sync_info?device_id=id-1",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 409  # сборка не удалась — понятная ошибка, не 500
                assert (await response.json())["error"] == "quasar_config_source"
    finally:
        await runner.cleanup()
        await runtime.close()


def test_quasar_defaults_package():
    """Встроенные дефолты: 13 платформенных файлов, квирк chiron/magritte доводит
    общий дефолт до платформенной формы, README несёт происхождение."""
    defaults = Path(server_emulator.__file__).parent / "quasar_defaults"
    platforms = sorted(p.stem for p in (defaults / "platforms").glob("*.json"))
    assert platforms == sorted(["chiron", "cucumber", "magritte", "malevich", "monet",
                                "yandexmicro", "yandexmidi", "yandexmini", "yandexmini_2",
                                "yandexmodule", "yandexmodule_2", "yandexstation",
                                "yandexstation_2"])
    common = json.loads((defaults / "common.json").read_text(encoding="utf-8"))
    assert common["config"]["device_config"]["name"] == "{{device.name}}"
    assert common["alice_pro_subscription"] == {"enabled": True, "ttl": 31536000}
    assert common["subscription"] == {"mode": "transaction"} and common["tags"] == []
    chiron = json.loads((defaults / "platforms" / "chiron.json").read_text(encoding="utf-8"))
    magritte = json.loads((defaults / "platforms" / "magritte.json").read_text(encoding="utf-8"))
    assert chiron == magritte
    hybrid = chiron["config"]["system_config"]["hybrid_request_factory"]
    assert hybrid["hybrid_voice_inputs"] is None  # null = удалить узел
    frames = hybrid["hybrid"]["scenarios"]["fast_command_scenario"]["supported_frames"]
    assert isinstance(frames, dict)
    merged = merge_patch(common, chiron)
    hybrid_merged = merged["config"]["system_config"]["hybrid_request_factory"]
    merged_frames = hybrid_merged["hybrid"]["scenarios"]["fast_command_scenario"]["supported_frames"]
    assert isinstance(merged_frames, dict) and "hybrid_voice_inputs" not in hybrid_merged
    assert "fefcd58803012c61f8a9f8c88351dabff1305c81" in \
        (defaults / "README.md").read_text(encoding="utf-8")


def test_device_platform_resolution_order():
    """Платформа шаблона: profile.platform → сообщённая колонкой (state.reported_platform) → пусто."""
    assert device_platform(None) == ""
    assert device_platform({"profile": {"platform": "chiron"}}) == "chiron"
    assert device_platform({"state": {"reported_platform": "yandexstation"}}) == "yandexstation"
    assert device_platform({"profile": {"platform": "chiron"},
                            "state": {"reported_platform": "yandexstation"}}) == "chiron"
    assert device_platform({}) == ""  # без профиля и без отчёта — общий дефолт


@pytest.mark.asyncio
async def test_reported_platform_applies_quirk_without_profile(tmp_path):
    """Колонка без profile.platform сообщает платформу в query: шаблон chiron (без
    hybrid_voice_inputs, supported_frames — объект) в ответе колонке, generate и
    просмотрщике composed; значение запоминается в state."""
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-1', 'user_id': 'u1',
                               'device_ids': ['id-1']})  # профиль пуст
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/get_sync_info?device_id=id-1&platform=chiron&version=3.367.41",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 200
                body = await response.json()
            hybrid = body["config"]["system_config"]["hybrid_request_factory"]
            frames = hybrid["hybrid"]["scenarios"]["fast_command_scenario"]["supported_frames"]
            assert isinstance(frames, dict) and frames
            assert "hybrid_voice_inputs" not in hybrid
            assert runtime.identities.resolve('id-1')["state"]["reported_platform"] == "chiron"
            async with client.post(url + "/emulator/api/quasar_source",
                                   json={"sn": "SN-1", "action": "generate"},
                                   headers={"X-Emulator": "1"}) as response:
                assert response.status == 200
                generated = await response.json()
            assert "hybrid_voice_inputs" not in generated["config"]["system_config"]["hybrid_request_factory"]
            async with client.get(url + "/emulator/api/quasar_composed?device=SN-1",
                                  headers={"X-Emulator": "1"}) as response:
                composed = await response.json()
            hybrid = composed["config"]["config"]["system_config"]["hybrid_request_factory"]
            assert "hybrid_voice_inputs" not in hybrid
            assert composed["platform"] == "chiron"
    finally:
        await runner.cleanup()
        await runtime.close()


def test_generated_base_platform_whitelist():
    """Платформа не попадает в путь файла: traversal даёт общий дефолт, квирк на месте."""
    base = generated_base("yandexstation")
    for bad in ("../common", "chiron/../x", "..\\common"):
        assert generated_base(bad) == base
    frames = generated_base("chiron")["config"]["system_config"]["hybrid_request_factory"][
        "hybrid"]["scenarios"]["fast_command_scenario"]["supported_frames"]
    assert isinstance(frames, dict) and frames


@pytest.mark.asyncio
async def test_get_sync_info_platform_quirk(tmp_path):
    """Квирк платформ из встроенных дефолтов: chiron/magritte — supported_frames-объект
    без hybrid_voice_inputs; остальные платформы — массив с hybrid_voice_inputs."""
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    for sn, platform in (('SN-C', 'chiron'), ('SN-M', 'magritte'), ('SN-S', 'yandexstation')):
        runtime.identities.mutate({'operation': 'device', 'sn': sn, 'user_id': 'u1',
                                   'device_ids': [sn], 'profile': {'platform': platform}})
    runner, url = await listen(create_app(runtime=runtime))

    async def hybrid_of(client, sn):
        async with client.get(url + f"/get_sync_info?device_id={sn}",
                              headers={"Host": "quasar.yandex.net"}) as response:
            assert response.status == 200
            body = await response.json()
            return body["config"]["system_config"]["hybrid_request_factory"]

    try:
        async with ClientSession() as client:
            for sn in ("SN-C", "SN-M"):
                hybrid = await hybrid_of(client, sn)
                frames = hybrid["hybrid"]["scenarios"]["fast_command_scenario"]["supported_frames"]
                assert isinstance(frames, dict) and frames
                assert "hybrid_voice_inputs" not in hybrid
            hybrid = await hybrid_of(client, "SN-S")
            frames = hybrid["hybrid"]["scenarios"]["fast_command_scenario"]["supported_frames"]
            assert isinstance(frames, list) and frames
            assert hybrid["hybrid_voice_inputs"] is True
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_get_sync_info_common_and_platform_rules(tmp_path):
    """На живом эндпоинте: общие замены перекрывают шаблон, замены платформы — общие."""
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-1', 'user_id': 'u1',
                               'device_ids': ['id-1'], 'profile': {'platform': 'yandexstation'}})
    rule = {"name": "mode", "path": "/config/system_config/commandSpottersMode", "kind": "value"}
    runtime.quasar_rules.save("common", [{**rule, "value": "из-общих"}])
    runner, url = await listen(create_app(runtime=runtime))

    async def mode_of(client):
        async with client.get(url + "/get_sync_info?device_id=id-1",
                              headers={"Host": "quasar.yandex.net"}) as response:
            assert response.status == 200
            body = await response.json()
            return body["config"]["system_config"]["commandSpottersMode"]

    try:
        async with ClientSession() as client:
            assert await mode_of(client) == "из-общих"
            runtime.quasar_rules.save("yandexstation", [{**rule, "value": "из-платформы"}])
            assert await mode_of(client) == "из-платформы"
            runtime.quasar_rules.remove_platform("yandexstation")
            assert await mode_of(client) == "из-общих"
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_get_sync_info_custom_and_legacy_unchanged(tmp_path):
    """custom/legacy: свой ответ как есть, legacy-конфиг state.quasar — пока нет ни одной замены."""
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    for sn in ("SN-1", "SN-2"):
        runtime.identities.mutate({'operation': 'device', 'sn': sn, 'user_id': 'u1',
                                   'device_ids': [sn], 'profile': {'platform': 'yandexstation'}})
    runtime.identities.mutate({'operation': 'state', 'sn': 'SN-1', 'state': {
        'quasar_source': 'custom',
        'quasar_custom': {'status': 'ok', 'config': {'a': 1}, 'tags': ['t']}}})
    runtime.identities.mutate({'operation': 'state', 'sn': 'SN-2', 'state': {
        'quasar': {'config': {'device_config': {'name': 'legacy'}}, 'tags': ['x'],
                   'subscription': {'mode': 'legacy'}}}})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/get_sync_info?device_id=SN-1",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 200
                assert await response.json() == {'status': 'ok', 'config': {'a': 1}, 'tags': ['t']}
            async with client.get(url + "/get_sync_info?device_id=SN-2",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 200
                body = await response.json()
                assert body["config"] == {'device_config': {'name': 'legacy'}}
                assert body["tags"] == ['x'] and body["subscription"] == {'mode': 'legacy'}
                security = body["glagol"]["security"]
                assert security["server_certificate"].startswith("-----BEGIN CERTIFICATE-----")
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_get_sync_info_stereo_group_over_rules(tmp_path):
    """Стереопара реестра главнее узла group из замен."""
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    for sn in ("SN-1", "SN-2"):
        runtime.identities.mutate({'operation': 'device', 'sn': sn, 'user_id': 'u1',
                                   'device_ids': [sn], 'profile': {'platform': 'yandexstation'}})
    runtime.quasar_rules.save("common", [{"name": "group", "path": "/group", "kind": "value",
                                          "value": {"name": "шаблон", "from": "common"}}])
    runtime.identities.mutate({'operation': 'group', 'name': 'Пара', 'secret': 'sec',
                               'devices': [{"sn": "SN-1", "role": "leader"},
                                           {"sn": "SN-2", "role": "follower"}]})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/get_sync_info?device_id=SN-1",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 200
                body = await response.json()
                assert body["group"]["name"] == "Пара" and body["group"]["secret"] == "sec"
                assert [m["role"] for m in body["group"]["devices"]] == ["leader", "follower"]
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_get_sync_info_generated_without_rules(tmp_path):
    """Чистое состояние (нет custom, замен и legacy): встроенный шаблон — 200, не 409."""
    from server_emulator.server import create_app
    from tests.conftest import is_rsa_key, listen

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-1', 'user_id': 'u1', 'device_ids': ['id-1']})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/get_sync_info?device_id=id-1",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 200
                body = await response.json()
                assert body["status"] == "ok"
                config = body["config"]
                assert config["system_config"]["uniProxyUrl"] == "ws://alica.lan/uni.ws"
                assert "useVinsRequestProto" in config["system_config"]
                assert "commandSpottersMode" in config["system_config"]
                fast = config["system_config"]["hybrid_request_factory"]["hybrid"]["scenarios"]["fast_command_scenario"]
                assert isinstance(fast["supported_frames"], list)
                assert "hybrid_voice_inputs" in config["system_config"]["hybrid_request_factory"]
                assert config["device_config"]["name"]  # {{device.name}} заполнен динамикой
                assert body["alice_pro_subscription"] == {"enabled": True, "ttl": 31536000}
                assert body["subscription"] == {"mode": "transaction"}
                assert body["tags"] == []
                security = body["glagol"]["security"]
                assert security["server_certificate"].startswith("-----BEGIN CERTIFICATE-----")
                assert is_rsa_key(security["server_private_key"])
    finally:
        await runner.cleanup()
        await runtime.close()


def test_count_keys():
    assert count_keys({"a": 1, "b": {"c": [1, 2]}, "d": None}) == 4
    assert count_keys({}) == 0 and count_keys([]) == 0 and count_keys("x") == 1
