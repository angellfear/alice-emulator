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


@pytest.mark.asyncio
async def test_builtin_silencers_and_dynamic_hosts(tmp_path):
    """Таблица образцов: push_events_batch — пустой protobuf до каталога (захваченный
    с промаха 400-эндпоинт не перекрывает), clck-protobuf/JSON по длине входа,
    appmetrica /report, scbh 204 + X-Start-Time, startup с одноразовыми hash/uuid."""
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    # захваченный с промаха эндпоинт с реальным 400 — как в живом state владельца
    runtime.endpoints.upsert(endpoint(host="rpc.alice.yandex.net", path="/gproxy/push_events_batch",
                                      id="rpc.push_events_batch", method="POST", status=400,
                                      source="miss",
                                      body_base64=base64.b64encode(b'{"Method":"PushEventBatch"}').decode()))
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/gproxy/push_events_batch",
                                   headers={"Host": "rpc.alice.yandex.net"}) as response:
                assert response.status == 200  # не 400 из захваченного эндпоинта
                assert await response.read() == b""
                assert response.headers["Content-Type"] == "application/x-protobuf"
            async with client.post(url + "/quasar_metrics/write/batch",
                                   headers={"Host": "clck.yandex.net"}) as response:
                assert response.status == 200
                assert await response.read() == b"\x0a\x02\x4f\x4b"
                assert response.headers["Content-Type"] == "application/x-protobuf"
            for path in ("/quasar_metrics/write/json/batch", "/smart_display_errors/write/json/batch",
                         "/quasar/write/json/batch"):
                async with client.post(url + path, headers={"Host": "clck.yandex.net"},
                                       data=json.dumps([{}, {}, {}])) as response:
                    assert response.status == 200
                    assert await response.json() == [{"ResponseStatus": "OK"}] * 3
                async with client.post(url + path, headers={"Host": "clck.yandex.net"},
                                       data=json.dumps({"not": "array"})) as response:
                    assert response.status == 500
                async with client.post(url + path, headers={"Host": "clck.yandex.net"},
                                       data=b"garbage") as response:
                    assert response.status == 500
            async with client.post(url + "/report",
                                   headers={"Host": "report.appmetrica.yandex.net"}) as response:
                assert response.status == 200
                assert await response.json() == {"status": "accepted"}
            stamps = []
            for _ in range(2):
                async with client.get(url + "/generate_204",
                                      headers={"Host": "scbh.yandex.net"}) as response:
                    assert response.status == 204
                    stamp = response.headers["X-Start-Time"]
                    assert stamp.isdigit() and len(stamp) == 16  # микросекунды строкой
                    stamps.append(int(stamp))
            assert stamps[1] >= stamps[0]  # монотонен
            async with client.get(url + "/analytics/startup",
                                  headers={"Host": "startup.mobile.yandex.net"}) as response:
                assert response.status == 200
                body = await response.json()
            assert len(body["device_id"]["hash"]) == 10 and body["device_id"]["hash"].isdigit()
            assert len(body["uuid"]["value"]) == 32
            hosts = body["query_hosts"]["list"]
            assert set(hosts) == {"apple_tracking", "check_updates", "diagnostic", "location",
                                  "mediascope", "redirect", "report", "ssl_pinning", "startup"}
            assert hosts["startup"]["urls"] == ["https://startup.mobile.yandex.net"]
            async with ClientSession() as client2:
                async with client2.get(url + "/analytics/startup",
                                       headers={"Host": "startup.mobile.yandex.net"}) as response:
                    again = await response.json()
            assert again["uuid"]["value"] != body["uuid"]["value"]  # одноразовые значения
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_register_and_ota_v3(tmp_path):
    """/register (hasCriticalUpdate из политик OTA), /tv/api/v3/updates — пустые коллекции,
    /tv/api/v3/ota — {} при deny, свой объект при custom, прошивка каталога при allow."""
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({"operation": "user", "id": "u1", "name": "U"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-1", "user_id": "u1",
                               "device_ids": ["id-1"], "profile": {"platform": "chiron"}})
    runner, url = await listen(create_app(runtime=runtime))
    FIRMWARE = {"url": "https://ota.s3.yandex.net/ota/chiron/firmware.bin",
                "version_name": "5.1.9", "update_id": "upd-1", "md5_hash": "abc123",
                "size": 12345, "priority": "FORCED"}
    try:
        async with ClientSession() as client:
            async with client.post(url + "/register?platform=chiron&firmware_version=5.1.8",
                                   headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 200
                assert await response.json() == {"hasCriticalUpdate": False, "status": "ok"}
            async with client.post(url + "/tv/api/v3/updates",
                                   headers={"Host": "quasar.yandex.net"}) as response:
                assert await response.json() == {"bundles": [], "updates": []}
            async with client.get(url + "/tv/api/v3/ota",
                                  headers={"Host": "quasar.yandex.net",
                                           "x-yaquasarplatform": "chiron"}) as response:
                assert response.status == 200
                assert await response.json() == {}  # свежая база: политика deny, каталог пуст
        value = runtime.updates.settings()
        value["policy"] = "custom"
        value["custom"] = {"updates": [dict(FIRMWARE)]}
        runtime.updates.save(value)
        async with ClientSession() as client:
            async with client.get(url + "/tv/api/v3/ota",
                                  headers={"Host": "quasar.yandex.net",
                                           "x-yaquasarplatform": "chiron"}) as response:
                assert await response.json() == FIRMWARE  # свой ответ целиком
            async with client.post(url + "/register?platform=chiron",
                                   headers={"Host": "quasar.yandex.net"}) as response:
                assert (await response.json())["hasCriticalUpdate"] is True  # custom с записями
        value = runtime.updates.settings()
        value["policy"] = "allow"
        value["custom"] = {"updates": []}
        runtime.updates.save(value)
        runtime.updates.record({"id": "att-1", "ts": 0, "endpoint": "ota", "device_sn": None,
                                "user_id": None, "device_id": None, "policy": "allow",
                                "status": 200, "error": None, "ip": None},
                               {"updates": [dict(FIRMWARE)]})
        async with ClientSession() as client:
            async with client.get(url + "/tv/api/v3/ota",
                                  headers={"Host": "quasar.yandex.net",
                                           "x-yaquasarplatform": "chiron"}) as response:
                body = await response.json()
            assert body["url"] == FIRMWARE["url"]  # объект из каталога прошивок
            assert body["version"] == "5.1.9" and body["md5_hash"] == "abc123"
            assert body["priority"] == "FORCED" and body["size"] == 12345
            async with client.get(url + "/tv/api/v3/ota",
                                  headers={"Host": "quasar.yandex.net",
                                           "x-yaquasarplatform": "yandexstation"}) as response:
                assert await response.json() == {}  # для другой платформы записи нет
        value = runtime.updates.settings()
        value["policy"] = "deny"
        runtime.updates.save(value)
        async with ClientSession() as client:
            async with client.get(url + "/tv/api/v3/ota",
                                  headers={"Host": "quasar.yandex.net",
                                           "x-yaquasarplatform": "chiron"}) as response:
                assert await response.json() == {}  # deny глушит и каталог
    finally:
        await runner.cleanup()
        await runtime.close()


def test_catalog_skips_miss_4xx_but_keeps_it_visible(tmp_path):
    """Запись из промаха с реальным отказом (source miss, статус ≥400) — не образец ответа:
    find её пропускает, каталог UI по-прежнему показывает."""
    catalog = EndpointCatalog(tmp_path / "endpoints")
    catalog.upsert(endpoint(id="miss400", host="rpc.alice.yandex.net", method="POST",
                            path="/gproxy/push_events_batch", status=400, source="miss",
                            body_base64=base64.b64encode(b'{"HttpCode":400}').decode()))
    assert catalog.find("rpc.alice.yandex.net", "POST", "/gproxy/push_events_batch") is None
    listed = catalog.public()["endpoints"]
    assert "miss400" in [e["id"] for e in listed]  # в UI запись видна
    # та же запись со статусом 200 (реальный успешный образец с промаха) — образец
    catalog.upsert(endpoint(id="miss200", host="rpc.alice.yandex.net", method="POST",
                            path="/gproxy/other", status=200, source="miss",
                            body_base64=base64.b64encode(b"{}").decode()))
    assert catalog.find("rpc.alice.yandex.net", "POST", "/gproxy/other") is not None


@pytest.mark.asyncio
async def test_user_endpoint_wins_over_builtin(tmp_path):
    """Пользовательская (не miss) запись перекрывает builtin — порядок прежний."""
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.endpoints.upsert(endpoint(host="rpc.alice.yandex.net", path="/gproxy/push_events_batch",
                                      id="rpc.push_user", method="POST", status=418, source="manual",
                                      body_base64=base64.b64encode(b'{"custom":true}').decode()))
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/gproxy/push_events_batch",
                                   headers={"Host": "rpc.alice.yandex.net"}) as response:
                assert response.status == 418
                assert await response.json() == {"custom": True}
    finally:
        await runner.cleanup()
        await runtime.close()


