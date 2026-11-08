"""Quasar-конфиг: слои + инлайн-узлы ($select/$bool/$string/$number/$list/$param)."""
import json
from pathlib import Path

import pytest
from aiohttp import ClientSession

import server_emulator

from server_emulator.quasar_config import (PlatformStore, QuasarConfigError, compose, count_inline,
                                           count_keys, context_for, device_platform, generated_base,
                                           inline_type, merge_layer, merge_patch, resolve_param,
                                           resolve_source, resolve_tree, trace_layer)
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
    assert store.platform_for({"profile": {}}) is None  # без профиля/отчёта — не угадываем
    assert store.platform_for({"profile": {"platform": "chiron"}}) == "chiron"
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
                                  headers={"Host": "quasar.yandex.net"}) as response:
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
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert (await response.json())["subscription"]["enabled"] is False
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
    """Платформа базы: profile.platform → сообщённая колонкой (state.reported_platform)
    → пусто; platform_for первым файлом не угадывает."""
    store = PlatformStore(Path(tmpdir()) / "quasar")
    assert device_platform(None) == ""
    assert device_platform({"profile": {"platform": "chiron"}}) == "chiron"
    assert device_platform({"state": {"reported_platform": "yandexstation"}}) == "yandexstation"
    assert device_platform({"profile": {"platform": "chiron"},
                            "state": {"reported_platform": "yandexstation"}}) == "chiron"
    assert device_platform({}) == ""  # без профиля и без отчёта — общий дефолт
    store.save("yandexstation", {"a": 1})  # файл платформ есть
    assert store.platform_for({"state": {"reported_platform": "chiron"}}) is None  # не угадываем


def tmpdir():
    import tempfile
    return Path(tempfile.mkdtemp())


@pytest.mark.asyncio
async def test_reported_platform_applies_quirk_without_profile(tmp_path):
    """Колонка без profile.platform сообщает платформу в query: патч chiron применяется
    (hybrid_voice_inputs удалён, supported_frames — объект) в ответе колонке, generate
    и просмотрщике composed; значение запоминается в state."""
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-1', 'user_id': 'u1',
                               'device_ids': ['id-1']})  # профиль пуст — как у C00NX
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/get_sync_info?device_id=id-1&platform=chiron&version=3.367.41",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 200
                body = await response.json()
            hybrid = body["config"]["system_config"]["hybrid_request_factory"]
            frames = hybrid["hybrid"]["scenarios"]["fast_command_scenario"]["supported_frames"]
            assert isinstance(frames, dict) and frames  # квирк chiron применён
            assert "hybrid_voice_inputs" not in hybrid  # ключ общего дефолта удалён
            assert runtime.identities.resolve('id-1')["state"]["reported_platform"] == "chiron"
            async with client.post(url + "/emulator/api/quasar_source",
                                   json={"sn": "SN-1", "action": "generate"},
                                   headers={"X-Emulator": "1"}) as response:
                assert response.status == 200
                generated = await response.json()
            hybrid = generated["config"]["system_config"]["hybrid_request_factory"]
            assert "hybrid_voice_inputs" not in hybrid  # generate — по сообщённой платформе
            async with client.get(url + "/emulator/api/quasar_composed?device=SN-1",
                                  headers={"X-Emulator": "1"}) as response:
                composed = await response.json()
            hybrid = composed["config"]["config"]["system_config"]["hybrid_request_factory"]
            assert "hybrid_voice_inputs" not in hybrid  # просмотрщик — тоже
            assert composed["platform"] is None  # пользовательских патчей нет, угадывания нет
    finally:
        await runner.cleanup()
        await runtime.close()


