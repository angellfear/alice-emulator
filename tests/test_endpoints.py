"""Каталог эндпоинтов: хранилище, порядок приоритетов dispatch, admin-API.
tools/endpoints_recover.py: извлечение тела из capture-базы (фикстура-миниатюра)."""
import base64
import json
import sqlite3

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient

from server_emulator.endpoints import EndpointCatalog
from server_emulator.runtime import Runtime


def endpoint(**overrides):
    value = {"id": "t.get", "title": "GET /get", "_group": "t.host", "enabled": True,
             "host": "t.host", "method": "GET", "path": "/get", "status": 200,
             "content_type": "application/json",
             "body_base64": base64.b64encode(b'{"ok":true}').decode(),
             "captured_ts": None, "source": "test", "notes": ""}
    value.update(overrides)
    return value


def test_catalog_roundtrip_toggle_remove(tmp_path):
    catalog = EndpointCatalog(tmp_path / "endpoints")
    assert catalog.public()["total"] == 0
    catalog.upsert(endpoint())
    catalog.upsert(endpoint(id="t.post", method="POST", path="/post",
                            body_base64=base64.b64encode(b"{}").decode()))
    assert catalog.find("T.HOST", "GET", "/get")["id"] == "t.get"  # host без регистра
    assert catalog.find("t.host", "GET", "/get?x=1") is None  # query не входит в матчинг
    catalog.toggle("t.get", False)
    assert catalog.find("t.host", "GET", "/get")["enabled"] is False
    # файлы — источник правды: перечитывание видит то же состояние
    reloaded = EndpointCatalog(tmp_path / "endpoints")
    assert reloaded.find("t.host", "GET", "/get")["enabled"] is False
    reloaded.remove("t.get")
    assert reloaded.find("t.host", "GET", "/get") is None


def test_catalog_skips_broken_files(tmp_path):
    directory = tmp_path / "endpoints"
    directory.mkdir()
    (directory / "good.json").write_text(json.dumps(endpoint()), encoding="utf-8")
    (directory / "bad.json").write_text("{not json", encoding="utf-8")
    (directory / "invalid.json").write_text(json.dumps({"id": "x", "host": "t.host"}), encoding="utf-8")
    catalog = EndpointCatalog(directory)
    assert [e["id"] for e in catalog.endpoints] == ["t.get"]


def test_catalog_validation(tmp_path):
    catalog = EndpointCatalog(tmp_path / "endpoints")
    with pytest.raises(ValueError, match="body_base64"):
        catalog.upsert(endpoint(body_base64=None))  # тип; пустая строка = ответ без тела
    catalog.upsert(endpoint(id="t.empty", body_base64=""))  # 200-ack без тела
    with pytest.raises(ValueError, match="status"):
        catalog.upsert(endpoint(status="200"))


@pytest.mark.asyncio
async def test_endpoint_replay_priority_and_fallback(tmp_path):
    """Приоритет: builtin > эндпоинт (не затеняет stateful-маршруты) > 404.
    Выключенный эндпоинт прозрачен — маршрут снова 404."""
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    # /check_updates — builtin Quasar; эндпоинт с тем же маршрутом его не затеняет
    runtime.endpoints.upsert(endpoint(host="quasar.yandex.net", path="/check_updates",
                                      id="quasar.check_updates",
                                      body_base64=base64.b64encode(b'{"shadow":true}').decode()))
    # незнакомый маршрут — переигрывается захваченным ответом
    runtime.endpoints.upsert(endpoint(host="rpc.alice.yandex.net", path="/gproxy/get_teasers",
                                      id="rpc.get_teasers", method="POST",
                                      body_base64=base64.b64encode(b'{"teasers":[]}').decode()))
    # выключенный эндпоинт — как будто его нет
    runtime.endpoints.upsert(endpoint(host="egress.yandex.net", path="/", id="egress.root",
                                      enabled=False))
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/check_updates?device_id=d1",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 200
                assert await response.json() == {"hasUpdate": False}  # builtin, не реплей
            async with client.post(url + "/gproxy/get_teasers",
                                   headers={"Host": "rpc.alice.yandex.net"}, json={}) as response:
                assert response.status == 200
                assert response.headers["Content-Type"].startswith("application/json")
                assert await response.json() == {"teasers": []}  # реплей каталога
            async with client.get(url + "/", headers={"Host": "egress.test"}) as response:
                assert response.status == 404  # выключенный — прозрачно
        assert any(r.get("kind") == "endpoint" and r["id"] == "rpc.get_teasers"
                   for r in runtime.recent)
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_endpoints_api_toggle_and_upsert(tmp_path):
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.put(url + "/emulator/api/endpoints", headers={"X-Emulator": "1"},
                                  json={"endpoint": endpoint()}) as response:
                assert response.status == 200 and (await response.json())["saved"] == "t.get"
            async with client.get(url + "/emulator/api/endpoints") as response:
                body = await response.json()
                assert body["total"] == 1 and body["enabled"] == 1
            async with client.put(url + "/emulator/api/endpoints", headers={"X-Emulator": "1"},
                                  json={"id": "t.get", "enabled": False}) as response:
                assert (await response.json())["enabled"] is False
            async with client.delete(url + "/emulator/api/endpoints?id=t.get",
                                     headers={"X-Emulator": "1"}) as response:
                assert (await response.json())["removed"] == "t.get"
    finally:
        await runner.cleanup()
        await runtime.close()


