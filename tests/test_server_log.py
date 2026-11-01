"""TASK-068: журнал сервера «Дополнительно → Лог» — хранилище, склейка, хранение, источники, API."""
import asyncio
import json
import logging
import time

import pytest
from aiohttp import ClientSession, CookieJar, web

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from server_emulator.server_log import ServerLog, logger_source
from tests.conftest import listen
from tests.test_quasar_source import alias_upstream, registry, state_of


@pytest.fixture
def store(tmp_path):
    journal = ServerLog(tmp_path / "server_log.sqlite3")
    yield journal
    journal.db.close()


def drain(journal):
    """Все записи из очереди — в БД (синхронно, для тестов без цикла событий)."""
    if journal._queue is not None:  # noqa: SLF001
        while not journal._queue.empty():
            kind, item = journal._queue.get_nowait()  # noqa: SLF001
            if kind == "entry":
                journal._insert(item)  # noqa: SLF001
    journal.db.commit()


def test_write_read_and_filters(store):
    store.log("info", "server", "Сервер запущен")
    store.log("warning", "tls", "Домен a.b.example.test не найден в сертификате", device_sn="SN-1", host="a.b.example.test")
    store.log("error", "http", "Ошибка обработки запроса", path="/x")
    drain(store)
    everything = store.query()
    assert [item["level"] for item in everything["items"]] == ["error", "warning", "info"]
    assert sorted(everything["sources"]) == sorted({"server", "tls", "http"})
    assert store.query(level="warning,error")["items"][0]["source"] == "http"  # новые сверху
    assert [item["source"] for item in store.query(level="warning")["items"]] == ["tls"]
    assert store.query(source="tls")["items"][0]["device_sn"] == "SN-1"
    assert store.query(q="сертификат")["items"][0]["source"] == "tls"
    assert store.query(q="SN-1")["items"][0]["device_sn"] == "SN-1"
    assert store.query(q="нет такого")["items"] == []
    detail = store.query(source="tls")["items"][0]["detail"]
    assert detail == {"host": "a.b.example.test"}  # detail — JSON без секретов


def test_pagination_before(store):
    now = time.time()
    for number in range(5):
        store.log("info", "server", f"событие {number}", ts=now + number)
    drain(store)
    first = store.query(limit=2)
    assert [item["message"] for item in first["items"]] == ["событие 4", "событие 3"]  # новые сверху
    assert first["more"] is True
    second = store.query(limit=2, before=first["items"][-1]["last_ts"])
    assert [item["message"] for item in second["items"]] == ["событие 2", "событие 1"]
    last = store.query(limit=10, before=second["items"][-1]["last_ts"])
    assert [item["message"] for item in last["items"]] == ["событие 0"] and last["more"] is False


def test_glue_repeats_within_hour_new_row_after(store):
    now = time.time()
    store.log("warning", "tls", "Домен x.y.test не найден в сертификате", ts=now)
    store.log("warning", "tls", "Домен x.y.test не найден в сертификате", ts=now + 60)
    store.log("warning", "tls", "Домен x.y.test не найден в сертификате", ts=now + 120)
    drain(store)
    glued = store.query()["items"]
    assert len(glued) == 1 and glued[0]["count"] == 3
    assert glued[0]["last_ts"] == now + 120 and glued[0]["ts"] == now  # время первого случая сохранено
    store.log("warning", "tls", "Домен x.y.test не найден в сертификате", ts=now + 2 * 3600)
    drain(store)
    rows = store.query()["items"]
    assert len(rows) == 2 and rows[0]["count"] == 1  # после часа — новая строка
    # другой источник или устройство — отдельные строки даже в пределах часа
    store.log("error", "tls", "Домен x.y.test не найден в сертификате", ts=now)
    store.log("warning", "tls", "Домен x.y.test не найден в сертификате", device_sn="SN-2", ts=now)
    drain(store)
    assert len(store.query()["items"]) == 4


def test_retention_and_row_cap(tmp_path):
    store = ServerLog(tmp_path / "log.sqlite3", max_rows=2)
    now = time.time()
    store.log("info", "server", "старое событие", ts=now - 4 * 86400)
    store.log("info", "server", "свежее", ts=now)
    drain(store)
    store.rotate(now=now)
    assert [item["message"] for item in store.query()["items"]] == ["свежее"]  # старше 3 суток удалено
    store.log("info", "server", "вторая", ts=now + 1)
    store.log("info", "server", "третья", ts=now + 2)
    drain(store)
    store.rotate(now=now + 3)
    rows = store.query()["items"]
    assert [item["message"] for item in rows] == ["третья", "вторая"]  # лишнее — с самых старых
    store.db.close()


