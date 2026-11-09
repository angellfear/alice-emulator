"""Замены Quasar-конфига: правила, перевод прежнего формата, перенос тел, хранилище, API."""
import json

import pytest
from aiohttp import ClientSession

from server_emulator.quasar_config import QuasarConfigError, generated_base, merge_patch
from server_emulator.quasar_rules import (RuleStore, body_to_rules, compose_config, legacy_override_to_rule,
                                          rules_as_list, validate_rules)
from server_emulator.runtime import Runtime
from tests.conftest import listen
from test_quasar_config import OPTIONS, FakeRuntime

HEADERS = {"X-Emulator": "1"}
ONE = "Значение"


def state(rule):
    """Значение текущего состояния замены."""
    return rule["options"][rule["value"]]


def test_validate_rules_normalizes_and_reports_line():
    rules = validate_rules([{"name": " Флаг ", "path": "/a/b", "kind": "bool", "value": False},
                            {"path": "/c", "kind": "delete", "value": 5},
                            {"path": "/d", "kind": "select", "value": "Б", "options": {"А": 1, "Б": 2, "В": 3}}])
    assert rules[0]["name"] == "Флаг" and rules[0]["enabled"] is True and rules[0]["id"]
    assert rules[0]["kind"] == "select" and rules[0]["options"] == {"Вкл": True, "Выкл": False}
    assert rules[0]["value"] == "Выкл"                       # флаг — два состояния, первое «вкл»
    assert rules[1]["value"] is None and rules[0]["id"] != rules[1]["id"]
    assert state(rules[2]) == 2
    for bad, text in (({"path": "a.b", "kind": "value"}, "JSON Pointer"),
                      ({"path": "/a", "kind": "bool", "value": 1}, "true или false"),
                      ({"path": "/a", "kind": "select", "value": "x", "options": {"y": 1}}, "не из списка"),
                      ({"path": "/a", "kind": "select", "value": "", "options": {"": 1}}, "пустое имя"),
                      ({"path": "/a", "kind": "select", "value": "x", "options": {}}, "хотя бы одно"),
                      ({"path": "/a", "kind": "param", "value": " "}, "пустое"),
                      ({"path": "/a//b", "kind": "value"}, "пустой сегмент"),
                      ({"path": "/a", "kind": "regex"}, "вид")):
        with pytest.raises(QuasarConfigError, match="Строка 2: .*" + text):
            validate_rules([{"path": "/ok", "kind": "value", "value": 1}, bad])


def test_legacy_kinds_become_states():
    """Прежние виды — одно состояние с тем же значением (подстановка, маркер db/run)."""
    one = {"name": "b", "path": "/config/a/b", "kind": "select", "value": ONE}
    assert legacy_override_to_rule({"path": "config.a.b", "source": {"expr": "{{x}}"}}) ==         {**one, "options": {ONE: "{{x}}"}}
    assert legacy_override_to_rule({"path": "a", "source": {"delete": True}})["kind"] == "delete"
    assert state(legacy_override_to_rule({"path": "a", "source": {"db": "user.name"}})) == {"db": "user.name"}
    assert state(legacy_override_to_rule({"path": "a", "source": {"run": "x.bat"}})) == {"run": "x.bat"}
    assert legacy_override_to_rule({"path": "a", "source": False})["options"] == {"Вкл": True, "Выкл": False}
    assert state(legacy_override_to_rule({"path": "a", "source": {"k": 1}})) == {"k": 1}
    assert state(validate_rules([{"path": "/p", "kind": "param", "value": "user.name"}])[0]) == "{{user.name}}"
    rule = legacy_override_to_rule({"path": "subscription", "source": {"$select": "Включена"}},
                                   {"/subscription": OPTIONS})
    assert rule["kind"] == "select" and rule["options"] == OPTIONS
    flag = legacy_override_to_rule({"path": "f", "source": {"$bool": True, "$name": "Флаг"}})
    assert flag["name"] == "Флаг" and flag["value"] == "Вкл"
    mixed = rules_as_list([{"path": "/n", "kind": "value", "value": 1}, {"path": "o", "source": 2},
                           {"path": "/x", "kind": "bool", "value": "битое"}, "мусор"])
    assert [r["path"] for r in mixed] == ["/n", "/o"]          # битая запись пропускается


