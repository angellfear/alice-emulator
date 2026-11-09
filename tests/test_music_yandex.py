"""Встроенный поставщик Яндекс.Музыки (plugins/yandex-music/music/yandex_music.py):
поиск по Web API с захваченным OAuth-токеном аккаунта (captured_oauth от ядра),
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
          "ogImage": f"https://cover.yandex.net/{i}/%@"} for i in range(1, 4)]

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

    async def search(self, request):
        self.searches.append(dict(request.query))
        return web.json_response(self.search_payload)

    async def info(self, request):
        self.infos.append(request.match_info["tid"])
        return web.json_response(self.info_payload)

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
    runner = web.AppRunner(app)
    await runner.setup()
    port = unused_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    monkeypatch.setattr(provider, "SEARCH_URL", f"http://127.0.0.1:{port}/search")
    fm.info_payload = {"result": [
        {"codec": "mp3", "preview": True, "bitrateInKbps": 320,
         "downloadInfoUrl": f"http://127.0.0.1:{port}/preview"},  # превью не берётся
        {"codec": "mp3", "preview": False, "bitrateInKbps": 192,
         "downloadInfoUrl": f"http://127.0.0.1:{port}/file-download-info/x"}]}
    monkeypatch.setattr(provider, "DOWNLOAD_INFO",
                        f"http://127.0.0.1:{port}/tracks/{{}}/download-info")
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
    assert first["cover_url"].endswith("200x200")  # ogImage с %@ подставляет размер
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

    monkeypatch.setattr(provider, "_search", broken)
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


async def test_rate_is_not_supported(provider):
    """Лайки не поддержаны — честно None."""
    assert await provider.rate(Ctx(), "101", True) is None
