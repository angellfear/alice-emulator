"""Замены Quasar-конфига: правила, хранилище, API."""
import json

import pytest
from aiohttp import ClientSession

from server_emulator.quasar_config import QuasarConfigError, generated_base
from server_emulator.quasar_rules import RuleStore, compose_config, validate_rules
from server_emulator.runtime import Runtime
from tests.conftest import listen

HEADERS = {"X-Emulator": "1"}
ONE = "Значение"


def state(rule):
    """Значение текущего состояния замены."""
    return rule["options"][rule["value"]]


def test_validate_rules_reports_line():
    rules = validate_rules([
        {"name": " Флаг ", "path": "/a/b", "kind": "select", "value": "Вкл",
         "options": {"Вкл": True, "Выкл": False}},
        {"path": "/c", "kind": "delete", "value": 5},
        {"path": "/d", "kind": "select", "value": "Б", "options": {"А": 1, "Б": 2, "В": 3}}])
    assert rules[0]["name"] == "Флаг" and rules[0]["enabled"] is True and rules[0]["id"]
    assert rules[0]["options"] == {"Вкл": True, "Выкл": False}
    assert rules[1]["value"] is None and rules[0]["id"] != rules[1]["id"]
    assert state(rules[2]) == 2
    for bad, text in (({"path": "a.b", "kind": "select", "value": "x", "options": {"x": 1}}, "JSON Pointer"),
                      ({"path": "/a", "kind": "select", "value": "x", "options": {"y": 1}}, "не из списка"),
                      ({"path": "/a", "kind": "select", "value": "", "options": {"": 1}}, "пустое имя"),
                      ({"path": "/a", "kind": "select", "value": "x", "options": {}}, "хотя бы одно"),
                      ({"path": "/a", "kind": "param", "value": "x"}, "вид"),
                      ({"path": "/a//b", "kind": "delete"}, "пустой сегмент"),
                      ({"path": "/a", "kind": "regex"}, "вид")):
        with pytest.raises(QuasarConfigError, match="Строка 2: .*" + text):
            validate_rules([{"path": "/ok", "kind": "select", "value": "x", "options": {"x": 1}}, bad])


def test_validate_rules_pydantic_fields_and_template_syntax():
    """Проверка при сохранении (pydantic): лишнее поле — ошибка с полем; битый шаблон
    подстановки не сохраняется."""
    with pytest.raises(QuasarConfigError, match="лишнее"):
        validate_rules([{"path": "/a", "kind": "select", "value": ONE, "options": {ONE: 1},
                         "лишнее": 1}])
    for bad in ("{{ x | qq }}", "{{ x + }}"):
        with pytest.raises(QuasarConfigError, match="template"):
            validate_rules([{"path": "/a", "kind": "select", "value": ONE, "options": {ONE: bad}}])
    assert validate_rules([{"path": "/a", "kind": "delete", "value": "мусор",
                            "options": {"лишний": 1}}])[0]["value"] is None


def test_shipped_layer_diff_save_and_reset(tmp_path):
    """save хранит только дифф поверх поставляемого: совпадающее не сохраняется,
    отсутствующее — delete-маркер, сброс возвращает поставляемое."""
    pkg = tmp_path / "pkg"
    (pkg / "platforms").mkdir(parents=True)
    base = {"name": "Шаблон", "path": "/config/account_config/spotter", "kind": "select",
            "value": "Значение", "options": {"Значение": "alisa"}}
    (pkg / "common.json").write_text(json.dumps({"rules": [{**base, "id": "s1"}]}), encoding="utf-8")
    store = RuleStore(tmp_path / "quasar", shipped_dir=pkg)
    assert store.shipped("common") and store.platforms() == []
    store.save("common", [{**base, "id": "s1", "options": {"Значение": "jarvis"}},
                          {"name": "Новая", "path": "/extra", "kind": "select",
                           "value": "Значение", "options": {"Значение": 1}}])
    saved = json.loads((tmp_path / "quasar/rules/common.json").read_text(encoding="utf-8"))["rules"]
    assert len(saved) == 2 and saved[0]["id"] == "s1"  # дифф: правка + новое
    store.save("common", [base | {"id": "s1"}])  # правка совпадает с поставляемым — не хранится
    assert json.loads((tmp_path / "quasar/rules/common.json").read_text(encoding="utf-8"))["rules"] == []
    store.save("common", [])  # правило удалено из списка → delete-маркер
    saved = json.loads((tmp_path / "quasar/rules/common.json").read_text(encoding="utf-8"))["rules"]
    assert [(r["id"], r["kind"], r["path"]) for r in saved] == [("s1", "delete", base["path"])]
    effective, view = store.layer_rules("common")
    assert effective == [] and view[0]["removed"] and view[0]["base"]["id"] == "s1"
    store.save("common", [view[0]["base"]])  # сброс: исходное вместо маркера
    assert json.loads((tmp_path / "quasar/rules/common.json").read_text(encoding="utf-8"))["rules"] == []
    assert [r["name"] for r in store.layer_rules("common")[0]] == ["Шаблон"]


