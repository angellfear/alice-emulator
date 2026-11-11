"""Quasar-конфиг: сборка шаблон → замены → динамика и ответ get_sync_info."""
import json
from pathlib import Path

import pytest
from aiohttp import ClientSession

import server_emulator

from server_emulator.quasar_config import (QuasarConfigError, context_for, count_keys, device_platform,
                                           generated_base, merge_patch, resolve_source)
from server_emulator.quasar_rules import RuleStore, compose_config
from server_emulator.runtime import Runtime

SINGLE = "Значение"
SUBSCRIPTION = {
    "Включена": {"enabled": True, "mode": "subscription", "ttl": 0,
                 "statusUrl": "https://plus.yandex.ru/module-brick?deviceId={{device_id}}"},
    "Выключена (invalid_subscription_user)": {"enabled": False, "mode": "subscription", "ttl": 0,
                                              "disabled_reason": "invalid_subscription_user"},
}
COMMON_RULES = [
    {"name": "Имя", "path": "/config/device_config/name", "kind": "select",
     "value": "Значение", "options": {"Значение": "{{device_name}}"}},
    {"name": "Теги", "path": "/tags", "kind": "select", "value": "Значение", "options": {"Значение": ["base"]}},
    {"name": "Подписка", "path": "/subscription", "kind": "select", "value": "Включена", "options": SUBSCRIPTION},
    {"name": "Alice Pro", "path": "/alice_pro_subscription", "kind": "select",
     "value": "Вкл", "options": {"Вкл": True, "Выкл": False}},
]
CHIRON_RULES = [{"name": "platform", "path": "/config/device_config/platform", "kind": "select",
                 "value": "Значение", "options": {"Значение": "chiron"}}]


class FakeParams:
    """Минимальный стор «Параметров»: определения + значения по уровням цепочки."""

    def __init__(self, defs=(), values=None):
        self._defs = list(defs)
        self._values = values or {}  # {(owner, key): value}

    def defs(self):
        return [{"key": key} for key in self._defs]

    def def_get(self, key):
        return {"key": key} if key in self._defs else None

    def resolve(self, key, user_id=None, sn=None, platform=None):
        for owner in (f"device:{sn}" if sn else None, f"user:{user_id}" if user_id else None,
                      f"platform:{platform}" if platform else None, "server"):
            if owner and (owner, key) in self._values:
                return self._values[(owner, key)]
        raise KeyError(key)


class FakeRuntime:
    def __init__(self, tmp_path, users=None, devices=None, sessions=None, params=None,
                 shipped_dir=None):
        self.identities = type("I", (), {"data": {"users": users or [], "devices": devices or []}})()
        self.sessions = sessions or {}
        self.quasar_rules = RuleStore(tmp_path / "quasar", shipped_dir=shipped_dir)
        self.quasar_params = params
        self.directory = tmp_path  # server.json нет — локальный домен по умолчанию


def device_fixture():
    return {"sn": "SN-1", "device_ids": ["id-1"], "name": "Кухня", "user_id": "u1",
            "profile": {"platform": "chiron"}, "room": "Кухня", "fields": {}}


def test_merge_patch_rfc7396():
    assert merge_patch({"a": {"b": 1, "c": 2}}, {"a": {"b": 3, "c": None}}) == {"a": {"b": 3}}
    assert merge_patch({"a": 1}, {"a": {"deep": True}}) == {"a": {"deep": True}}
    assert merge_patch(None, {"x": 1}) == {"x": 1}


