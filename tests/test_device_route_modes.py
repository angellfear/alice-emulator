"""TASK-044: режимы маршрута устройств — кто отвечает и что попадает в журнал.

Только локальные серверы: «настоящий Яндекс» — локальная aiohttp-заглушка, цель
подменяется значением алиаса со схемой (settings.aliases[host] = http://127.0.0.1:порт).
"""
import asyncio
import json

from aiohttp import ClientSession, WSMsgType, web

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen


async def start_upstream(calls):
    """Локальный upstream: HTTP-заглушка + WS-эхо. calls — журнал его вызовов."""

    async def http(request):
        calls.append(("http", request.rel_url.path_qs))
        return web.json_response({"from": "upstream", "path": request.rel_url.path_qs})

    async def ws(request):
        calls.append(("ws-open", request.rel_url.path_qs))
        socket = web.WebSocketResponse(autoping=False)
        await socket.prepare(request)
        async for msg in socket:
            if msg.type == WSMsgType.TEXT:
                await socket.send_str("up:" + msg.data)
        return socket

    app = web.Application()
    app.router.add_route("*", "/{p:.*}", http)
    app.router.add_route("GET", "/uni.ws", ws)
    runner, url = await listen(app)
    return runner, int(url.rsplit(":", 1)[1])


def alias_upstream(runtime, port):
    """Алиасы со схемой: весь трафик «Яндекса» идёт на локальную заглушку."""
    runtime.settings["aliases"] = {host: f"http://127.0.0.1:{port}" for host in (
        "quasar.yandex.net", "rpc.alice.yandex.net", "uniproxy.alice.yandex.net",
        "quasar.local", "rpc.local", "uniproxy.local")}


def registry(runtime, sn="SN-UP", device_id="up-id"):
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    runtime.identities.mutate({"operation": "device", "sn": sn, "user_id": "u", "device_ids": [device_id]})


DEVICE = {"Host": "quasar.yandex.net", "X-YaQuasarDeviceId": "up-id"}


async def test_http_modes_answer_and_journal(tmp_path):
    runtime = Runtime(tmp_path)
    registry(runtime)
    calls = []
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            # emulate: отвечает эмулятор (честный 404), наверх ничего не уходит
            runtime.settings["default_route_mode"] = "emulate"
            async with client.get(url + "/unknown/path", headers=DEVICE) as response:
                assert response.status == 404
                assert (await response.json())["error"] == "emulator_handler_not_found"
            assert calls == [] and runtime.upstream.journal.list()["total"] == 0

            # proxy: ответ upstream, журнала нет
            runtime.settings["default_route_mode"] = "proxy"
            async with client.get(url + "/unknown/path", headers=DEVICE) as response:
                assert (await response.json())["from"] == "upstream"
            assert len(calls) == 1 and runtime.upstream.journal.list()["total"] == 0

            # record: ответ upstream + запись журнала с устройством
            runtime.settings["default_route_mode"] = "record"
            async with client.get(url + "/unknown/path?q=1", headers=DEVICE) as response:
                assert (await response.json())["from"] == "upstream"
            await runtime.upstream.journal.flush()
            items = runtime.upstream.journal.list(device="SN-UP")["items"]
            assert len(items) == 1
            assert items[0]["source"] == "upstream" and items[0]["status"] == 200
            assert items[0]["path"] == "/unknown/path?q=1"

            # неопознанный запрос тоже уходит по default и не регистрирует SN
            async with client.get(url + "/unknown/path", headers={"Host": "quasar.yandex.net"}) as response:
                assert (await response.json())["from"] == "upstream"
            await runtime.upstream.journal.flush()
            items = runtime.upstream.journal.list()["items"]
            assert items[0]["device_sn"] is None
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()


