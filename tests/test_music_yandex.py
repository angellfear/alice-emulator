"""Встроенный поставщик Яндекс.Музыки (plugins/yandex-music/music/yandex_music.py):
поиск через библиотеку yandex-music с захваченным OAuth-токеном аккаунта (captured_oauth от ядра),
прямые ссылки mp3 из download-info (downloadInfoUrl → XML host/path/ts/s → get-mp3 с
подписью), очередь в памяти модуля.
Настоящий Яндекс из тестов не дёргается: URL-константы модуля подменяются на
локальный подменный HTTP.
"""
import hashlib
import importlib.util
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import unused_port

import server_emulator

MODULE = Path(server_emulator.__file__).parent / "plugins" / "yandex-music" / "music" / "yandex_music.py"

ITEMS = [{"id": f"{100 + i}", "title": f"Трек {i}", "artists": [{"name": "Артист"}],
          "albums": [{"title": "Альбом"}], "durationMs": 180000 + i,
          "ogImage": f"avatars.yandex.net/get-music-content/{i}/%%"} for i in range(1, 4)]

INFO_XML = ("<?xml version=\"1.0\" encoding=\"utf-8\"?><download-info><host>s1.storage.example</host>"
            "<path>/rmusic/U2FsdGVk/x</path><ts>0005f1</ts><region>-1</region><s>abc</s></download-info>")
EXPECTED_URL = ("https://s1.storage.example/get-mp3/"
                + hashlib.md5(("XGRlBW9FXlekgbPrRHuSiA" + "rmusic/U2FsdGVk/x" + "abc").encode()).hexdigest()
                + "/0005f1/rmusic/U2FsdGVk/x")


