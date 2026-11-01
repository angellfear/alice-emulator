"""TASK-070: встроенные ответы без эндпоинтов — метрики, проверки связи, подсказки,
обложки avatars с кешем, favicon панели. Промахи и журнал «Запись» не засоряются.
Пользовательский эндпоинт перекрывает встроенный."""
import json

from aiohttp import ClientSession, web

from server_emulator.dispatcher import Dispatcher
from server_emulator.endpoints import builtin_covers, find_builtin
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen

DEAD = "http://127.0.0.1:1"  # порт без слушателя: пересылка не выходит в сеть


def registry(runtime, sn="SN-S", device_id="s-id"):
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    runtime.identities.mutate({"operation": "device", "sn": sn, "user_id": "u", "device_ids": [device_id]})


class FakeRequest:
    """Минимальный запрос для builtin_answer без aiohttp-цикла."""

    def __init__(self, host, method, path, query_string="", headers=None, remote="192.0.2.50"):
        self.host, self.method, self.path = host, method, path
        self.query_string, self.headers, self.remote = query_string, headers or {}, remote
        self.rel_url = path + ("?" + query_string if query_string else "")


def test_builtin_answers_table():
    assert find_builtin("clck.yandex.net", "POST", "/quasar_metrics/write/batch")["status"] == 200
    assert find_builtin("clck.yandex.net", "POST", "/quasar_metrics/write/batch")["body"] == b""
    assert find_builtin("report.appmetrica.yandex.net", "POST", "/report")["status"] == 200
    assert find_builtin("scbh.yandex.net", "HEAD", "/generate_204")["status"] == 204
    assert find_builtin("scbh.yandex.net", "GET", "/generate_204")["status"] == 204
    assert find_builtin("soft.export.yandex.ru", "GET", "/status.xml")["body"] == b"<status>ok</status>"
    assert json.loads(find_builtin("rpc.alice.yandex.net", "POST", "/gproxy/get_auth_suggests")["body"]) == {}
    assert json.loads(find_builtin("rpc.alice.yandex.net", "POST", "/gproxy/get_teasers")["body"]) == {}
    assert json.loads(find_builtin("keyboard.yandex.net", "GET", "/experiments")["body"]) == {}
    assert json.loads(find_builtin("yandex.ru", "GET", "/ads/v1/startup")["body"]) == {}
    assert find_builtin("clck.yandex.net", "GET", "/unknown") is None  # метод не тот
    assert find_builtin("quasar.yandex.net", "POST", "/anything") is None


async def test_egress_answers_client_ip(tmp_path):
    """egress.yandex.net — проверка исходящего IP: адрес, с которого пришла колонка."""
    runtime = Runtime(tmp_path)
    request = FakeRequest("egress.yandex.net", "GET", "/", remote="192.0.2.50")
    status, body, headers = await Dispatcher(runtime).builtin_answer(request, "egress.yandex.net", "/")
    assert status == 200 and body == b"192.0.2.50" and headers["Content-Type"] == "text/plain"
    await runtime.close()


def test_builtin_covers_avatars_and_misses():
    assert builtin_covers("avatars.yandex.net", "GET", "/get-music-content/123/700x700")
    assert builtin_covers("avatars.yandex.net", "GET", "/get-video-content/9/b700x700")
    assert builtin_covers("clck.yandex.net", "POST", "/tv/write/json/batch")
    assert not builtin_covers("avatars.yandex.net", "GET", "/unrelated/path")
    assert not builtin_covers("quasar.yandex.net", "GET", "/get_sync_info")