def test_body_to_rules_differs_from_any_reference():
    refs = [{"a": 1, "b": {"x": 1, "y": 2}}, {"a": 2, "b": {"x": 1}}]
    rules = body_to_rules({"a": 1, "b": {"x": 1, "y": None, "z": True}, "c": {"$param": "user.name"},
                           "d": "{{device_name}}", "e": {}}, refs)
    by_path = {r["path"]: r for r in rules}
    assert state(by_path["/a"]) == 1                       # отличается от второй ссылки
    assert "/b/x" not in by_path                           # совпадает со всеми
    assert by_path["/b/y"]["kind"] == "delete"             # null над существующим ключом
    assert by_path["/b/z"]["options"] == {"Вкл": True, "Выкл": False}
    assert state(by_path["/c"]) == "{{user.name}}" and state(by_path["/d"]) == "{{device_name}}"
    assert state(by_path["/e"]) == {}                      # новый пустой объект — создаётся
    assert body_to_rules({"b": {}}, refs) == []            # пустой объект поверх объекта — без изменений


def test_migrate_keeps_effective_result(tmp_path):
    """Перенос прежних тел даёт тот же итог, что получали колонки: null в файле платформы
    не действовал (merge-patch в пустой объект его отбрасывал) — и после переноса не действует."""
    quasar = tmp_path / "quasar"
    (quasar / "platforms").mkdir(parents=True)
    common = {"config": {"system_config": {"commandSpottersMode": "из-common"}},
              "subscription": {"$select": "Вкл", "Вкл": {"enabled": True}, "Выкл": {"enabled": False}}}
    platform = {"config": {"system_config": {"hybrid_request_factory": {"hybrid_voice_inputs": None},
                                             "audio": {}, "extra": 1}}}
    (quasar / "common.json").write_text(json.dumps(common), encoding="utf-8")
    (quasar / "platforms" / "yandexstation.json").write_text(json.dumps(platform), encoding="utf-8")
    runtime = FakeRuntime(tmp_path, users=[{"id": "u1", "name": "U"}])
    store = runtime.quasar_rules
    report = store.migrate()
    assert report["common"] >= 2 and report["platforms"] == {"yandexstation": 2}  # extra + новый audio: {}
    assert not (quasar / "common.json").exists() and not (quasar / "platforms").exists()
    backup = next(quasar.glob("legacy-*"))
    assert json.loads((backup / "common.json").read_text(encoding="utf-8")) == common
    assert store.migrate() is None  # однократно
    body = compose_config(runtime, "yandexstation", None, render=False)
    system = body["config"]["system_config"]
    expected = merge_patch(merge_patch(generated_base("yandexstation"), {
        "config": {"system_config": {"commandSpottersMode": "из-common"}}}),
        {"config": {"system_config": {"extra": 1}}})["config"]["system_config"]
    assert system["commandSpottersMode"] == "из-common" and system["extra"] == 1
    assert system["hybrid_request_factory"] == expected["hybrid_request_factory"]  # null не применён
    assert system["hybrid_request_factory"]["hybrid_voice_inputs"] is True
    assert body["subscription"] == {"enabled": True}
    assert any(r["kind"] == "select" for r in store.load("common"))