def test_context_nested_roots_and_params(tmp_path):
    """Корни подстановок: время, реестр (device/user — вложенные), каталог «Параметры»
    (занятые реестром пути не перезаписываются)."""
    runtime = FakeRuntime(
        tmp_path, users=[{"id": "u1", "name": "Аня", "location": {"latitude": 55.7}, "fields": {}}],
        params=FakeParams(defs=("user.wifi_hash", "device.glagol_cert", "device.glagol_key",
                                "user.location.latitude", "device.name"),
                          values={("user:u1", "user.wifi_hash"): "abc123",
                                  ("device:SN-1", "device.glagol_cert"): "CERT",
                                  ("user:u1", "user.location.latitude"): 1.0,
                                  ("device:SN-1", "device.name"): "Из каталога"}))
    context = context_for(runtime, device_fixture())
    assert context["device_id"] == "id-1" and context["device_name"] == "Кухня"
    assert context["device"]["name"] == "Кухня" and context["device"]["sn"] == "SN-1"  # реестр сильнее каталога
    assert context["device"]["room"] == "Кухня" and context["device"]["fields"] == {}
    assert context["user_id"] == "u1" and context["user"]["name"] == "Аня"
    assert context["user"]["location"]["latitude"] == 55.7  # реестр сильнее каталога
    assert context["user"]["wifi_hash"] == "abc123"        # {{user.wifi_hash}} живых правил
    assert context["device"]["glagol_cert"] == "CERT"      # {{device.glagol_cert}} живых правил
    assert "glagol_key" not in context["device"]           # значения нет ни на одном уровне
    assert "name" not in context["user"]["location"]       # чужие пути не появляются


def test_substitutions_typed_whole_text_and_errors(tmp_path):
    """Строка из одной подстановки — значение как есть (число), внутри текста — текст;
    неразрешённая подстановка — правило пропущено (нижний слой), ошибка в errors."""
    runtime = FakeRuntime(tmp_path, users=[{"id": "u1", "name": "Аня", "location": {"latitude": 55.7}}])
    runtime.quasar_rules.save("common", [
        {"name": "lat", "path": "/lat", "kind": "select", "value": SINGLE,
         "options": {SINGLE: "{{user.location.latitude}}"}},
        {"name": "txt", "path": "/txt", "kind": "select", "value": SINGLE,
         "options": {SINGLE: "Имя: {{device.name}}, владелец {{user.name}}"}},
        {"name": "bad", "path": "/bad", "kind": "select", "value": SINGLE,
         "options": {SINGLE: "{{no.such.param}}"}},
        {"name": "after", "path": "/after", "kind": "select", "value": SINGLE, "options": {SINGLE: 7}}])
    errors = []
    body = compose_config(runtime, "", device_fixture(), errors=errors)
    assert body["lat"] == 55.7 and body["txt"] == "Имя: Кухня, владелец Аня"
    assert body["after"] == 7
    assert "bad" not in body  # колонка получает конфиг без этой замены
    assert [e["rule"] for e in errors] == ["bad"] and "неизвестный корень 'no'" in errors[0]["error"]
    assert errors[0]["layer"] == "общие замены" and errors[0]["path"] == "/bad"


def test_unfilled_param_error_text(tmp_path):
    """Параметр каталога без значения — «не заполнен параметр …»; неизвестный путь —
    обычная ошибка шаблона."""
    runtime = FakeRuntime(tmp_path, params=FakeParams(defs=("user.wifi_hash",), values={}))
    runtime.quasar_rules.save("common", [
        {"name": "hash", "path": "/hash", "kind": "select", "value": SINGLE,
         "options": {SINGLE: "{{user.wifi_hash}}"}},
        {"name": "bad", "path": "/bad", "kind": "select", "value": SINGLE,
         "options": {SINGLE: "{{no.such.thing}}"}}])
    errors = []
    compose_config(runtime, "", device_fixture(), errors=errors)
    texts = {e["rule"]: e["error"] for e in errors}
    assert texts["hash"] == "не заполнен параметр user.wifi_hash — вкладка «Параметры»"
    assert "неизвестный корень" in texts["bad"]