def test_generated_base_platform_whitelist():
    """Платформа не попадает в путь файла: белый список platforms/ — traversal
    («../common», «chiron/../x») даёт общий дефолт (не исключение и не чужой файл),
    настоящий квирк на месте."""
    base = generated_base("yandexstation")
    for bad in ("../common", "chiron/../x", "..\\common"):
        assert generated_base(bad) == base
    frames = generated_base("chiron")["config"]["system_config"]["hybrid_request_factory"][
        "hybrid"]["scenarios"]["fast_command_scenario"]["supported_frames"]
    assert isinstance(frames, dict) and frames  # квирк chiron применён


def test_generated_base_under_common(tmp_path):
    """generated — нижний этаж слоёв: common перекрывает built-in дефолт (узлы generated
    целы под ним), платформа перекрывает common."""
    runtime = FakeRuntime(tmp_path, users=[{"id": "u1", "name": "U"}])
    device = dict(device_fixture(), profile={"platform": "yandexstation"})
    common = {"config": {"system_config": {"commandSpottersMode": "из-common"}}}
    base = merge_layer(generated_base("yandexstation"), common)
    body = compose(base, None, [], runtime, device)
    assert body["config"]["system_config"]["commandSpottersMode"] == "из-common"
    assert body["config"]["system_config"]["uniProxyUrl"].startswith("wss://")  # generated под common
    assert body["alice_pro_subscription"] == {"enabled": True, "ttl": 31536000}  # узел generated цел
    body = compose(base, {"config": {"system_config": {"commandSpottersMode": "из-платформы"}}},
                   [], runtime, device)
    assert body["config"]["system_config"]["commandSpottersMode"] == "из-платформы"


def test_generated_account_and_device_over_platform(tmp_path):
    """На базе generated: устройство сильнее аккаунта, аккаунт сильнее платформы,
    платформа сильнее дефолта."""
    account_overrides = [{"path": "config.account_config.spotter", "source": "аккаунт"}]
    runtime = FakeRuntime(tmp_path, users=[{"id": "u1", "name": "U",
                                            "quasar_overrides": account_overrides}])
    device = device_fixture()
    platform = {"config": {"account_config": {"spotter": "платформа"}}}
    body = compose(generated_base("chiron"), platform,
                   [{"path": "config.account_config.spotter", "source": "устройство"}],
                   runtime, device, account_overrides=account_overrides, account_name="U")
    assert body["config"]["account_config"]["spotter"] == "устройство"
    body = compose(generated_base("chiron"), platform, [], runtime, device,
                   account_overrides=account_overrides, account_name="U")
    assert body["config"]["account_config"]["spotter"] == "аккаунт"
    body = compose(generated_base("chiron"), platform, [], runtime, device)
    assert body["config"]["account_config"]["spotter"] == "платформа"
    body = compose(generated_base("chiron"), None, [], runtime, device)
    assert body["config"]["account_config"]["spotter"] == "alisa"  # дефолт generated


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
                assert isinstance(frames, dict) and frames  # объект: кадр → условия
                assert "hybrid_voice_inputs" not in hybrid
            hybrid = await hybrid_of(client, "SN-S")
            frames = hybrid["hybrid"]["scenarios"]["fast_command_scenario"]["supported_frames"]
            assert isinstance(frames, list) and frames
            assert hybrid["hybrid_voice_inputs"] is True
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_get_sync_info_common_and_platform_over_generated(tmp_path):
    """Швы на живом эндпоинте: common перекрывает generated, платформа — common."""
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-1', 'user_id': 'u1',
                               'device_ids': ['id-1'], 'profile': {'platform': 'yandexstation'}})
    runtime.quasar_platforms.save_common({"config": {"system_config":
                                                     {"commandSpottersMode": "из-common"}}})
    runner, url = await listen(create_app(runtime=runtime))

    async def mode_of(client):
        async with client.get(url + "/get_sync_info?device_id=id-1",
                              headers={"Host": "quasar.yandex.net"}) as response:
            assert response.status == 200
            body = await response.json()
            return body["config"]["system_config"]["commandSpottersMode"]

    try:
        async with ClientSession() as client:
            assert await mode_of(client) == "из-common"
            runtime.quasar_platforms.save("yandexstation",
                                          {"config": {"system_config":
                                                      {"commandSpottersMode": "из-платформы"}}})
            assert await mode_of(client) == "из-платформы"
            runtime.quasar_platforms.remove("yandexstation")
            assert await mode_of(client) == "из-common"
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_get_sync_info_custom_and_legacy_unchanged(tmp_path):
    """custom/legacy работают как раньше: свой ответ как есть, legacy-конфиг без compose
    (форвардинг «Напрямую» — прежний путь диспетчера, покрытие в test_device_record)."""
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
                security = body["glagol"]["security"]  # glagol.security поверх legacy тоже
                assert security["server_certificate"].startswith("-----BEGIN CERTIFICATE-----")
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_get_sync_info_stereo_group_over_template(tmp_path):
    """Стереопара реестра главнее узла group в собранном теле (в том числе на generated)."""
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    for sn in ("SN-1", "SN-2"):
        runtime.identities.mutate({'operation': 'device', 'sn': sn, 'user_id': 'u1',
                                   'device_ids': [sn], 'profile': {'platform': 'yandexstation'}})
    runtime.quasar_platforms.save_common({"group": {"name": "шаблон", "from": "common"}})
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
async def test_get_sync_info_generated_without_layers(tmp_path):
    """Чистое состояние (нет custom, слоёв и legacy): встроенный generated — 200, не 409."""
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
                assert config["system_config"]["uniProxyUrl"].startswith("wss://")
                assert "useVinsRequestProto" in config["system_config"]
                assert "commandSpottersMode" in config["system_config"]
                fast = config["system_config"]["hybrid_request_factory"]["hybrid"]["scenarios"]["fast_command_scenario"]
                assert isinstance(fast["supported_frames"], list)  # не chiron/magritte — массив
                assert "hybrid_voice_inputs" in config["system_config"]["hybrid_request_factory"]
                assert config["device_config"]["name"]  # {{device.name}} заполнен динамикой
                assert body["alice_pro_subscription"] == {"enabled": True, "ttl": 31536000}
                assert body["subscription"] == {"mode": "transaction"}
                assert body["tags"] == []
                security = body["glagol"]["security"]  # автоген поверх собранного конфига
                assert security["server_certificate"].startswith("-----BEGIN CERTIFICATE-----")
                assert is_rsa_key(security["server_private_key"])
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