def test_logging_handler_routes_and_exceptions(store):
    from server_emulator.server_log import install_logging
    install_logging(store)
    logging.getLogger("aiohttp.server").error("Error handling request from 203.0.113.9")
    try:
        raise ValueError("соединение оборвано")
    except ValueError as exc:
        logging.getLogger("aiohttp.server").error("Error handling request", exc_info=exc)
    logging.getLogger("server_emulator.extensions.manager").warning("Расширение ext-1 не загружено: битый манифест")
    logging.getLogger("server_emulator.server_log").warning("журнал о себе — не попадает")  # рекурсия исключена
    logging.getLogger("aiohttp.access").info("GET / 200")  # INFO от access-логгера не пишется
    drain(store)
    items = {item["message"]: item for item in store.query()["items"]}
    assert any("203.0.113.9" in message for message in items)  # LineTooLong-подобные записи доходят
    exception_entry = next(item for message, item in items.items() if "соединение оборвано" in message)
    assert exception_entry["level"] == "error" and exception_entry["detail"]["logger"] == "aiohttp.server"
    assert exception_entry["message"].startswith("ValueError:")  # первая строка исключения, без трейсбека
    assert any("ext-1" in message for message in items)  # source по имени логгера
    assert all(item["source"] in ("http", "extensions") for item in store.query()["items"])
    assert len(store.query()["items"]) == 3


def test_logger_source_mapping():
    assert logger_source("aiohttp.server") == "http"
    assert logger_source("aiohttp.access") == "http"
    assert logger_source("asyncio") == "server"
    assert logger_source("server_emulator.upstream.forward") == "upstream"
    assert logger_source("server_emulator.endpoint_scripts") == "scripts"
    assert logger_source("server_emulator.scenario_engine.dialog") == "scenarios"
    assert logger_source("server_emulator.self_update.check") == "self_update"
    assert logger_source("server_emulator.runtime") == "server"


async def test_quasar_replace_broken_json_logged(tmp_path):
    """Замены regexp сломали JSON ответа get_sync_info — запись quasar с устройством."""
    from tests.test_quasar_direct_replace import rule, sync
    runtime = Runtime(tmp_path)
    registry(runtime)
    state = dict(state_of(runtime), quasar_source="direct",
                 quasar_direct_replace=[rule('"volume": 5', '"volume": 5,')])  # запятая ломает JSON
    runtime.identities.mutate({"operation": "state", "sn": "SN-Q", "state": state})
    payload = json.dumps({"status": "ok", "config": {"volume": 5}}).encode()

    async def handler(request):
        return web.Response(body=payload, content_type="application/json")

    app = web.Application()
    app.router.add_route("*", "/{p:.*}", handler)
    upstream_runner, upstream_url = await listen(app)
    alias_upstream(runtime, int(upstream_url.rsplit(":", 1)[1]))
    runner, url = await listen(create_app(runtime=runtime))
    try:
        status, _headers, _body = await sync(url)
        assert status == 200  # колонке уходит оригинал, ошибка — в журнал
        await runtime.server_log.flush()
        entries = runtime.server_log.query(source="quasar")["items"]
        assert len(entries) == 1
        assert entries[0]["level"] == "error" and entries[0]["device_sn"] == "SN-Q"
        assert "Замены" in entries[0]["message"] and "get_sync_info" in entries[0]["message"]
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()


async def test_api_filters_and_admin_only(tmp_path):
    """GET с фильтрами; DELETE и чтение — только админ при включённом входе; вход без пароля в журнале."""
    runtime = Runtime(tmp_path)
    runtime.auth.bootstrap("admin", "top-secret")
    runtime.server_log.log("info", "server", "Сервер запущен (версия 1.2.3)")
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            anonymous = await client.get(url + "/emulator/api/server_log")
            assert anonymous.status == 401  # вход включён: журнал — только для админа
            delete_anonymous = await client.delete(url + "/emulator/api/server_log")
            assert delete_anonymous.status == 401
            async with client.post(url + "/api/auth/login",
                                   json={"name": "admin", "password": "wrong"}) as response:
                assert response.status == 401  # неудачный вход — и он в журнале
            await runtime.server_log.flush()
            async with client.post(url + "/api/auth/login",
                                   json={"name": "admin", "password": "top-secret"}) as response:
                assert response.status == 200
            async with client.get(url + "/emulator/api/server_log") as response:
                body = await response.json()
                assert any("Сервер запущен" in item["message"] for item in body["items"])
                assert "server" in body["sources"]
            async with client.get(url + "/emulator/api/server_log?source=auth&level=warning") as response:
                entries = (await response.json())["items"]
            assert len(entries) == 1
            assert "admin" in entries[0]["message"]
            assert "top-secret" not in json.dumps(entries)  # пароль не попадает в журнал
            assert entries[0]["detail"].get("ip")
            async with client.get(url + "/emulator/api/server_log?q=запущен") as response:
                assert (await response.json())["items"]
            async with client.delete(url + "/emulator/api/server_log",
                                     headers={"X-Emulator": "1"}) as response:
                assert response.status == 200
            async with client.get(url + "/emulator/api/server_log") as response:
                assert (await response.json())["items"] == []
    finally:
        await runner.cleanup()
        await runtime.close()