# --- экстрактор tools/endpoints_recover.py: миниатюра capture-базы ---------------


def build_capture_db(path):
    """Мини-копия схемы: events + payloads + blocks; gzip-ответ облака."""
    import zlib
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE events(id TEXT PRIMARY KEY, ts REAL, kind TEXT, host TEXT, url TEXT,"
        " method TEXT, status INTEGER, detail TEXT NOT NULL);"
        "CREATE TABLE payloads(hash TEXT PRIMARY KEY, block_id INTEGER, offset INTEGER, size INTEGER);"
        "CREATE TABLE blocks(id INTEGER PRIMARY KEY, data BLOB NOT NULL, raw_size INTEGER NOT NULL);"
        "CREATE TABLE event_payloads(event_id TEXT, role TEXT, hash TEXT,"
        " PRIMARY KEY(event_id, role));")
    c = zlib.compressobj(9, zlib.DEFLATED, 31)
    body = c.compress(b'{"config": {"device_config": {}}, "tags": ["x"]}') + c.flush()
    detail = json.dumps({"emulated": None,
                         "headers": {"response_original": [["Content-Type", "application/json"],
                                                           ["Content-Encoding", "gzip"]]}})
    db.execute("INSERT INTO events VALUES('e1',1.0,'http','quasar.yandex.net',"
               "'/get_sync_info?device_id=SN1','GET',200,?)", (detail,))
    db.execute("INSERT INTO blocks VALUES(1, ?, ?)", (body, len(body)))
    db.execute("INSERT INTO payloads VALUES('h1',1,0,?)", (len(body),))
    db.execute("INSERT INTO event_payloads VALUES('e1','response_original','h1')")
    db.commit()
    return db


def test_recover_latest_real_body(tmp_path):
    import sys
    sys.path.insert(0, str(tmp_path))
    from tools import endpoints_recover as rec

    db_path = tmp_path / "cap.sqlite3"
    build_capture_db(db_path)
    db = rec.open_db(db_path)
    found = rec.latest_real(db, "quasar.yandex.net", "GET", "/get_sync_info", want_json=True)
    assert found is not None
    _event_id, status, content_type, body = found
    assert status == 200 and content_type == "application/json"
    assert json.loads(body)["config"] == {"device_config": {}}


def test_template_render_dynamic_tokens():
    """Шаблон: строки-листья с {{токенами}} рендерятся контекстом запроса."""
    from server_emulator.endpoints import render_template, request_context
    template = {"status": "ok", "device": "{{device_id}}", "seen_at": "{{now}}",
                "nested": {"ip": "{{ client_ip }}", "count": 7, "flag": True,
                           "list": ["{{method}}", "static"]}}
    context = request_context({"device_id": "SN-1", "client_ip": "192.0.2.19",
                               "method": "GET"})
    rendered = render_template(template, context)
    assert rendered["device"] == "SN-1" and rendered["nested"]["ip"] == "192.0.2.19"
    assert rendered["nested"]["list"][0] == "GET" and rendered["nested"]["list"][1] == "static"
    assert rendered["nested"]["count"] == 7 and rendered["nested"]["flag"] is True
    assert isinstance(rendered["seen_at"], str) and rendered["seen_at"].isdigit()
    unknown = render_template("{{no_such_token}}", context)
    assert unknown == "{{no_such_token}}"  # неизвестный токен остаётся как есть


@pytest.mark.asyncio
async def test_endpoint_template_served_dynamically(tmp_path):
    """Эндпоинт с template отдаёт тело на каждом запросе с контекстом (device_id из query)."""
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.endpoints.upsert(endpoint(
        host="quasar.yandex.net", path="/tv/api/v4/settings", id="quasar.settings",
        template={"status": "ok", "for_device": "{{device_id}}", "ts": "{{now}"}))
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/tv/api/v4/settings?device_id=SN-DYN",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                body = await response.json()
                assert response.status == 200
                assert body["for_device"] == "SN-DYN" and body["status"] == "ok"
    finally:
        await runner.cleanup()
        await runtime.close()


def test_flat_file_migrates_to_category(tmp_path):
    (tmp_path / "x.json").write_text(
        json.dumps(endpoint(id="x", host="quasar.yandex.net")), encoding="utf-8")
    catalog = EndpointCatalog(tmp_path)
    assert (tmp_path / "quasar" / "x.json").exists()
    assert not (tmp_path / "x.json").exists()
    assert catalog.endpoints[0]["category"] == "quasar"


