"""TASK-043: проход наверх (upstream) и журнал трафика. Только локальные серверы — Яндекс не трогается.

Резолвер в HTTP/WS-тестах подменён (всё → 127.0.0.1): цель задаётся именем upstream.test,
чтобы коннектор реально прошёл через резолвер. Цикл жизни журнала (ротация, лимиты тел
и кадров) проверяется на собственном SQLite в tmp_path.
"""
import asyncio
import json
import os
import socket
from pathlib import Path

import pytest
from aiohttp import ClientSession, WSMsgType, web
from aiohttp.resolver import ResolveResult

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from server_emulator.upstream import UpstreamError
from server_emulator.upstream.journal import TrafficJournal
from server_emulator.upstream.resolver import LoopError, UpstreamResolver, local_addresses
from tests.conftest import listen


class StubResolver:
    """Подменённый резолвер: любой хост → 127.0.0.1 (локальный upstream-сервер)."""

    async def resolve(self, hostname, port=0, family=socket.AF_INET):
        return [ResolveResult(hostname=hostname, host="127.0.0.1", port=port,
                              family=socket.AF_INET, proto=socket.IPPROTO_TCP, flags=0)]

    async def close(self):
        pass


async def start_upstream(app):
    """Локальный upstream: (runner, порт)."""
    runner, url = await listen(app)
    return runner, int(url.rsplit(":", 1)[1])


async def test_http_forward_returns_upstream_response_and_journals(tmp_path):
    runtime = Runtime(tmp_path)
    journal = runtime.upstream.journal
    seen = []

    async def handler(request):
        seen.append(request.rel_url.path_qs)
        seen.append(request.headers.get("Authorization"))
        return web.json_response({"ok": True, "path": request.rel_url.path_qs},
                                 headers={"X-Upstream": "1", "Set-Cookie": "sid=secret-cookie"})

    upstream = web.Application()
    upstream.router.add_route("*", "/{p:.*}", handler)
    runner, port = await start_upstream(upstream)
    runtime.upstream.forwarder.resolver = StubResolver()
    try:
        status, headers, payload = await runtime.upstream.forwarder.http(
            "GET", f"http://upstream.test:{port}/api/data?q=1",
            [("Host", "upstream.test"), ("X-Device", "1"), ("Authorization", "OAuth device-secret")], b"",
            host="upstream.test", path="/api/data?q=1", device_sn="SN-T")
        assert status == 200 and json.loads(payload)["ok"] is True
        assert any(k.lower() == "x-upstream" for k, _v in headers)
        assert seen == ["/api/data?q=1", "OAuth device-secret"]  # запрос дошёл до upstream с настоящим токеном
        await journal.flush()
        items = journal.list(host="upstream.test")["items"]
        assert len(items) == 1
        record = items[0]
        assert record["source"] == "upstream" and record["status"] == 200
        assert record["kind"] == "http" and record["device_sn"] == "SN-T"
        detail = journal.item(record["id"])
        assert json.loads(detail["response_body"])["path"] == "/api/data?q=1"
        assert any(k == "X-Device" for k, _v in detail["request_headers"])
        # секреты в журнале замаскированы, в файле их нет
        assert ["Authorization", "OAuth ***"] in [list(h) for h in detail["request_headers"]]
        assert ["Set-Cookie", "***"] in [list(h) for h in detail["response_headers"]]
        raw = (tmp_path / "traffic.sqlite3").read_bytes() + (tmp_path / "traffic.sqlite3-wal").read_bytes()
        assert b"device-secret" not in raw and b"secret-cookie" not in raw
    finally:
        await runner.cleanup()
        await runtime.upstream.close()


async def test_serve_journals_device_via_request(tmp_path):
    """serve() на реальном запросе: ответ upstream и запись журнала с устройством."""
    runtime = Runtime(tmp_path)

    async def handler(request):
        return web.Response(body=b"upstream-body")

    upstream = web.Application()
    upstream.router.add_route("*", "/{p:.*}", handler)
    upstream_runner, port = await start_upstream(upstream)
    runtime.upstream.forwarder.resolver = StubResolver()

    async def proxy(request):  # точка вызова диспетчера: route() дал цель — serve
        return await runtime.upstream.serve(request, f"http://upstream.test:{port}{request.rel_url.path_qs}",
                                            host="upstream.test")

    app = web.Application()
    app.router.add_route("*", "/{p:.*}", proxy)
    runner, url = await listen(app)
    try:
        # решение о пересылке с TASK-044 — dispatcher.route_mode_for; покрывается
        # tests/test_device_route_modes.py, здесь — механизм serve() и журнал.
        async with ClientSession() as client:
            async with client.get(url + "/hello?x=1", headers={"X-YaQuasarDeviceId": "dev-t"}) as response:
                assert response.status == 200 and await response.read() == b"upstream-body"
        await runtime.upstream.journal.flush()
        items = runtime.upstream.journal.list()["items"]
        assert len(items) == 1 and items[0]["source"] == "upstream"
        assert items[0]["device_id"] == "dev-t"  # DeviceId взят из заголовка соединения
        assert items[0]["path"] == "/hello?x=1"
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()