def test_count_keys_and_inline():
    assert count_keys({"a": 1, "b": {"c": [1, 2]}, "d": None}) == 4
    assert count_keys({}) == 0 and count_keys([]) == 0 and count_keys("x") == 1
    body = {"tags": {"$name": "Теги", "$list": ["base"]},
            "sel": {"$select": "Вкл", "Вкл": {"$bool": True}, "$name": "S"},
            "plain": {"deep": 1}}
    assert count_inline(body) == 2  # sel — один узел: в варианты не углубляемся
    assert count_inline({"plain": [1, 2]}) == 0


def test_trace_layer_marks_added_and_changed_paths():
    trace = {}
    trace_layer(trace, {"a": 1, "b": {"x": 1, "y": 2}}, {"a": 2, "b": {"x": 1, "z": 3}, "c": 4},
                "", "общий конфиг")
    assert trace == {"a": "общий конфиг", "b.z": "общий конфиг", "c": "общий конфиг"}
    # словарь поверх отсутствующего — весь путь одним узлом, без метки внутри
    trace = {}
    trace_layer(trace, {"a": 1}, {"new": {"deep": {"x": 1}}}, "", "слой")
    assert trace == {"new": "слой"}


def test_compose_render_false_keeps_tokens_and_markers(tmp_path):
    runtime = FakeRuntime(tmp_path, users=[{"id": "u1", "name": "Аня"}])
    common = {"name": "{{device_name}}", "db": {"db": "user.name"}, "flag": {"$bool": True}}
    body = compose(common, {"extra": 1}, None, runtime, None, render=False)
    assert body == {"name": "{{device_name}}", "db": {"db": "user.name"}, "flag": True,
                    "extra": 1}  # инлайн-узел разрешён, токен и маркер остались как есть
    trace = {}
    body = compose({"sel": SELECTOR}, None, None, runtime, None, trace=trace, render=False)
    assert body["sel"] == SELECTOR["Включена"]  # $select разрешён по выбранному варианту
    assert body["sel"]["statusUrl"].endswith("{{device_id}}")  # токен внутри не тронут
    assert trace["sel"].startswith("шаблон")