def test_upsert_changes_url_and_category(tmp_path):
    catalog = EndpointCatalog(tmp_path)
    catalog.upsert(endpoint(id="a", host="rpc.alice.yandex.net", path="/gproxy/a"))
    assert (tmp_path / "gproxy" / "a.json").exists()
    catalog.upsert(endpoint(id="a", host="rpc.alice.yandex.net", path="/gproxy/b",
                            category="custom"))
    assert (tmp_path / "custom" / "a.json").exists()
    assert not (tmp_path / "gproxy" / "a.json").exists()
    assert EndpointCatalog(tmp_path).find("rpc.alice.yandex.net",
                                          endpoint()["method"], "/gproxy/b") is not None


def test_upsert_rejects_url_clash_and_readonly(tmp_path):
    catalog = EndpointCatalog(tmp_path)
    catalog.upsert(endpoint(id="a"))
    with pytest.raises(ValueError):
        catalog.upsert(endpoint(id="b"))  # тот же URL: t.host GET /get
    with pytest.raises(ValueError):
        catalog.upsert(endpoint(id="c", path="/other", readonly=True))


def test_public_includes_system_entries(tmp_path):
    from server_emulator.endpoints import system_entries

    system = system_entries(
        {("GET", "/get_sync_info")},
        [{"id": "ota", "host": "quasar.yandex.net", "path": "/tv/api/v4/updates",
          "method": "POST", "kind": "list"}],
    )
    data = EndpointCatalog(tmp_path).public(system)
    assert any(e["path"] == "/get_sync_info" and e.get("readonly") is True
               and e.get("owner") == "quasar-http" for e in data["endpoints"])
    assert any(e["id"] == "updates.ota" and e.get("owner") == "updates"
               for e in data["endpoints"])
    assert "quasar" in data["categories"] and "updates" in data["categories"]
    assert data["total"] == 0


def test_misses_recorded_and_hidden_when_described(tmp_path):
    catalog = EndpointCatalog(tmp_path / "endpoints")
    catalog.note_miss("Quasar.Yandex.net", "GET", "/foo", "a=1", b"xyz", "text/plain")
    catalog.note_miss("Quasar.Yandex.net", "GET", "/foo", "a=1", b"xyz", "text/plain")
    data = catalog.public()
    assert len(data["misses"]) == 1
    miss = data["misses"][0]
    assert miss["host"] == "quasar.yandex.net"
    assert miss["method"] == "GET"
    assert miss["path"] == "/foo"
    assert miss["count"] == 2
    assert miss["query"] == "a=1"
    assert miss["request_size"] == 3
    assert miss["key"] == "GET quasar.yandex.net/foo"
    # файл с промахами существует
    assert (tmp_path / "endpoints_misses.json").exists()
    # новый каталог читает сохранённые промахи
    newcat = EndpointCatalog(tmp_path / "endpoints")
    assert "GET quasar.yandex.net/foo" in newcat.misses()
    # после добавления описанного эндпоинта промах скрывается
    catalog.upsert(endpoint(id="foo", host="quasar.yandex.net", method="GET", path="/foo"))
    assert catalog.public()["misses"] == []


def test_forget_miss(tmp_path):
    catalog = EndpointCatalog(tmp_path / "endpoints")
    catalog.note_miss("h.net", "GET", "/a", "", b"", "")
    catalog.note_miss("h.net", "GET", "/b", "", b"", "")
    # проверяем, что оба промаха присутствуют
    misses_before = catalog.public()["misses"]
    assert any(m["path"] == "/a" for m in misses_before)
    assert any(m["path"] == "/b" for m in misses_before)
    # удаляем один по ключу
    catalog.forget_miss("GET h.net/a")
    misses_after = catalog.public()["misses"]
    assert all(m["path"] != "/a" for m in misses_after)
    assert any(m["path"] == "/b" for m in misses_after)
    # очищаем всё
    catalog.forget_miss()
    assert catalog.public()["misses"] == []


def test_upsert_rejects_url_owned_by_other_tab(tmp_path):
    from server_emulator.endpoints import system_entries

    system = system_entries({("GET", "/get_sync_info")}, [])
    catalog = EndpointCatalog(tmp_path)
    with pytest.raises(ValueError, match="Квазар"):
        catalog.upsert(endpoint(id="s", host="quasar.yandex.net", method="GET", path="/get_sync_info"), system)
    # другой путь на том же хосте — проходит
    catalog.upsert(endpoint(id="ok", host="quasar.yandex.net", method="GET", path="/other"), system)
    assert catalog.find("quasar.yandex.net", "GET", "/other") is not None


def test_public_shows_system_route_once_at_real_owner(tmp_path):
    from server_emulator.endpoints import system_entries

    system = system_entries(
        {("GET", "/check_updates")},
        [{"id": "legacy", "host": "quasar.yandex.net", "path": "/check_updates",
          "method": "GET", "kind": "check"}],
    )
    data = EndpointCatalog(tmp_path).public(system)
    # записей с path '/check_updates' ровно одна
    check_updates_entries = [e for e in data["endpoints"] if e["path"] == "/check_updates"]
    assert len(check_updates_entries) == 1
    # её owner == 'updates'
    assert check_updates_entries[0]["owner"] == "updates"
    # ни у одной записи нет ключа 'shadowed_by'
    for e in data["endpoints"]:
        assert "shadowed_by" not in e