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


def test_validate_rules_normalizes_and_reports_line():
    rules = validate_rules([{"name": " Флаг ", "path": "/a/b", "kind": "bool", "value": False},
                            {"path": "/c", "kind": "delete", "value": 5}])
    assert rules[0]["name"] == "Флаг" and rules[0]["enabled"] is True and rules[0]["id"]
    assert rules[1]["value"] is None and rules[0]["id"] != rules[1]["id"]
    for bad, text in (({"path": "a.b", "kind": "value"}, "JSON Pointer"),
                      ({"path": "/a", "kind": "bool", "value": 1}, "true или false"),
                      ({"path": "/a", "kind": "select", "value": "x", "options": {"y": 1}}, "не из списка"),
                      ({"path": "/a", "kind": "param", "value": " "}, "пустое"),
                      ({"path": "/a//b", "kind": "value"}, "пустой сегмент"),
                      ({"path": "/a", "kind": "regex"}, "вид")):
        with pytest.raises(QuasarConfigError, match="Строка 2: .*" + text):
            validate_rules([{"path": "/ok", "kind": "value", "value": 1}, bad])


def test_legacy_override_conversion():
    assert legacy_override_to_rule({"path": "config.a.b", "source": {"expr": "{{x}}"}}) == \
        {"name": "b", "path": "/config/a/b", "kind": "template", "value": "{{x}}"}
    assert legacy_override_to_rule({"path": "a", "source": {"delete": True}})["kind"] == "delete"
    assert legacy_override_to_rule({"path": "a", "source": {"db": "user.name"}})["kind"] == "db"
    assert legacy_override_to_rule({"path": "a", "source": {"run": "x.bat"}})["kind"] == "script"
    assert legacy_override_to_rule({"path": "a", "source": False})["kind"] == "bool"
    assert legacy_override_to_rule({"path": "a", "source": {"k": 1}})["kind"] == "value"
    rule = legacy_override_to_rule({"path": "subscription", "source": {"$select": "Включена"}},
                                   {"/subscription": OPTIONS})
    assert rule["kind"] == "select" and rule["options"] == OPTIONS
    assert legacy_override_to_rule({"path": "f", "source": {"$bool": True, "$name": "Флаг"}}) == \
        {"name": "Флаг", "path": "/f", "kind": "bool", "value": True}
    mixed = rules_as_list([{"path": "/n", "kind": "value", "value": 1}, {"path": "o", "source": 2}, "мусор"])
    assert [r["path"] for r in mixed] == ["/n", "/o"]


def test_body_to_rules_differs_from_any_reference():
    refs = [{"a": 1, "b": {"x": 1, "y": 2}}, {"a": 2, "b": {"x": 1}}]
    rules = body_to_rules({"a": 1, "b": {"x": 1, "y": None, "z": True}, "c": {"$param": "user.name"},
                           "d": "{{device_name}}", "e": {}}, refs)
    by_path = {r["path"]: r for r in rules}
    assert by_path["/a"]["value"] == 1                     # отличается от второй ссылки
    assert "/b/x" not in by_path                           # совпадает со всеми
    assert by_path["/b/y"]["kind"] == "delete"             # null над существующим ключом
    assert by_path["/b/z"]["kind"] == "bool"
    assert by_path["/c"]["kind"] == "param" and by_path["/d"]["kind"] == "template"
    assert by_path["/e"]["value"] == {}                    # новый пустой объект — создаётся
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
                assert body["rules"] == [{"name": "lamp", "path": "/extra/lamp", "kind": "value", "value": 42}]
            async with client.put(api + "quasar_rules", headers=HEADERS,
                                  json={"layer": "device:SN-1", "rules": body["rules"]}) as response:
                assert response.status == 200
            assert runtime.identities.resolve('id-1')['state']['quasar_overrides'][0]['kind'] == "value"
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
