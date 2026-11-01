"""TASK-044, TASK-054: режимы маршрута устройств (emulate | hybrid | proxy) — кто отвечает.
Журнал пишется по опции «Запись» устройства (подробно — tests/test_device_record.py).

Только локальные серверы: «настоящий Яндекс» — локальная aiohttp-заглушка, цель
подменяется значением алиаса со схемой (settings.aliases[host] = http://127.0.0.1:порт).
"""
import asyncio
import json
import time

from aiohttp import ClientSession, WSMsgType, web

from server_emulator import voice, ws_close
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
        "quasar.yandex.net", "rpc.alice.yandex.net", "uniproxy.alice.yandex.net")}


def registry(runtime, sn="SN-UP", device_id="up-id"):
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    runtime.identities.mutate({"operation": "device", "sn": sn, "user_id": "u", "device_ids": [device_id]})


def set_route(runtime, sn, mode):
    runtime.identities.mutate({"operation": "route_mode", "sn": sn, "mode": mode})


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
            set_route(runtime, "SN-UP", "emulate")
            async with client.get(url + "/unknown/path", headers=DEVICE) as response:
                assert response.status == 404
                assert (await response.json())["error"] == "emulator_handler_not_found"
            assert calls == [] and runtime.upstream.journal.list()["total"] == 0

            # proxy: ответ upstream, запись выключена — журнала нет
            set_route(runtime, "SN-UP", "proxy")
            async with client.get(url + "/unknown/path", headers=DEVICE) as response:
                assert (await response.json())["from"] == "upstream"
            assert len(calls) == 1 and runtime.upstream.journal.list()["total"] == 0

            # proxy + запись «Включено»: ответ upstream + запись журнала с устройством
            runtime.identities.mutate({"operation": "record", "sn": "SN-UP", "mode": "on"})
            async with client.get(url + "/unknown/path?q=1", headers=DEVICE) as response:
                assert (await response.json())["from"] == "upstream"
            await runtime.upstream.journal.flush()
            items = runtime.upstream.journal.list(device="SN-UP")["items"]
            assert len(items) == 1
            assert items[0]["source"] == "upstream" and items[0]["status"] == 200
            assert items[0]["path"] == "/unknown/path?q=1"

            # неопознанный запрос — всегда эмуляция (маршрута по умолчанию нет): наверх не уходит
            async with client.get(url + "/unknown/path", headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 404
            assert len(calls) == 2
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
    set_route(runtime, "SN-H", "hybrid")
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            # есть обработчик (builtin) — эмулятор, наверх не уходит
            async with client.get(url + "/check_updates", headers=headers) as response:
                assert await response.json() == {"hasUpdate": False}
            assert calls == []

            # промах — наверх с отметкой в «Без ответа»; запись выключена — журнала нет
            async with client.get(url + "/no/handler", headers=headers) as response:
                assert (await response.json())["from"] == "upstream"
            await runtime.upstream.journal.flush()
            assert runtime.upstream.journal.list()["total"] == 0
            # «Нет обработчиков»: промах, ушедший наверх, пишется
            runtime.identities.mutate({"operation": "record", "sn": "SN-H", "mode": "misses"})
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
    headers = {"Host": "uniproxy.alice.yandex.net", "x-uprx-device-id": "w-id"}
    calls = []
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            # emulate/hybrid: WS целиком эмулятор — upstream соединения не видит; запись выключена
            for mode in ("emulate", "hybrid"):
                set_route(runtime, "SN-W", mode)
                async with client.ws_connect(url + "/uni.ws", headers=headers) as ws:
                    await ws.send_str("hello")
                    await asyncio.sleep(0.5)
            assert not [kind for kind, _ in calls if kind == "ws-open"]
            assert runtime.upstream.journal.list()["total"] == 0

            # proxy: мост; запись выключена — журнала нет, «Включено» — open + close + неизвестный кадр
            set_route(runtime, "SN-W", "proxy")
            for record in ("off", "on"):
                runtime.identities.mutate({"operation": "record", "sn": "SN-W", "mode": record})
                async with client.ws_connect(url + "/uni.ws", headers=headers) as ws:
                    await ws.send_str("привет")
                    assert (await ws.receive()).data == "up:привет"
                items = []
                for _ in range(50):  # close-запись пишется при завершении обработчика моста
                    await runtime.upstream.journal.flush()
                    items = [i for i in runtime.upstream.journal.list()["items"] if i["kind"] == "ws"]
                    if record == "off" or {"open", "close"} <= {i["event"] for i in items}:
                        break
                    await asyncio.sleep(0.05)
                if record == "off":
                    assert items == []
                else:
                    assert {"open", "close", "unknown"} <= {i["event"] for i in items}
                    assert all(i["device_sn"] == "SN-W" for i in items)
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()


async def test_device_route_mode_applies_and_reroutes(tmp_path):
    runtime = Runtime(tmp_path)
    registry(runtime, "SN-R", "r-id")
    runtime.identities.mutate({"operation": "route_mode", "sn": "SN-R", "mode": "emulate"})
    calls = []
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            headers = {"Host": "quasar.yandex.net", "X-YaQuasarDeviceId": "r-id"}
            async with client.get(url + "/no/handler", headers=headers) as response:
                assert response.status == 404  # emulate: наверх не уходит
            assert calls == []
            # смена emulate → proxy через API закрывает соединение только этого устройства
            closed = []

            class FakeWS:
                async def close(self, **kwargs):
                    closed.append(kwargs)
            runtime.sessions["a"] = {"device_sn": "SN-R", "ws": FakeWS()}
            runtime.sessions["b"] = {"device_id": "up-id", "ws": FakeWS()}
            async with client.put(url + "/emulator/api/route_mode",
                                  json={"sn": "SN-R", "mode": "proxy"}, headers={"X-Emulator": "1"}) as response:
                result = await response.json()
            assert result == {"sn": "SN-R", "mode": "proxy", "disconnected": 1}
            await runtime.closer.drain()  # закрытие идёт в фоне
            assert len(closed) == 1 and closed[0]["code"] == 1012
            async with client.get(url + "/no/handler", headers=headers) as response:
                assert (await response.json())["from"] == "upstream"
            # «Запись» — не маршрут: PUT route_mode record → 400, прежний маршрут не меняется
            async with client.put(url + "/emulator/api/route_mode",
                                  json={"sn": "SN-R", "mode": "record"}, headers={"X-Emulator": "1"}) as response:
                assert response.status == 400
            assert runtime.identities.resolve("r-id")["route_mode"] == "proxy"
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()


async def test_route_change_does_not_wait_for_busy_ws(tmp_path, monkeypatch):
    """TASK-057: обработчик занят, клиент не подтверждает close — PUT route_mode отвечает сразу
    (aiohttp ждал бы ответный close до 10 с), а соединение всё равно закрывается."""
    release = asyncio.Event()

    async def busy(self, data):
        await release.wait()

    monkeypatch.setattr(voice.Conversation, "receive_audio", busy)
    monkeypatch.setattr(ws_close, "CLOSE_TIMEOUT_S", 0.3)
    runtime = Runtime(tmp_path)
    registry(runtime, "SN-B", "b-id")
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            ws = await client.ws_connect(url + "/uni.ws", headers={"Host": "uniproxy.alice.yandex.net", "x-uprx-device-id": "b-id"})
            await ws.send_bytes(b"audio-frame")  # дальше клиент сокет не читает
            await asyncio.sleep(0.3)
            (session,) = runtime.sessions.values()
            started = time.monotonic()
            async with client.put(url + "/emulator/api/route_mode",
                                  json={"sn": "SN-B", "mode": "proxy"}, headers={"X-Emulator": "1"}) as response:
                assert response.status == 200
                assert (await response.json())["disconnected"] == 1
            assert time.monotonic() - started < 1.0
            for _ in range(60):  # close без ответа клиента → по таймауту закрывается транспорт
                if session["transport"].is_closing():
                    break
                await asyncio.sleep(0.05)
            assert session["ws"].closed and session["transport"].is_closing()
            release.set()
            await ws.close()
    finally:
        release.set()
        await runner.cleanup()
        await runtime.close()


async def test_route_change_closes_proxy_bridge(tmp_path):
    """TASK-057: мост прохода наверх (режим «Прозрачно») не в runtime.sessions — смена маршрута
    устройства закрывает и его (1012); соединения другого устройства не затрагиваются."""
    runtime = Runtime(tmp_path)
    registry(runtime, "SN-P", "p-id")
    runtime.identities.mutate({"operation": "device", "sn": "SN-O", "user_id": "u", "device_ids": ["o-id"]})
    for sn in ("SN-P", "SN-O"):
        runtime.identities.mutate({"operation": "route_mode", "sn": sn, "mode": "proxy"})
    calls = []
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            mine = await client.ws_connect(url + "/uni.ws", headers={"Host": "uniproxy.alice.yandex.net", "x-uprx-device-id": "p-id"})
            other = await client.ws_connect(url + "/uni.ws", headers={"Host": "uniproxy.alice.yandex.net", "x-uprx-device-id": "o-id"})
            await mine.send_str("a")
            assert (await mine.receive()).data == "up:a"
            assert not runtime.sessions and len(runtime.upstream.forwarder.connections) == 2
            started = time.monotonic()
            async with client.put(url + "/emulator/api/route_mode",
                                  json={"sn": "SN-P", "mode": "hybrid"}, headers={"X-Emulator": "1"}) as response:
                assert (await response.json())["disconnected"] == 1
            assert time.monotonic() - started < 1.0
            message = await asyncio.wait_for(mine.receive(), 5)
            assert message.type == WSMsgType.CLOSE and message.data == 1012
            await other.send_str("b")  # соединение другого устройства живо
            assert (await other.receive()).data == "up:b"
            await other.close()
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()


async def test_default_route_is_emulate(tmp_path):
    """TASK-064: маршрута по умолчанию нет — устройство без route_mode и неизвестное идут в эмуляцию;
    API `mode` удалён, `identities` не отдаёт default_route_mode, форма настроек его не сохраняет."""
    runtime = Runtime(tmp_path)
    registry(runtime, "SN-D", "d-id")
    calls = []
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession(headers={"X-Emulator": "1"}) as client:
            assert runtime.identities.resolve("d-id").get("route_mode") is None
            for headers in ({"Host": "quasar.yandex.net", "X-YaQuasarDeviceId": "d-id"},
                            {"Host": "quasar.yandex.net", "X-YaQuasarDeviceId": "ghost"},
                            {"Host": "quasar.yandex.net"}):
                async with client.get(url + "/no/handler", headers=headers) as response:
                    assert response.status == 404
            assert calls == []
            async with client.put(url + "/emulator/api/mode", json={"mode": "proxy"}) as response:
                assert response.status == 404
            async with client.get(url + "/emulator/api/identities") as response:
                assert "default_route_mode" not in await response.json()
            form = runtime.public()
            form["default_route_mode"] = "proxy"  # устаревшая форма настроек
            async with client.put(url + "/emulator/api/settings", json=form) as response:
                assert response.status == 200
                assert "default_route_mode" not in await response.json()
            assert "default_route_mode" not in runtime.settings
            async with client.get(url + "/no/handler", headers={"Host": "quasar.yandex.net", "X-YaQuasarDeviceId": "d-id"}) as response:
                assert response.status == 404
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()


class _FakeWS:
    def __init__(self, closed):
        self.closed = closed

    async def close(self, **kwargs):
        self.closed.append(kwargs)


async def test_route_change_closes_ws_only_when_serving_changes(tmp_path):
    """TASK-064: WS устройства обслуживает эмулятор (emulate, hybrid) или мост (proxy). Рвётся (1012)
    только переход в proxy или из него; emulate ↔ hybrid открытое соединение не трогает."""
    runtime = Runtime(tmp_path)
    registry(runtime, "SN-S", "s-id")
    closed = []
    runtime.sessions["a"] = {"device_sn": "SN-S", "ws": _FakeWS(closed)}
    runtime.sessions["other"] = {"device_sn": "SN-X", "ws": _FakeWS(closed)}

    async def change(mode):
        result = await runtime.set_device_route_mode("SN-S", mode)
        await runtime.closer.drain()
        return result["disconnected"], len(closed)

    assert await change("hybrid") == (0, 0)  # emulate → hybrid
    assert await change("emulate") == (0, 0)  # hybrid → emulate
    assert await change("emulate") == (0, 0)  # тот же маршрут
    assert await change("proxy") == (1, 1)  # emulate → proxy
    assert await change("proxy") == (0, 1)  # без изменений — не рвёт
    assert await change("hybrid") == (1, 2)  # proxy → hybrid
    assert await change("proxy") == (1, 3)
    assert await change(None) == (1, 4)  # proxy → снять маршрут (Эмуляция)
    assert await change(None) == (0, 4)
    assert all(item["code"] == 1012 for item in closed)
    await runtime.close()


async def test_route_change_response_does_not_wait_and_hybrid_keeps_ws(tmp_path, monkeypatch):
    """TASK-064: emulate → hybrid открытый WS не закрывает и отвечает сразу; emulate → proxy закрывает,
    ответ при этом не ждёт колонку (фоновое закрытие, TASK-057)."""
    release = asyncio.Event()

    async def busy(self, data):
        await release.wait()

    monkeypatch.setattr(voice.Conversation, "receive_audio", busy)
    monkeypatch.setattr(ws_close, "CLOSE_TIMEOUT_S", 0.3)
    runtime = Runtime(tmp_path)
    registry(runtime, "SN-K", "k-id")
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            ws = await client.ws_connect(url + "/uni.ws", headers={"Host": "uniproxy.alice.yandex.net", "x-uprx-device-id": "k-id"})
            await ws.send_bytes(b"audio-frame")
            await asyncio.sleep(0.3)
            (session,) = runtime.sessions.values()
            async with client.put(url + "/emulator/api/route_mode",
                                  json={"sn": "SN-K", "mode": "hybrid"}, headers={"X-Emulator": "1"}) as response:
                assert await response.json() == {"sn": "SN-K", "mode": "hybrid", "disconnected": 0}
            await asyncio.sleep(0.2)
            assert not session["ws"].closed and not ws.closed
            started = time.monotonic()
            async with client.put(url + "/emulator/api/route_mode",
                                  json={"sn": "SN-K", "mode": "proxy"}, headers={"X-Emulator": "1"}) as response:
                assert (await response.json())["disconnected"] == 1
            assert time.monotonic() - started < 1.0
            release.set()
            await ws.close()
    finally:
        release.set()
        await runner.cleanup()
        await runtime.close()


async def test_proxy_route_intercepts_ota(tmp_path):
    """TASK-066: OTA перехватывается и в «Прозрачно» — политика «Обновлений» действует,
    апстрим не вызван; запрос оригинала (для каталога URL) идёт мимо маршрута, петля закрыта."""
    runtime = Runtime(tmp_path)
    value = runtime.updates.settings()
    value['policy'] = 'deny'
    runtime.updates.save(value)
    served, fetches = [], []
    upstream_runner, port = await start_upstream(served)
    alias_upstream(runtime, port)
    registry(runtime)
    set_route(runtime, "SN-UP", "proxy")

    async def original(url, method, headers, body):  # запрос оригинала, как в эмуляции
        fetches.append((url, headers.get("X-Alice-Update-Probe")))
        return 200, {"Content-Type": "application/json"}, b'{"updates": [{"url": "https://cdn.example/ota.zip"}]}'

    runtime.update_fetch = original
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/tv/api/v4/updates?device_id=up-id", headers={"Host": "quasar.yandex.net"},
                                   json={"installed_apps": []}) as response:
                assert response.status == 200
                assert await response.json() == {"updates": []}  # ответ по политике, не от Яндекса
            assert served == []  # апстрим не вызван
            assert len(fetches) == 1 and fetches[0][1] == "1"  # оригинал запрошен с маркером probe
            inventory = runtime.updates.inventory(sn="SN-UP")
            assert inventory["attempts"] and inventory["attempts"][0]["device_sn"] == "SN-UP"
            # остальной HTTP «Прозрачно» — как раньше, прямым проходом
            async with client.get(url + "/anything?device_id=up-id", headers={"Host": "quasar.yandex.net"}) as response:
                assert (await response.json())["from"] == "upstream"
            assert len(served) == 1
            # защита от петли работает и в «Прозрачно»: probe-запрос не уходит в оригинал
            probes = len(fetches)
            async with client.post(url + "/tv/api/v4/updates?device_id=up-id",
                                   headers={"Host": "quasar.yandex.net", "X-Alice-Update-Probe": "1"},
                                   json={}) as response:
                assert response.status == 508
            assert len(fetches) == probes
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()