def test_store_files_platforms_and_errors(tmp_path):
    store = RuleStore(tmp_path / "quasar")
    assert store.is_empty() and store.load("common") == [] and store.platforms() == []
    store.save("chiron", [{"path": "/a", "kind": "value", "value": 1}])
    assert not store.is_empty() and store.platforms() == ["chiron"]
    store.create_platform("station_x")
    with pytest.raises(QuasarConfigError, match="уже есть"):
        store.create_platform("station_x")
    store.rename_platform("station_x", "station_y")
    assert store.platforms() == ["chiron", "station_y"]
    assert store.remove_platform("station_y") and not store.remove_platform("station_y")
    for bad in ("../x", "Big", "a/b"):
        with pytest.raises(QuasarConfigError):
            store.save(bad, [])
    with pytest.raises(QuasarConfigError, match="зарезервировано"):
        store.create_platform("common")
    with pytest.raises(QuasarConfigError, match="Строка 1"):
        store.save("common", [{"path": "/a", "kind": "bool", "value": "да"}])
    assert json.loads((tmp_path / "quasar/rules/platforms/chiron.json").read_text(encoding="utf-8"))["rules"][0]["path"] == "/a"


@pytest.mark.asyncio
async def test_rules_api_layers_and_validation(tmp_path):
    from server_emulator.server import create_app

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-1', 'user_id': 'u1',
                               'device_ids': ['id-1'], 'profile': {'platform': 'chiron'}})
    state = dict(runtime.identities.resolve('id-1')['state'])
    state['quasar_overrides'] = [{"path": "extra.lamp", "source": 42}]  # прежний формат
    runtime.identities.mutate({'operation': 'state', 'sn': 'SN-1', 'state': state})
    runner, url = await listen(create_app(runtime=runtime))
    api = url + "/emulator/api/"
    rule = {"name": "Хеш", "path": "/config/account_config/user_wifi_config/wifi_hash", "kind": "param",
            "value": "user.wifi_hash"}
    try:
        async with ClientSession() as client:
            async with client.put(api + "quasar_rules", headers=HEADERS,
                                  json={"layer": "common", "rules": [rule]}) as response:
                assert response.status == 200
                assert (await response.json())["rules"][0]["id"]
            async with client.put(api + "quasar_rules", headers=HEADERS,
                                  json={"layer": "common", "rules": [{**rule, "kind": "bool"}]}) as response:
                assert response.status == 400 and "Строка 1" in (await response.json())["error"]
            async with client.get(api + "quasar_rules?layer=common") as response:
                body = await response.json()
                assert body["rules"][0]["name"] == "Хеш" and body["inherited"] == []
            async with client.put(api + "quasar_rules", headers=HEADERS, json={"layer": "platform:chiron", "rules": [
                    {"name": "p", "path": "/config/device_config/platform", "kind": "value", "value": "chiron"}]}) as response:
                assert response.status == 200
            async with client.get(api + "quasar_rules?layer=device:SN-1") as response:
                body = await response.json()
                assert body["platform"] == "chiron"
                assert body["rules"] == [{"name": "lamp", "path": "/extra/lamp", "kind": "select", "value": ONE,
                                          "options": {ONE: 42}}]
            async with client.put(api + "quasar_rules", headers=HEADERS,
                                  json={"layer": "device:SN-1", "rules": body["rules"]}) as response:
                assert response.status == 200
            assert runtime.identities.resolve('id-1')['state']['quasar_overrides'][0]['kind'] == "select"
            async with client.get(api + "quasar_rules?layer=bogus") as response:
                assert response.status == 400
            # обзор, итог без устройства: параметр — тег {{ключ}}, не ошибка
            async with client.get(api + "quasar_platforms") as response:
                body = await response.json()
                assert body["common_rules"] == 1 and {"platform": "chiron", "rules": 1} in body["platforms"]
                assert "chiron" in body["builtin_platforms"] and body["devices"] == 1
            async with client.get(api + "quasar_platforms?compose=chiron") as response:
                body = await response.json()
                assert body["config"]["config"]["account_config"]["user_wifi_config"]["wifi_hash"] == "{{user.wifi_hash}}"
                assert body["origins"]["config.device_config.platform"] == "замены chiron: p"
            async with client.get(api + "quasar_platforms?builtin=chiron") as response:
                assert (await response.json())["keys"] > 0
            # итог устройства: без значения параметра — ошибка в ответе, raw=1 — тег
            async with client.get(api + "quasar_composed?device=SN-1") as response:
                assert "user.wifi_hash" in (await response.json())["error"]
            async with client.get(api + "quasar_composed?raw=1&device=SN-1") as response:
                body = await response.json()
                assert body["config"]["extra"]["lamp"] == 42
                assert body["origins"]["extra.lamp"] == "устройство: lamp"
            # платформы: создать / переименовать / удалить
            async with client.post(api + "quasar_platforms", headers=HEADERS,
                                   json={"action": "create", "platform": "station_x"}) as response:
                assert response.status == 200
            async with client.post(api + "quasar_platforms", headers=HEADERS,
                                   json={"action": "rename", "platform": "station_x", "to": "station_y"}) as response:
                assert (await response.json())["platform"] == "station_y"
            async with client.delete(api + "quasar_platforms?platform=station_y", headers=HEADERS) as response:
                assert response.status == 200
            async with client.delete(api + "quasar_platforms?platform=station_y", headers=HEADERS) as response:
                assert response.status == 400
    finally:
        await runner.cleanup()
        await runtime.close()