def test_shipped_layer_compose_and_errors(tmp_path):
    """Поставляемый набор действует как слой «общие замены»; правка state сильнее,
    снятое правило не применяется."""
    pkg = tmp_path / "pkg"
    (pkg / "platforms").mkdir(parents=True)
    (pkg / "common.json").write_text(json.dumps({"rules": [
        {"id": "s1", "name": "Шаблон", "path": "/spotter", "kind": "select",
         "value": "Значение", "options": {"Значение": "alisa"}},
        {"id": "s2", "name": "Снят", "path": "/gone", "kind": "select",
         "value": "Значение", "options": {"Значение": "x"}}]}), encoding="utf-8")
    runtime = FakeRuntime(tmp_path, shipped_dir=pkg)
    runtime.quasar_rules.save("common", [
        {"id": "s1", "name": "Шаблон", "path": "/spotter", "kind": "select",
         "value": "Значение", "options": {"Значение": "alisa"}},
        {"id": "s2", "name": "Снят", "path": "/gone", "kind": "delete"},
        {"name": "Своя", "path": "/own", "kind": "select", "value": SINGLE, "options": {SINGLE: 5}}])
    body = compose_config(runtime, "", device_fixture())
    assert body["spotter"] == "alisa"  # поставляемое применяется
    assert "gone" not in body          # снятое — delete-маркер, к конфигу не применяется
    assert body["own"] == 5            # новое правило state — после поставляемых
    effective, view = runtime.quasar_rules.layer_rules("common")
    assert [r["name"] for r in effective] == ["Шаблон", "Своя"]
    assert [(r.get("shipped", False), r.get("modified", False), r.get("removed", False))
            for r in view] == [(True, False, False), (True, True, True), (False, False, False)]


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
        {"name": "Теги", "path": "/tags", "kind": "select", "value": "Значение",
         "options": {"Значение": ["chiron-tag"]}},{"name": "gone", "path": "/subscription", "kind": "delete"}])
    body = compose_config(runtime, "chiron", device_fixture(), device_rules=[
        {"name": "Подписка", "path": "/subscription", "kind": "select",
         "value": "Выключена (invalid_subscription_user)", "options": SUBSCRIPTION},
        {"name": "Лампа", "path": "/extra/lamp", "kind": "select", "value": "Значение", "options": {"Значение": 42}},
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
        {"name": "off", "path": "/config/system_config/commandSpottersMode", "kind": "select",
         "value": "Значение", "options": {"Значение": "выкл"}, "enabled": False},
        {"name": "sdk", "path": "/config/system_config/com.yandex.io.sdk/flag", "kind": "select",
         "value": "Вкл", "options": {"Вкл": True, "Выкл": False}}])
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
                   device_rules=[{"name": "Лампа", "path": "/extra/lamp", "kind": "select",
                                  "value": "Значение", "options": {"Значение": 42}}])
    assert trace["/config/device_config/platform"] == "замены chiron: platform"
    assert trace["/subscription"] == "замены chiron: gone (удалено)"
    assert trace["/extra/lamp"] == "устройство: Лампа"
    assert trace["/tags"] == "общие замены: Теги"
    assert trace["/config/device_config/name"].startswith("динамика {{device_name}}")


def test_compose_account_and_device_order(tmp_path):
    """Устройство сильнее аккаунта, аккаунт сильнее платформы; аккаунт — только account_config."""
    account = [{"name": "s", "path": "/config/account_config/spotter", "kind": "select",
                "value": "Значение", "options": {"Значение": "аккаунт"}}]
    runtime = FakeRuntime(tmp_path, users=[{"id": "u1", "name": "U", "quasar_overrides": account}])
    runtime.quasar_rules.save("chiron", [{"name": "s", "path": "/config/account_config/spotter",
                                          "kind": "select", "value": "Значение",
                                          "options": {"Значение": "платформа"}}])
    device = device_fixture()
    rule = {"name": "s", "path": "/config/account_config/spotter", "kind": "select",
            "value": "Значение", "options": {"Значение": "устройство"}}
    assert compose_config(runtime, "chiron", device, device_rules=[rule])["config"]["account_config"]["spotter"] == "устройство"
    assert compose_config(runtime, "chiron", device)["config"]["account_config"]["spotter"] == "аккаунт"
    runtime.identities.data["users"][0]["quasar_overrides"] = []
    assert compose_config(runtime, "chiron", device)["config"]["account_config"]["spotter"] == "платформа"
    runtime.quasar_rules.save("chiron", [])
    assert compose_config(runtime, "chiron", device)["config"]["account_config"]["spotter"] == "alisa"  # шаблон
    runtime.identities.data["users"][0]["quasar_overrides"] = [
        {"name": "x", "path": "/config/device_config/x", "kind": "select",
         "value": "Значение", "options": {"Значение": 1}}]
    with pytest.raises(QuasarConfigError, match="account_config"):
        compose_config(runtime, "chiron", device)