async def test_ws_bridge_forwards_frames_both_ways_and_closes(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({"operation": "device", "sn": "SN-WS", "device_ids": ["ws-id"]})
    received = []

    async def ws_handler(request):
        ws = web.WebSocketResponse(autoping=False)
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                received.append(msg.data)
                await ws.send_str("echo:" + msg.data)
            elif msg.type == WSMsgType.BINARY:
                received.append(msg.data)
                await ws.send_bytes(msg.data)
        return ws

    upstream = web.Application()
    upstream.router.add_route("GET", "/uni.ws", ws_handler)
    upstream_runner, port = await start_upstream(upstream)
    runtime.upstream.forwarder.resolver = StubResolver()

    async def bridge(request):  # колонка ↔ настоящий uniproxy
        return await runtime.upstream.serve(request, f"ws://uniproxy.test:{port}/uni.ws",
                                            host="uniproxy.alice.yandex.net")

    app = web.Application()
    app.router.add_route("GET", "/uni.ws", bridge)
    runner, url = await listen(app)
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url + "/uni.ws", headers={"x-uprx-device-id": "ws-id"}) as ws:
                await ws.send_str("привет")
                assert (await asyncio.wait_for(ws.receive(), 3)).data == "echo:привет"
                await ws.send_bytes(b"")
                assert (await asyncio.wait_for(ws.receive(), 3)).data == b""
        assert "привет" in received and b"" in received  # кадры дошли наверх
        items = []
        for _ in range(50):  # close-запись пишется при завершении обработчика моста
            await runtime.upstream.journal.flush()
            items = runtime.upstream.journal.list(host="uniproxy.alice.yandex.net")["items"]
            if {"open", "close"} <= {item["event"] for item in items}:
                break
            await asyncio.sleep(0.05)
        assert {"open", "close"} <= {item["event"] for item in items}
        unknown = [item for item in items if item["event"] == "unknown"]
        assert {item["direction"] for item in unknown} == {"client_to_server", "server_to_client"}
        # текстовый кадр — запись «неизвестный» как есть; звук (бинарный без AAPI) данными не пишется
        cards = [runtime.upstream.journal.item(item["id"]) for item in unknown]
        assert any(card["request_body"] == "привет".encode() for card in cards)
        assert not any(b"" in (card["request_body"] or b"") + (card["response_body"] or b"") for card in cards)
        close = runtime.upstream.journal.item(next(i["id"] for i in items if i["event"] == "close"))
        assert (close["audio_in_frames"], close["audio_in_bytes"]) == (1, 2)
        assert (close["audio_out_frames"], close["audio_out_bytes"]) == (1, 2)
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()


async def test_resolver_loop_protection_and_pins():
    """Петля: адрес самого сервера в пересылку не идёт; пин на чужой адрес работает."""
    own = "127.0.0.1"
    assert own in local_addresses()  # loopback всегда свой
    resolver = UpstreamResolver(config={"upstream_ip": {"loopback.test": own, "pinned.test": "192.0.2.10"}})
    try:
        with pytest.raises(LoopError):
            await resolver.resolve("loopback.test", 443)
        result = await resolver.resolve("pinned.test", 443)
        assert result[0]["host"] == "192.0.2.10" and result[0]["port"] == 443
    finally:
        await resolver.close()


async def test_forward_to_self_journals_error_and_raises(tmp_path):
    """Резолв в собственный адрес: ошибки понятный текст, запроса нет, запись 502 в журнале."""
    runtime = Runtime(tmp_path)
    journal = runtime.upstream.journal
    bait = []

    async def handler(request):
        bait.append(request.path)
        return web.Response(body=b"loop!")

    app = web.Application()
    app.router.add_route("*", "/{p:.*}", handler)
    runner, _url = await listen(app)
    runtime.settings["upstream"]["upstream_ip"]["loop.test"] = "127.0.0.1"
    try:
        with pytest.raises(UpstreamError):
            await runtime.upstream.forwarder.http("GET", "http://loop.test/x", [], b"",
                                                  host="loop.test", path="/x")
        assert not bait  # запрос не вернулся в самого себя
        await journal.flush()
        items = journal.list()["items"]
        assert len(items) == 1 and items[0]["status"] == 502 and items[0]["error"]
    finally:
        await runner.cleanup()
        await runtime.upstream.close()