def load_provider():
    """Модуль встроенного плагина как отдельный (без импорта остального кода ядра)."""
    spec = importlib.util.spec_from_file_location("builtin_yandex_music", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Ctx:
    """Минимальный двойник ScenarioContext: sn/device/extension (+captured_oauth ядра)."""

    def __init__(self, sn="SN-T", market="ru", oauth="y0_" + "t" * 30):
        self.sn = sn
        self.device = {"sn": sn}
        self.text = ""
        values = {"market": market, "captured_oauth": oauth}
        self.extension = type("Ext", (), {
            "settings": values,
            "get": staticmethod(lambda key, default=None: values.get(key, default)),
        })()


class FakeMusic:
    """Подменный Web API Яндекс.Музыки: /search и /tracks/<id>/download-info, журнал."""

    def __init__(self):
        self.searches = []  # [dict(query)]
        self.infos = []  # [track_id]
        self.search_payload = {"result": {"tracks": {"results": ITEMS}}}
        self.info_payload = None  # собирается фикстурой: ссылка на XML подменного сервера
        self.rated = []  # [(kind, uid, track-ids)]

    async def search(self, request):
        self.searches.append(dict(request.query))
        return web.json_response(self.search_payload)

    async def info(self, request):
        self.infos.append(request.match_info["tid"])
        return web.json_response(self.info_payload)

    async def status(self, request):
        return web.json_response({"result": {"account": {"uid": 42, "login": "test"}}})

    async def rate(self, request):
        form = await request.post()
        self.rated.append((request.match_info["kind"], request.match_info["uid"], form.get("track-ids")))
        return web.json_response({"result": {"revision": 7}})

    async def xml(self, request):
        return web.Response(text=INFO_XML, content_type="text/xml")


@pytest.fixture()
def provider():
    module = load_provider()
    module.STATE.clear()
    yield module
    module.STATE.clear()


@pytest.fixture()
async def fake(provider, monkeypatch):
    fm = FakeMusic()
    app = web.Application()
    app.router.add_get("/search", fm.search)
    app.router.add_get("/tracks/{tid}/download-info", fm.info)
    app.router.add_get("/file-download-info/x", fm.xml)
    app.router.add_get("/account/status", fm.status)
    app.router.add_post("/users/{uid}/{kind}/tracks/add-multiple", fm.rate)
    app.router.add_get("/preview", fm.xml)  # библиотека добирает ссылки всех вариантов
    runner = web.AppRunner(app)
    await runner.setup()
    port = unused_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    monkeypatch.setattr(provider, "BASE_URL", f"http://127.0.0.1:{port}")  # клиент библиотеки — на подменный API
    monkeypatch.setattr(provider, "_final_link", lambda url: url)  # хост ссылки вымышленный — без сети
    fm.info_payload = {"result": [
        {"codec": "mp3", "preview": True, "bitrateInKbps": 320,
         "downloadInfoUrl": f"http://127.0.0.1:{port}/preview"},  # превью не берётся
        {"codec": "mp3", "preview": False, "bitrateInKbps": 192,
         "downloadInfoUrl": f"http://127.0.0.1:{port}/file-download-info/x"}]}
    yield fm
    await runner.cleanup()


async def test_search_without_token_honest_note(provider, fake):
    """Токен ещё не захвачен — честный отказ, к Яндексу не ходили."""
    result = await provider.search(Ctx(oauth=""), "ветер", "music")
    assert result == {"tracks": [], "note": provider.NOTE_NO_TOKEN}
    assert fake.searches == [] and fake.infos == []


async def test_search_returns_full_track_with_stream_link(provider, fake):
    """Поиск: Track контракта ядра (stream{url, format}); ссылка первого трека —
    download-info (лучший mp3 без превью → XML → get-mp3); запрос — text/type/page."""
    result = await provider.search(Ctx(), "ветер", "music")
    assert len(result["tracks"]) == len(ITEMS)  # выдача фейка, ядро режет до QUEUE_LIMIT
    assert len(result["tracks"]) <= provider.QUEUE_LIMIT
    first = result["tracks"][0]
    assert first["id"] == "101" and first["title"] == "Трек 1"
    assert first["artist"] == "Артист" and first["album"] == "Альбом"
    assert first["cover_url"].endswith("200x200")  # ogImage с %% — размер подставляет библиотека
    assert first["stream"] == {"url": EXPECTED_URL, "format": "mp3"}
    assert fake.infos == ["101"]  # ссылка добирается только для первого
    assert fake.searches[0]["text"] == "ветер" and fake.searches[0]["type"] == "track"
    assert fake.searches[0]["page"] == "0"  # без page API отвечает 400


async def test_empty_search_without_note(provider, fake):
    """Пустая выдача — ровно {"tracks": []} без note: ядро отвечает «не нашла»."""
    fake.search_payload = {"result": {"tracks": {"results": []}}}
    result = await provider.search(Ctx(), "ветер", "music")
    assert result == {"tracks": []}


async def test_search_failure_becomes_note(provider, fake, monkeypatch):
    """Сбой поиска → честный отказ с текстом ошибки."""
    def broken(*_args, **_kwargs):
        raise provider.MusicError("Яндекс не отвечает")

    monkeypatch.setattr(provider, "_download_link", broken)
    result = await provider.search(Ctx(), "ветер", "music")
    assert result == {"tracks": [], "note": "Яндекс не отвечает"}


async def test_empty_query_uses_cache_or_asks(provider, fake):
    """«Включи музыку» без запроса: без кеша — «Что включить?», после поиска —
    случайный трек из кеша (queue сохранена целиком)."""
    note = await provider.search(Ctx(), "", "music")
    assert note == {"tracks": [], "note": provider.NOTE_WHAT}
    saved = await provider.search(Ctx(), "ветер", "music")
    again = await provider.search(Ctx(), "", "music")
    assert again["context"] == {"type": "cache"}
    assert again["tracks"] == saved["tracks"]
    assert 0 <= again["start"] < len(again["tracks"])
    assert provider.STATE["SN-T"]["current"] == again["start"]
    assert len(fake.searches) == 1  # повторного поиска не было


async def test_neighbor_steps_through_queue_and_fetches_links(provider, fake):
    """«Дальше/назад»: соседний трек очереди, ссылка добирается при переходе, края — None."""
    await provider.search(Ctx(), "ветер", "music")
    second = await provider.neighbor(Ctx(), "101", None, 1)
    assert second["id"] == "102"
    assert second["stream"]["url"] == EXPECTED_URL
    assert fake.infos == ["101", "102"]  # ссылка соседнего — при переходе
    third = await provider.neighbor(Ctx(), "102", None, 1)
    assert third["id"] == "103"
    assert await provider.neighbor(Ctx(), "103", None, 1) is None  # дальше нечего
    back = await provider.neighbor(Ctx(), "103", None, -1)
    assert back["id"] == "102"
    assert await provider.neighbor(Ctx(), "101", None, -1) is None  # назад с первого — некуда
    assert await provider.neighbor(Ctx(sn="SN-другой"), "", None, 1) is None  # без очереди


async def test_rate_likes_and_dislikes_in_account(provider, fake):
    """Лайк/дизлайк — uid из account/status, затем likes|dislikes add-multiple."""
    assert await provider.rate(Ctx(), "101", True) is True
    assert await provider.rate(Ctx(), "102", False) is True
    assert fake.rated == [("likes", "42", "101"), ("dislikes", "42", "102")]
    assert await provider.rate(Ctx(oauth=""), "101", True) is None  # токена нет — к Яндексу не ходим
    assert len(fake.rated) == 2


async def test_final_link_follows_permanent_redirect(provider):
    """get-mp3 отвечает 308 — колонке уходит конечный адрес потока (её плеер 308 не проходит)."""
    import asyncio

    async def redirect(request):
        raise web.HTTPPermanentRedirect("/stream.mp3")

    async def stream(request):
        return web.Response(body=b"ID3", content_type="audio/mpeg")

    app = web.Application()
    app.router.add_get("/get-mp3/s/1/x", redirect)
    app.router.add_get("/stream.mp3", stream)
    runner = web.AppRunner(app)
    await runner.setup()
    port = unused_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    try:
        base = f"http://127.0.0.1:{port}"
        assert await asyncio.to_thread(provider._final_link, base + "/get-mp3/s/1/x") == base + "/stream.mp3"
        dead = "http://127.0.0.1:1/get-mp3/s/1/x"
        assert await asyncio.to_thread(provider._final_link, dead) == dead  # сбой — исходная ссылка
    finally:
        await runner.cleanup()