def test_compose_render_false_keeps_params_and_markers(tmp_path):
    """Итог без устройства: параметр — {{ключ}} (не ошибка «не найден»), db/run — маркеры."""
    runtime = FakeRuntime(tmp_path)
    runtime.quasar_rules.save("common", [
        {"name": "hash", "path": "/config/account_config/user_wifi_config/wifi_hash", "kind": "select",
         "value": "Значение", "options": {"Значение": "{{user.wifi_hash}}"}},
        {"name": "loc", "path": "/location", "kind": "select", "value": "Значение",
         "options": {"Значение": {"db": "user.location"}}},
        {"name": "Подписка", "path": "/subscription", "kind": "select", "value": "Включена", "options": SUBSCRIPTION}])
    trace = {}
    body = compose_config(runtime, "", None, trace=trace, render=False)
    assert body["config"]["account_config"]["user_wifi_config"]["wifi_hash"] == "{{user.wifi_hash}}"
    assert body["location"] == {"db": "user.location"}
    assert body["subscription"]["statusUrl"].endswith("{{device_id}}")  # токен внутри варианта не тронут
    assert trace["/subscription"] == "общие замены: Подписка"
    errors = []  # с устройством привязка обязательна: правила пропущены, ошибки видны
    compose_config(runtime, "", device_fixture(), errors=errors)
    by_rule = {e["rule"]: e["error"] for e in errors}
    assert "user.wifi_hash" in by_rule["hash"] and "db: путь" in by_rule["loc"]


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
                                          "value": "Выключена (invalid_subscription_user)", "options": SUBSCRIPTION}]
            runtime.identities.mutate({'operation': 'state', 'sn': 'SN-1', 'state': state})
            async with client.get(url + "/get_sync_info?device_id=id-1",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert (await response.json())["subscription"]["enabled"] is False
            runtime.quasar_rules.save("common", [{"name": "bad", "path": "/x", "kind": "select",
                                                  "value": "Значение",
                                                  "options": {"Значение": "{{no.such.param}}"}}])
            async with client.get(url + "/get_sync_info?device_id=id-1",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 200  # сборка не падает: правило пропущено, ошибка видна в UI
                assert "x" not in (await response.json())
            recorded = runtime.quasar_errors["SN-1"]  # состояние для уведомления (api/live)
            assert recorded and recorded[0]["rule"] == "bad"
            async with client.get(url + "/emulator/api/live") as response:
                live = await response.json()
            assert live["quasar_errors"] and live["quasar_errors"][0]["sn"] == "SN-1"
            runtime.quasar_rules.save("common", [])
            async with client.get(url + "/get_sync_info?device_id=id-1",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 200
            assert runtime.quasar_errors == {}  # следующая сборка без ошибки снимает состояние
            async with client.get(url + "/emulator/api/live") as response:
                assert (await response.json())["quasar_errors"] == []
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
    rule = {"name": "mode", "path": "/config/system_config/commandSpottersMode", "kind": "select",
            "value": "Значение", "options": {"Значение": ""}}
    runtime.quasar_rules.save("common", [{**rule, "options": {"Значение": "из-общих"}}])
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
            runtime.quasar_rules.save("yandexstation", [{**rule, "options": {"Значение": "из-платформы"}}])
            assert await mode_of(client) == "из-платформы"
            runtime.quasar_rules.remove_platform("yandexstation")
            assert await mode_of(client) == "из-общих"
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_get_sync_info_custom_unchanged(tmp_path):
    """«Свой» источник: сохранённый ответ как есть — без слоёв, glagol.security и группы."""
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-1', 'user_id': 'u1',
                               'device_ids': ['SN-1'], 'profile': {'platform': 'yandexstation'}})
    runtime.identities.mutate({'operation': 'state', 'sn': 'SN-1', 'state': {
        'quasar_source': 'custom',
        'quasar_custom': {'status': 'ok', 'config': {'a': 1}, 'tags': ['t']}}})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/get_sync_info?device_id=SN-1",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 200
                assert await response.json() == {'status': 'ok', 'config': {'a': 1}, 'tags': ['t']}
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
    runtime.quasar_rules.save("common", [{"name": "group", "path": "/group", "kind": "select",
                                          "value": "Значение",
                                          "options": {"Значение": {"name": "шаблон", "from": "common"}}}])
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