async def test_hybrid_handled_stays_local_miss_goes_upstream(tmp_path):
    runtime = Runtime(tmp_path)
    registry(runtime, "SN-H", "h-id")
    headers = {"Host": "quasar.yandex.net", "X-YaQuasarDeviceId": "h-id"}
    calls = []
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    runtime.settings["default_route_mode"] = "hybrid"
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            # есть обработчик (builtin) — эмулятор, наверх не уходит
            async with client.get(url + "/check_updates", headers=headers) as response:
                assert await response.json() == {"hasUpdate": False}
            assert calls == []

            # промах — наверх с журналом и отметкой в «Без ответа»
            async with client.get(url + "/no/handler", headers=headers) as response:
                assert (await response.json())["from"] == "upstream"
            await runtime.upstream.journal.flush()
            assert runtime.upstream.journal.list()["total"] == 1
            async with client.get(url + "/emulator/api/misses", headers={"X-Emulator": "1"}) as response:
                misses = (await response.json())["http"]
            assert any(m.get("forwarded") == "upstream" for m in misses)

            # upstream недоступен — 502 колонке, промах остаётся с пометкой об ошибке
            runtime.settings["aliases"]["quasar.yandex.net"] = "http://127.0.0.1:1"
            async with client.get(url + "/no/handler", headers=headers) as response:
                assert response.status == 502
                assert (await response.json())["error"] == "upstream_unavailable"
            async with client.get(url + "/emulator/api/misses", headers={"X-Emulator": "1"}) as response:
                misses = (await response.json())["http"]
            assert any(str(m.get("forwarded", "")).startswith("error") for m in misses)
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()


async def test_ws_modes_bridge_or_local(tmp_path):
    runtime = Runtime(tmp_path)
    registry(runtime, "SN-W", "w-id")
    headers = {"Host": "uniproxy.local", "x-uprx-device-id": "w-id"}
    calls = []
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            # emulate/hybrid: WS целиком эмулятор — upstream соединения не видит
            for mode in ("emulate", "hybrid"):
                runtime.settings["default_route_mode"] = mode
                async with client.ws_connect(url + "/uni.ws", headers=headers) as ws:
                    await ws.send_str("hello")
                    await asyncio.sleep(0.5)
            assert not [kind for kind, _ in calls if kind == "ws-open"]
            assert runtime.upstream.journal.list()["total"] == 0

            # proxy: мост без журнала; record: мост с журналом (open + close)
            for mode in ("proxy", "record"):
                runtime.settings["default_route_mode"] = mode
                async with client.ws_connect(url + "/uni.ws", headers=headers) as ws:
                    await ws.send_str("привет")
                    assert (await ws.receive()).data == "up:привет"
                await runtime.upstream.journal.flush()
                items = [i for i in runtime.upstream.journal.list()["items"] if i["kind"] == "ws"]
                if mode == "proxy":
                    assert items == []
                else:
                    assert items and all(i["device_sn"] == "SN-W" for i in items)
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()


async def test_device_route_mode_overrides_default(tmp_path):
    runtime = Runtime(tmp_path)
    registry(runtime, "SN-R", "r-id")
    runtime.identities.mutate({"operation": "route_mode", "sn": "SN-R", "mode": "emulate"})
    calls = []
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    runtime.settings["default_route_mode"] = "record"
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            headers = {"Host": "quasar.yandex.net", "X-YaQuasarDeviceId": "r-id"}
            async with client.get(url + "/no/handler", headers=headers) as response:
                assert response.status == 404  # свой emulate сильнее default record
            assert calls == []
            # смена маршрута через API закрывает соединение только этого устройства
            closed = []

            class FakeWS:
                async def close(self, **kwargs):
                    closed.append(kwargs)
            runtime.sessions["a"] = {"device_sn": "SN-R", "ws": FakeWS()}
            runtime.sessions["b"] = {"device_id": "up-id", "ws": FakeWS()}
            async with client.put(url + "/emulator/api/route_mode",
                                  json={"sn": "SN-R", "mode": "record"}, headers={"X-Emulator": "1"}) as response:
                result = await response.json()
            assert result == {"sn": "SN-R", "mode": "record", "disconnected": 1}
            assert len(closed) == 1 and closed[0]["code"] == 1012
            async with client.get(url + "/no/handler", headers=headers) as response:
                assert (await response.json())["from"] == "upstream"
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()