@pytest.mark.asyncio
async def test_quasar_platforms_api_builtin_and_compose(tmp_path):
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/quasar_platforms") as response:
                body = await response.json()
                assert "chiron" in body["builtin_platforms"]
                assert body["common_keys"] == 0 and body["common_inline"] == 0  # скелет без значений
                assert body["builtin_counts"]["chiron"] > body["builtin_counts"][""]  # квирк добавляет ключи
            # встроенная база: квирк платформы; имя вне белого списка — общий дефолт
            async with client.get(url + "/emulator/api/quasar_platforms?builtin=chiron") as response:
                chiron = await response.json()
                fast = chiron["config"]["config"]["system_config"]["hybrid_request_factory"] \
                    ["hybrid"]["scenarios"]["fast_command_scenario"]
                assert isinstance(fast["supported_frames"], dict)
                assert chiron["keys"] > 0
            async with client.get(url + "/emulator/api/quasar_platforms?builtin=") as response:
                default = (await response.json())["keys"]
            async with client.get(url + "/emulator/api/quasar_platforms?builtin=unknown") as response:
                unknown = await response.json()
                assert unknown["keys"] == default
                fast = unknown["config"]["config"]["system_config"]["hybrid_request_factory"] \
                    ["hybrid"]["scenarios"]["fast_command_scenario"]
                assert isinstance(fast["supported_frames"], list)
            async with client.put(url + "/emulator/api/quasar_platforms",
                                  headers={"X-Emulator": "1"}, json={"common_file": COMMON}) as response:
                assert (await response.json())["saved"] == "common"
            async with client.put(url + "/emulator/api/quasar_platforms", headers={"X-Emulator": "1"},
                                  json={"platform_file": {"platform": "chiron",
                                                          "patch": {"config": {"device_config": {"platform": "chiron"}}}}}) as response:
                assert (await response.json())["saved"] == "chiron"
            async with client.get(url + "/emulator/api/quasar_platforms") as response:
                body = await response.json()
                assert body["common_keys"] == 16 and body["common_inline"] == 3  # tags/selector/флаг
                assert next(p for p in body["platforms"] if p["platform"] == "chiron")["keys"] == 1
            # итог слоёв без устройства: токены не подставлены, инлайн-узлы разрешены
            async with client.get(url + "/emulator/api/quasar_platforms?compose=chiron") as response:
                body = await response.json()
                assert body["platform"] == "chiron"
                config = body["config"]
                assert config["config"]["device_config"]["name"] == "{{device_name}}"
                assert config["config"]["device_config"]["platform"] == "chiron"
                assert config["tags"] == ["base"]
                assert config["alice_pro_subscription"] is True
                assert config["subscription"]["enabled"] is True
                assert config["subscription"]["statusUrl"].endswith("{{device_id}}")
                assert body["origins"]["config.device_config.platform"] == "платформа chiron"
                assert body["origins"]["config.device_config.name"] == "общий конфиг"
                assert body["origins"]["tags"] == "общий конфиг"
                assert "config.config.system_config" not in body["origins"]  # пути базы не помечаются
            # пустая платформа: итог база + common без патча
            async with client.get(url + "/emulator/api/quasar_platforms?compose=") as response:
                body = await response.json()
                assert body["platform"] == ""
                assert "device_config.platform" not in body["origins"]
                assert body["origins"]["tags"] == "общий конфиг"
    finally:
        await runner.cleanup()
        await runtime.close()