async def test_dispatch_answers_builtins_without_miss_or_journal(tmp_path):
    """Метрики/204/подсказки/обложка без данных отвечаются эмулятором:
    промахов нет, журнал «Запись» не пишет, обложка без сети — 404."""
    runtime = Runtime(tmp_path)
    registry(runtime)
    runtime.identities.mutate({"operation": "record", "sn": "SN-S", "mode": "on"})
    runtime.settings["aliases"] = {"avatars.yandex.net": DEAD}
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/quasar_metrics/write/batch", headers={"Host": "clck.yandex.net"},
                                   json={"metrics": []}) as response:
                assert response.status == 200
            async with client.head(url + "/generate_204", headers={"Host": "scbh.yandex.net"}) as response:
                assert response.status == 204
            async with client.get(url + "/experiments", headers={"Host": "keyboard.yandex.net"}) as response:
                assert response.status == 200 and await response.json() == {}
            async with client.get(url + "/get-music-content/abc/700x700",
                                  headers={"Host": "avatars.yandex.net"}) as response:
                assert response.status == 404  # сети нет (цель мертва) и кеша нет
        assert runtime.endpoints.misses() == {}  # ни один из запросов — не промах
        await runtime.upstream.journal.flush()
        assert runtime.upstream.journal.list()["total"] == 0  # и в журнал «Запись» — тишина
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_avatar_forward_with_disk_cache(tmp_path):
    """Обложка пересылается настоящему серверу и кешируется; без сети — кеш (даже
    протухший); совсем без данных — 404. Range проходит насквозь без кеша."""
    runtime = Runtime(tmp_path)
    registry(runtime)
    served = []

    async def avatar_handler(request):
        served.append(request.rel_url.path_qs)
        return web.Response(body=b"JPEGDATA" * 64, content_type="image/jpeg")

    upstream_app = web.Application()
    upstream_app.router.add_route("*", "/{p:.*}", avatar_handler)
    upstream_runner, upstream_url = await listen(upstream_app)
    port = int(upstream_url.rsplit(":", 1)[1])
    runtime.settings["aliases"] = {"avatars.yandex.net": f"http://127.0.0.1:{port}"}

    class StubResolver:
        async def resolve(self, hostname, port_=0, family=None):
            from aiohttp.resolver import ResolveResult
            import socket as _socket
            return [ResolveResult(hostname=hostname, host="127.0.0.1", port=port_,
                                  family=_socket.AF_INET, proto=_socket.IPPROTO_TCP, flags=0)]

        async def close(self):
            pass

    runtime.upstream.forwarder.resolver = StubResolver()
    runner, url = await listen(create_app(runtime=runtime))
    dispatcher = Dispatcher(runtime)
    try:
        async with ClientSession() as client:
            async with client.get(url + "/get-music-content/abc/700x700",
                                  headers={"Host": "avatars.yandex.net"}) as response:
                assert response.status == 200 and await response.read() == b"JPEGDATA" * 64
            assert served == ["/get-music-content/abc/700x700"]
            files = list((runtime.directory / "cache" / "avatars").glob("*.bin"))
            assert len(files) == 1
            body, content_type, _ts = dispatcher._read_avatar(files[0].stem)
            assert body == b"JPEGDATA" * 64 and content_type == "image/jpeg"
            # цель умирает — ответ из кеша
            runtime.settings["aliases"] = {"avatars.yandex.net": DEAD}
            served.clear()
            async with client.get(url + "/get-music-content/abc/700x700",
                                  headers={"Host": "avatars.yandex.net"}) as response:
                assert response.status == 200 and await response.read() == b"JPEGDATA" * 64
            assert served == []  # из кеша, заглушка не звалась
            # совсем без данных — 404
            async with client.get(url + "/get-music-content/none/700x700",
                                  headers={"Host": "avatars.yandex.net"}) as response:
                assert response.status == 404
                assert (await response.json())["error"] == "avatar_unavailable"
            # Range-запрос проходит насквозь (кеш не подменяет частичный ответ)
            async with client.get(url + "/get-music-content/abc/700x700",
                                  headers={"Host": "avatars.yandex.net", "Range": "bytes=0-9"}) as response:
                assert response.status == 404  # цель мертва, Range кеш не использует
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()


async def test_user_endpoint_overrides_builtin(tmp_path):
    """Свой эндпоинт с тем же URL перекрывает встроенный ответ (защиты URL нет)."""
    runtime = Runtime(tmp_path)
    registry(runtime)
    runtime.endpoints.upsert({"id": "user.metrics", "host": "clck.yandex.net", "method": "POST",
                              "path": "/quasar_metrics/write/batch", "mode": "static", "status": 200,
                              "body_base64": "b2s=", "content_type": "text/plain"})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/quasar_metrics/write/batch",
                                   headers={"Host": "clck.yandex.net"}) as response:
                assert response.status == 200 and await response.text() == "ok"  # ответ пользователя
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_favicon_served_and_panel_requests_not_misses(tmp_path):
    """favicon.ico отдаётся файлом; другие пути панели (/, /emulator/) не промахи."""
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/favicon.ico") as response:
                assert response.status == 200
                assert response.headers["Content-Type"].startswith("image/")
            async with client.get(url + "/") as response:
                assert response.status == 200  # панель
        assert runtime.endpoints.misses() == {}
    finally:
        await runner.cleanup()
        await runtime.close()
