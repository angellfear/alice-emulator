"""Встроенные ответы без эндпоинтов — метрики, проверки связи, подсказки,
обложки avatars с кешем, favicon панели. Промахи и журнал «Запись» не засоряются.
Пользовательский эндпоинт перекрывает встроенный."""
import json

from aiohttp import ClientSession, web

from server_emulator.dispatcher import Dispatcher
from server_emulator.endpoints import builtin_covers, find_builtin
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen
from tests.test_device_route_modes import alias_upstream, start_upstream

DEAD = "http://0.0.0.0:1"  # адрес не существует: отказ мгновенный, не 2 с Windows-рефузла


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
    assert find_builtin("clck.yandex.net", "POST", "/quasar_metrics/write/batch")["body"] == b"\x0a\x02OK"
    report = find_builtin("report.appmetrica.yandex.net", "POST", "/report")
    assert report["status"] == 200 and json.loads(report["body"]) == {"status": "accepted"}
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
    assert builtin_covers("rpc.alice.yandex.net", "POST", "/gproxy/music_search")
    assert builtin_covers("rpc.alice.yandex.net", "POST", "/gproxy/save_search_suggest_history")
    assert not builtin_covers("avatars.yandex.net", "GET", "/unrelated/path")
    assert not builtin_covers("quasar.yandex.net", "GET", "/get_sync_info")


async def test_music_search_generated_answer(tmp_path):
    """Поиск музыки с экрана в «Эмуляции» — генерируемый пустой результат по схеме:
    Type — эхо типа запроса, галерей нет (экран показывает «ничего не найдено»),
    промахов нет. В «Прозрачном» поиск уходит настоящему сервису. Сохранение
    подсказок — пустое подтверждение в «Эмуляции»."""
    from server_emulator.schema import load_type, schema_file, schemas_dir

    runtime = Runtime(tmp_path)
    registry(runtime)
    runtime.identities.mutate({"operation": "record", "sn": "SN-S", "mode": "on"})
    calls = []
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    runner, url = await listen(create_app(runtime=runtime))
    root = schemas_dir()
    name = schema_file(root, "centaur")
    request_type = load_type(root, name, "NAlice.NMusic.TMusicSearchRequest")
    response_type = load_type(root, name, "NAlice.NMusic.TMusicSearchResponse")
    try:
        query = request_type(Query="тест", Type=request_type.Track, Limit=20).SerializeToString()
        async with ClientSession() as client:
            async with client.post(url + "/gproxy/music_search",
                                   headers={"Host": "rpc.alice.yandex.net", "X-YaQuasarDeviceId": "s-id"},
                                   data=query) as response:
                assert response.status == 200
                assert response.headers["Content-Type"].startswith("application/x-protobuf")
                answer = response_type.FromString(await response.read())
                assert answer.Type == request_type.Track and not answer.SearchElements
        assert runtime.endpoints.misses() == {}  # генерируемый ответ — не промах
        assert not calls  # в «Эмуляции» настоящий сервис не звался

        async with ClientSession() as client:
            async with client.post(url + "/gproxy/save_search_suggest_history",
                                   headers={"Host": "rpc.alice.yandex.net", "X-YaQuasarDeviceId": "s-id"},
                                   data=b"\x0a\x04test") as response:
                assert response.status == 200
                # пустое подтверждение: TSaveSearchSuggestHistoryResponse без полей — пустое тело
                assert await response.read() == b""

        runtime.identities.mutate({"operation": "route_mode", "sn": "SN-S", "mode": "hybrid"})
        async with ClientSession() as client:
            async with client.post(url + "/gproxy/music_search",
                                   headers={"Host": "rpc.alice.yandex.net", "X-YaQuasarDeviceId": "s-id"},
                                   data=query) as response:
                assert response.status == 200
                assert await response.json() == {"from": "upstream", "path": "/gproxy/music_search"}
        assert calls  # в «Прозрачном» выдача настоящая
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()


async def test_search_family_generated_answers(tmp_path):
    """Голосовой поиск и обслуги (подсказки, промо-фразы, голосовое управление
    аккаунтами) — в «Эмуляции» пустые ответы по схеме (своей выдачи нет), промахов
    нет; urlinfo браузера — пустой JSON. В «Прозрачном» всё уходит настоящему
    сервису. Тела запросов — формы захваченных промахов (без данных аккаунта)."""
    runtime = Runtime(tmp_path)
    registry(runtime)
    calls = []
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    runtime.settings["aliases"]["sba.yandex.net"] = f"http://127.0.0.1:{port}"
    runner, url = await listen(create_app(runtime=runtime))
    posts = [("/gproxy/search", b"\x12\x08moderate"),
             ("/gproxy/get_search_suggests", b"\x18\x05"),
             ("/gproxy/get_tv_promo_phrases", b"\x12\n2147483647"),
             ("/gproxy/get_account_list_voice_controls", b"\x0a\x04test")]
    try:
        async with ClientSession() as client:
            for path, body in posts:
                async with client.post(url + path,
                                       headers={"Host": "rpc.alice.yandex.net", "X-YaQuasarDeviceId": "s-id"},
                                       data=body) as response:
                    assert response.status == 200
                    assert response.headers["Content-Type"].startswith("application/x-protobuf")
                    assert await response.read() == b""  # пустое сообщение по умолчанию — валидный ответ
            async with client.get(url + "/urlinfo",
                                  headers={"Host": "sba.yandex.net", "X-YaQuasarDeviceId": "s-id"},
                                  params={"pver": "1.0", "url": "https://m.youtube.com/watch?v=x"}) as response:
                assert response.status == 200
                assert response.headers["Content-Type"].startswith("application/json")
                assert await response.json() == {}
        assert runtime.endpoints.misses() == {}  # ни один из запросов — не промах
        assert not calls  # в «Эмуляции» настоящий сервис не звался

        runtime.identities.mutate({"operation": "route_mode", "sn": "SN-S", "mode": "hybrid"})
        async with ClientSession() as client:
            async with client.post(url + "/gproxy/search",
                                   headers={"Host": "rpc.alice.yandex.net", "X-YaQuasarDeviceId": "s-id"},
                                   data=b"\x12\x08moderate") as response:
                assert response.status == 200
                assert await response.json() == {"from": "upstream", "path": "/gproxy/search"}
            async with client.get(url + "/urlinfo",
                                  headers={"Host": "sba.yandex.net", "X-YaQuasarDeviceId": "s-id"},
                                  params={"url": "https://m.youtube.com"}) as response:
                assert response.status == 200
                assert (await response.json())["path"].startswith("/urlinfo?")
        assert calls  # в «Прозрачном» выдача настоящая
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()


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
        assert (await runtime.upstream.journal.list())["total"] == 0  # и в журнал «Запись» — тишина
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