def test_shipped_platforms(tmp_path):
    pkg = tmp_path / "pkg"
    (pkg / "platforms").mkdir(parents=True)
    (pkg / "platforms" / "chiron.json").write_text(json.dumps({"rules": [
        {"id": "p1", "name": "p", "path": "/config/device_config/platform", "kind": "select",
         "value": "Значение", "options": {"Значение": "chiron"}}]}), encoding="utf-8")
    store = RuleStore(tmp_path / "quasar", shipped_dir=pkg)
    assert "chiron" in store.platforms()
    effective, view = store.layer_rules("chiron")
    assert [r["id"] for r in effective] == ["p1"] and view[0]["shipped"]
    store.save("chiron", [])  # снятие → delete-маркер в state, поставляемая платформа в списке
    assert store.layer_rules("chiron")[0] == []
    assert "chiron" in store.platforms()


def test_shipped_package_files_validate():
    """Поставляемый набор в пакете читается и проходит валидацию замены целиком
    (RuleModel: вид/путь/состояния/синтаксис подстановок), id уникальны внутри слоя."""
    from server_emulator.quasar_rules import QUASAR_RULES_DIR
    root = QUASAR_RULES_DIR
    assert (root / "common.json").exists()
    for path in [root / "common.json"] + sorted((root / "platforms").glob("*.json")):
        rules = validate_rules(json.loads(path.read_text(encoding="utf-8"))["rules"])
        assert len({r["id"] for r in rules}) == len(rules), path.name


def test_store_files_platforms_and_errors(tmp_path):
    store = RuleStore(tmp_path / "quasar")
    assert store.load("common") == [] and store.platforms() == []
    store.save("chiron", [{"name": "a", "path": "/a", "kind": "select",
                           "value": ONE, "options": {ONE: 1}}])
    assert store.platforms() == ["chiron"]
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
        store.save("common", [{"path": "/a", "kind": "param", "value": "x"}])
    saved = json.loads((tmp_path / "quasar/rules/platforms/chiron.json").read_text(encoding="utf-8"))
    assert saved["rules"][0]["path"] == "/a" and saved["rules"][0]["kind"] == "select"