def test_recover_skips_non_2xx_answers(tmp_path):
    """Автозаполнение из промахов не сохраняет отказ наверху: последний реальный ответ
    со статусом ≥400 — не образец (latest_real вернёт None)."""
    import sys
    sys.path.insert(0, str(tmp_path))
    from tools import endpoints_recover as rec

    db_path = tmp_path / "cap400.sqlite3"
    db = sqlite3.connect(db_path)
    db.executescript(
        "CREATE TABLE events(id TEXT PRIMARY KEY, ts REAL, kind TEXT, host TEXT, url TEXT,"
        " method TEXT, status INTEGER, detail TEXT NOT NULL);"
        "CREATE TABLE payloads(hash TEXT PRIMARY KEY, block_id INTEGER, offset INTEGER, size INTEGER);"
        "CREATE TABLE blocks(id INTEGER PRIMARY KEY, data BLOB NOT NULL, raw_size INTEGER NOT NULL);"
        "CREATE TABLE event_payloads(event_id TEXT, role TEXT, hash TEXT,"
        " PRIMARY KEY(event_id, role));")
    detail = json.dumps({"emulated": None, "headers": {"response_original": [["Content-Type", "application/json"]]}})
    db.execute("INSERT INTO events VALUES('rej',1.0,'http','rpc.alice.yandex.net',"
               "'/gproxy/push_events_batch','POST',400,?)", (detail,))
    db.commit()
    db = rec.open_db(db_path)
    assert rec.latest_real(db, "rpc.alice.yandex.net", "POST", "/gproxy/push_events_batch",
                           want_json=True) is None


@pytest.mark.asyncio
async def test_local_domain_serves_gproxy_reports_and_startup(tmp_path):
    """Локальный домен: /gproxy/* — как rpc.alice.yandex.net (arpc_client.server_url из
    get_sync_info), /report — как report.appmetrica; startup отдаёт отчёты на http://<домен>."""
    from server_emulator import server_config
    from server_emulator.server import create_app
    from tests.conftest import listen
    server_config.save(tmp_path, server_config.validate(tmp_path, {"ssl": False, "local_domain": "emu.home"}))
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/gproxy/push_events_batch", headers={"Host": "emu.home"}) as response:
                assert response.status == 200
            async with client.post(url + "/report", headers={"Host": "emu.home:80"}) as response:
                assert response.status == 200 and await response.json() == {"status": "accepted"}
            async with client.get(url + "/analytics/startup",
                                  headers={"Host": "startup.mobile.yandex.net"}) as response:
                hosts = (await response.json())["query_hosts"]["list"]
            assert hosts["report"]["urls"] == hosts["diagnostic"]["urls"] == ["http://emu.home"]
            assert hosts["startup"]["urls"] == ["https://startup.mobile.yandex.net"]
    finally:
        await runner.cleanup()
        await runtime.close()