async def test_journal_body_truncation(tmp_path):
    journal = TrafficJournal(Path(tmp_path) / "traffic.sqlite3", config={"max_body_kb": 1})
    try:
        journal.write(kind="http", direction="exchange", host="b.test", method="POST", path="/upload",
                      status=200, source="upstream", request_size=99999, response_size=99999,
                      request_body=b"q" * 99999, response_body=b"r" * 99999)
        await journal.flush()
        record = journal.item(journal.list()["items"][0]["id"])
        assert record["body_truncated"] == 1  # усечено с отметкой
        assert len(record["request_body"]) == 1024 and len(record["response_body"]) == 1024
        assert record["request_size"] == 99999  # размеры остались настоящими
    finally:
        await journal.close()


async def test_journal_scrubs_headers_written_before_masking(tmp_path):
    import sqlite3
    path = Path(tmp_path) / "traffic.sqlite3"
    TrafficJournal(path).db.close()
    db = sqlite3.connect(path)
    with db:
        db.execute("PRAGMA user_version=0")  # журнал прежней версии — с открытыми значениями
        db.execute("INSERT INTO exchanges(ts, kind, host, request_headers) VALUES(1, 'http', 'old.test', ?)",
                   (json.dumps([["Authorization", "OAuth old-secret"], ["X-Device", "1"]]),))
    db.close()
    journal = TrafficJournal(path)
    try:
        record = journal.item(journal.list()["items"][0]["id"])
        assert [list(h) for h in record["request_headers"]] == [["Authorization", "OAuth ***"], ["X-Device", "1"]]
    finally:
        await journal.close()


async def test_journal_rotation_by_age_and_size(tmp_path):
    age = TrafficJournal(Path(tmp_path) / "age.sqlite3", config={"max_age_days": 14})
    size = TrafficJournal(Path(tmp_path) / "size.sqlite3", config={"max_size_mb": 0.3, "max_age_days": 0})
    try:
        for index in range(5):
            age.write(kind="http", direction="exchange", host="a.test", method="GET", path=f"/{index}",
                      status=200, source="upstream")
        await age.flush()
        with age.db:
            age.db.execute("UPDATE exchanges SET ts=ts-30*86400")  # записи старше лимита
        age.rotate()
        assert age.list()["total"] == 0  # ротация по сроку

        for _index in range(12):
            size.write(kind="http", direction="exchange", host="s.test", method="GET", path=f"/x{_index}",
                       status=200, source="upstream",
                       request_body=os.urandom(48 * 1024), response_body=os.urandom(48 * 1024))
        await size.flush()
        assert size.list()["total"] == 12
        size.rotate()
        rotated = size.list()
        assert 0 < rotated["total"] < 12  # старые удалены, новые остались
        assert rotated["items"][0]["path"].startswith("/x")
    finally:
        await age.close()
        await size.close()


async def test_traffic_api_list_card_clear(tmp_path):
    runtime = Runtime(tmp_path)
    journal = runtime.upstream.journal
    journal.write(kind="http", direction="exchange", host="a.test", method="GET", path="/",
                  status=200, source="upstream", response_body=b'{"x":1}')
    await journal.flush()
    app = create_app(runtime=runtime)
    runner, url = await listen(app)
    try:
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/traffic") as response:
                data = await response.json()
                assert response.status == 200 and data["total"] == 1
            item = data["items"][0]
            async with client.get(url + "/emulator/api/traffic?host=a.test") as response:
                assert (await response.json())["total"] == 1  # фильтр по хосту
            async with client.get(url + f"/emulator/api/traffic?item={item['id']}") as response:
                card = await response.json()
                assert card["response_body"]["encoding"] == "json"
                assert card["request_body"] is None
            async with client.get(url + f"/emulator/api/traffic?export={item['id']}") as response:
                assert response.status == 200
                assert "attachment" in response.headers["Content-Disposition"]
            async with client.delete(url + "/emulator/api/traffic", headers={"X-Emulator": "1"}) as response:
                assert response.status == 200
            assert journal.list()["total"] == 0
    finally:
        await runner.cleanup()