def test_state_value_substitutions_and_params(tmp_path):
    """В значении состояния — подстановки: строка из одной подстановки даёт значение как есть
    (число из реестра), внутри текста — текст; маркер db — значение реестра; нет параметра — ошибка."""
    runtime = FakeRuntime(tmp_path, users=[{"id": "u1", "name": "Аня", "location": {"latitude": 55.7}}])
    device = {"sn": "SN-1", "device_ids": ["id-1"], "name": "Кухня", "user_id": "u1", "profile": {}}
    runtime.quasar_rules.save("common", [
        {"name": "lat", "path": "/lat", "kind": "select", "value": ONE, "options": {ONE: "{{user.location.latitude}}"}},
        {"name": "txt", "path": "/txt", "kind": "select", "value": ONE, "options": {ONE: "{{device_name}} у {{user.name}}"}},
        {"name": "loc", "path": "/loc", "kind": "select", "value": ONE, "options": {ONE: {"db": "user.location"}}},
        {"name": "mode", "path": "/mode", "kind": "select", "value": "Б", "options": {"А": 1, "Б": "{{device_id}}", "В": 3}}])
    body = compose_config(runtime, "", device)
    assert body["lat"] == 55.7 and body["txt"] == "Кухня у Аня"
    assert body["loc"] == {"latitude": 55.7} and body["mode"] == "id-1"
    runtime.quasar_rules.save("common", [{"name": "x", "path": "/x", "kind": "select", "value": ONE,
                                          "options": {ONE: "{{no.such.param}}"}}])
    with pytest.raises(QuasarConfigError, match="не найден"):
        compose_config(runtime, "", device)
    assert compose_config(runtime, "", None, render=False)["x"] == "{{no.such.param}}"


def test_local_address_by_route_mode(tmp_path):
    """«Эмуляция» и «Вместе» — голос на ws://alica.lan; «Прозрачно» — шаблон как есть;
    замена устройства сильнее локального адреса."""
    runtime = FakeRuntime(tmp_path)
    path = "/config/system_config/uniProxyUrl"
    base = generated_base("yandexstation")["config"]["system_config"]["uniProxyUrl"]

    def url(device, rules=None, trace=None):
        return compose_config(runtime, "yandexstation", device, device_rules=rules,
                              trace=trace)["config"]["system_config"]["uniProxyUrl"]

    trace = {}
    assert url({"sn": "SN-1", "profile": {}}, trace=trace) == "ws://alica.lan/uni.ws"
    assert trace["config.system_config.uniProxyUrl"].startswith("локальный адрес alica.lan")
    assert url({"sn": "SN-1", "route_mode": "hybrid", "profile": {}}) == "ws://alica.lan/uni.ws"
    assert url({"sn": "SN-1", "route_mode": "proxy", "profile": {}}) == base
    own = [{"name": "u", "path": path, "kind": "select", "value": ONE, "options": {ONE: "wss://x/uni.ws"}}]
    assert url({"sn": "SN-1", "profile": {}}, own) == "wss://x/uni.ws"
