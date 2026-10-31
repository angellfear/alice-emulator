"""B01: lifecycle runtime — общий клиент, трекинг задач, идемпотентный close, контекст."""
import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

from server_emulator.lifecycle import Lifecycle, RequestContext
from server_emulator.runtime import Runtime
from server_emulator.server import create_app


def test_request_context_is_immutable():
    context = RequestContext(device_id="d1", device_sn="SN1", user_id="u1", connection_id="c1")
    with pytest.raises(Exception):
        context.device_sn = "SN2"
    assert context.recorder is None


def test_context_record_is_noop_without_recorder():
    context = RequestContext()
    context.record({"x": 1}, kind="ws")  # не падает и ничего не делает
    calls = []
    context = RequestContext(recorder=lambda event, **kw: calls.append((event, kw)))
    context.record({"x": 1}, kind="ws")
    assert calls and calls[0][1]["kind"] == "ws"


async def test_lifecycle_shared_client_and_idempotent_close():
    lifecycle = Lifecycle()
    first = lifecycle.client_session()
    assert lifecycle.client_session() is first and not first.closed
    await lifecycle.close()
    await lifecycle.close()  # повторный close безопасен
    assert first.closed and lifecycle.client is None and not lifecycle.tasks
    with pytest.raises(RuntimeError):
        lifecycle.client_session()
    coro = asyncio.sleep(0)
    with pytest.raises(RuntimeError):
        lifecycle.spawn(coro)
    coro.close()


async def test_spawned_tasks_cancelled_on_close():
    lifecycle = Lifecycle()
    started = asyncio.Event()

    async def forever():
        started.set()
        await asyncio.sleep(3600)

    task = lifecycle.spawn(forever())
    await started.wait()
    await lifecycle.close()
    assert task.done()
    assert not lifecycle.tasks


async def test_provider_uses_shared_client_and_closes_it(tmp_path):
    async def handler(request):
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/v1/chat", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}/v1/chat"
    runtime = Runtime(tmp_path)
    runtime.settings["model"].update(url=url, model="x")
    try:
        raw, content_type = await runtime.provider("model", {"q": 1})
        assert json.loads(raw) == {"ok": True}
        client = runtime.lifecycle.client
        assert client is not None and not client.closed
        raw, _ = await runtime.provider("model", {"q": 2})
        assert runtime.lifecycle.client is client  # один клиент на оба вызова
    finally:
        await runtime.close()
        await runner.cleanup()
    assert client.closed


async def test_same_response_with_and_without_recorder(tmp_path):
    """Standalone и embedded дают одинаковый fixture-ответ; recorder только фиксирует."""
    runtime = Runtime(tmp_path)
    import base64 as _b64
    runtime.endpoints.upsert({"id": "fixture", "host": "quasar.yandex.net", "path": "/fixture",
                              "method": "GET", "status": 200, "content_type": "application/json",
                              "body_base64": _b64.b64encode(b'{"ok": true}').decode()})

    class FakeRequest:
        def __init__(self, path, headers, method="GET"):
            self.path = path
            self.query = {}
            self.headers = headers
            self.method = method
            self.host = "quasar.yandex.net:443"
            self.query_string = ""
            self.remote = "127.0.0.1"

        async def read(self):
            return b""

    runner = web.AppRunner(create_app(runtime=runtime))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    captured = []
    try:
        async with ClientSession() as client:
            async with client.get(f"http://127.0.0.1:{port}/fixture",
                                  headers={"Host": "quasar.yandex.net"}) as plain:
                plain_body, plain_status = await plain.read(), plain.status
        fake = FakeRequest("/fixture", {})
        response = await runtime.dispatch(fake, record=lambda event, **kw: captured.append((event, kw)))
        embedded_body, embedded_status = response.body, response.status
        assert plain_status == embedded_status == 200
        assert plain_body == embedded_body == b'{"ok": true}'
        assert captured and captured[0][1].get("emulated") is True
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_app_cleanup_closes_runtime_client(tmp_path):
    """standalone: on_cleanup приложения закрывает разделяемый клиент runtime."""
    runtime = Runtime(tmp_path)
    runner = web.AppRunner(create_app(runtime=runtime))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        async with ClientSession() as client:
            async with client.get(f"http://127.0.0.1:{port}/emulator/api/status") as response:
                assert response.status == 200
        client = runtime.lifecycle.client_session()
        assert not client.closed
    finally:
        await runner.cleanup()  # запускает on_cleanup → runtime.close()
    assert client.closed and runtime.lifecycle.closed