@pytest.mark.asyncio
async def test_rules_api_layers_and_validation(tmp_path):
    from server_emulator.server import create_app

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-1', 'user_id': 'u1',
                               'device_ids': ['id-1'], 'profile': {'platform': 'chiron'}})
    state = dict(runtime.identities.resolve('id-1')['state'])
    state['quasar_overrides'] = [{"name": "lamp", "path": "/extra/lamp", "kind": "select",
                                  "value": ONE, "options": {ONE: 42}}]
    runtime.identities.mutate({'operation': 'state', 'sn': 'SN-1', 'state': state})
    runner, url = await listen(create_app(runtime=runtime))
    api = url + "/emulator/api/"
    rule = {"name": "Хеш", "path": "/config/account_config/user_wifi_config/wifi_hash", "kind": "select",
            "value": ONE, "options": {ONE: "{{user.wifi_hash}}"}}
    try:
        async with ClientSession() as client:
            async with client.put(api + "quasar_rules", headers=HEADERS,
                                  json={"layer": "common", "rules": [rule]}) as response:
                assert response.status == 200
                assert (await response.json())["rules"][0]["id"]
            async with client.put(api + "quasar_rules", headers=HEADERS,
                                  json={"layer": "common", "rules": [{**rule, "kind": "param", "value": "x"}]}) as response:
                assert response.status == 400 and "Строка 1" in (await response.json())["error"]
            async with client.get(api + "quasar_rules?layer=common") as response:
                body = await response.json()
                assert body["rules"][0]["name"] == "Хеш" and body["inherited"] == []
            async with client.put(api + "quasar_rules", headers=HEADERS, json={"layer": "platform:chiron", "rules": [
                    {"name": "p", "path": "/config/device_config/platform", "kind": "select",
                     "value": ONE, "options": {ONE: "chiron"}}]}) as response:
                assert response.status == 200
            async with client.get(api + "quasar_rules?layer=device:SN-1") as response:
                body = await response.json()
                assert body["platform"] == "chiron"
                assert body["rules"] == [{"name": "lamp", "path": "/extra/lamp", "kind": "select",
                                          "value": ONE, "options": {ONE: 42}}]
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
                assert body["origins"]["/config/device_config/platform"] == "замены chiron: p"
            async with client.get(api + "quasar_platforms?builtin=chiron") as response:
                assert (await response.json())["keys"] > 0
            # итог устройства: без значения привязки — правило пропущено, ошибка в errors; raw=1 — тег
            async with client.get(api + "quasar_composed?device=SN-1") as response:
                body = await response.json()
                assert body["errors"] and body["errors"][0]["rule"] == "Хеш"
                assert "wifi_hash" not in body["config"]["config"].get("account_config", {})
            async with client.get(api + "quasar_composed?raw=1&device=SN-1") as response:
                body = await response.json()
                assert body["config"]["extra"]["lamp"] == 42
                assert body["origins"]["/extra/lamp"] == "устройство: lamp"
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
    from test_quasar_config import FakeRuntime
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
    errors = []  # привязка обязательна: правило пропущено, ошибка видна (не пустая строка)
    compose_config(runtime, "", device, errors=errors)
    assert [e["rule"] for e in errors] == ["x"] and "неизвестный корень" in errors[0]["error"]
    assert compose_config(runtime, "", None, render=False)["x"] == "{{no.such.param}}"


def test_local_address_by_route_mode(tmp_path):
    """«Эмуляция» и «Вместе» — голос на ws://alica.lan; «Прозрачно» — шаблон как есть;
    замена устройства сильнее локального адреса."""
    from test_quasar_config import FakeRuntime
    runtime = FakeRuntime(tmp_path)
    path = "/config/system_config/uniProxyUrl"
    base = generated_base("yandexstation")["config"]["system_config"]["uniProxyUrl"]

    def url(device, rules=None, trace=None):
        return compose_config(runtime, "yandexstation", device, device_rules=rules,
                              trace=trace)["config"]["system_config"]["uniProxyUrl"]

    trace = {}
    assert url({"sn": "SN-1", "profile": {}}, trace=trace) == "ws://alica.lan/uni.ws"
    assert trace["/config/system_config/uniProxyUrl"].startswith("локальный адрес alica.lan")
    assert url({"sn": "SN-1", "route_mode": "hybrid", "profile": {}}) == "ws://alica.lan/uni.ws"
    assert url({"sn": "SN-1", "route_mode": "proxy", "profile": {}}) == base
    xiva = compose_config(runtime, "yandexstation", {"sn": "SN-1", "profile": {}})
    assert xiva["config"]["system_config"]["pushd"]["xivaSubscribeUrl"] == "ws://alica.lan/v2/subscribe/websocket"
    assert xiva["config"]["system_config"]["arpc_client"]["server_url"] == "http://alica.lan"
    own = [{"name": "u", "path": path, "kind": "select", "value": ONE, "options": {ONE: "wss://x/uni.ws"}}]
    assert url({"sn": "SN-1", "profile": {}}, own) == "wss://x/uni.ws"


def test_local_address_follows_server_domain(tmp_path):
    """Домен — из server.json на каждую сборку; пустой — uniProxyUrl из шаблона."""
    from server_emulator import server_config
    from test_quasar_config import FakeRuntime
    runtime = FakeRuntime(tmp_path)
    base = generated_base("yandexstation")["config"]["system_config"]["uniProxyUrl"]
    device = {"sn": "SN-1", "profile": {}}

    def url():
        return compose_config(runtime, "yandexstation", device)["config"]["system_config"]["uniProxyUrl"]

    server_config.save(tmp_path, server_config.validate(tmp_path, {"ssl": False, "local_domain": "emu.home"}))
    assert url() == "ws://emu.home/uni.ws"
    server_config.save(tmp_path, server_config.validate(tmp_path, {"ssl": False, "local_domain": ""}))
    assert url() == base
