"""Встроенный поставщик Яндекс.Музыки (plugins/yandex-music/music/yandex_music.py):
поиск через библиотеку yandex-music с захваченным OAuth-токеном аккаунта (captured_oauth от ядра),
прямые ссылки mp3 из download-info (downloadInfoUrl → XML host/path/ts/s → get-mp3 с
подписью), очередь в памяти модуля.
Настоящий Яндекс из тестов не дёргается: URL-константы модуля подменяются на
локальный подменный HTTP.
"""
import asyncio
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


class Data:
    """Двойник ctx.data: get/set поверх dict — персональное хранилище (state/)."""

    def __init__(self):
        self.rows = {}

    def get(self, key, default=None):
        return self.rows.get(key, default)

    def set(self, key, value):
        self.rows[key] = value
        return True


class Ctx:
    """Минимальный двойник ScenarioContext: sn/device/extension (+captured_oauth ядра)
    и ctx.data (персональное хранилище; общий экземпляр — shared_data)."""

    def __init__(self, sn="SN-T", market="ru", oauth="y0_" + "t" * 30, shared_data=None):
        self.sn = sn
        self.device = {"sn": sn}
        self.text = ""
        self.data = shared_data if shared_data is not None else Data()
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
        self.delay = 0.0  # сек: download-info отвечает с задержкой (тест таймаута)

    async def search(self, request):
        self.searches.append(dict(request.query))
        return web.json_response(self.search_payload)

    async def info(self, request):
        if self.delay:
            await asyncio.sleep(self.delay)
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
    download-info (лучший mp3 без превью → XML → get-mp3); запрос — text/type/page.
    Остальную очередь поставщик предзагружает в фоне: «дальше» отвечает мгновенно."""
    result = await provider.search(Ctx(), "ветер", "music")
    await provider.prefetch_wait()
    assert len(result["tracks"]) == len(ITEMS)  # выдача фейка, ядро режет до QUEUE_LIMIT
    assert len(result["tracks"]) <= provider.QUEUE_LIMIT
    first = result["tracks"][0]
    assert first["id"] == "101" and first["title"] == "Трек 1"
    assert first["artist"] == "Артист" and first["album"] == "Альбом"
    assert first["cover_url"].endswith("200x200")  # ogImage с %% — размер подставляет библиотека
    assert first["stream"] == {"url": EXPECTED_URL, "format": "mp3"}
    assert sorted(fake.infos) == ["101", "102", "103"]  # первый сразу, очередь — предзагрузкой
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
    """«Дальше/назад»: соседний трек очереди, края — None. После предзагрузки очереди
    поиском шаги отвечают из кеша ссылок — к API лишних обращений нет."""
    await provider.search(Ctx(), "ветер", "music")
    await provider.prefetch_wait()
    infos_after_search = sorted(fake.infos)
    second = await provider.neighbor(Ctx(), "101", None, 1)
    assert second["id"] == "102"
    assert second["stream"]["url"] == EXPECTED_URL
    third = await provider.neighbor(Ctx(), "102", None, 1)
    assert third["id"] == "103"
    assert await provider.neighbor(Ctx(), "103", None, 1) is None  # дальше нечего
    back = await provider.neighbor(Ctx(), "103", None, -1)
    assert back["id"] == "102"
    assert await provider.neighbor(Ctx(), "101", None, -1) is None  # назад с первого — некуда
    assert await provider.neighbor(Ctx(sn="SN-другой"), "", None, 1) is None  # без очереди
    assert sorted(fake.infos) == infos_after_search  # шаги из кеша — обращений не добавили


async def test_prev_after_next_and_prefetch_returns_previous_track(provider, fake):
    """Позицию очереди двигает только подтверждённый старт трека (recoverCommit → commit):
    «дальше» и предзагрузка @@mm_stack_engine_get_next её не сдвигают — «предыдущий»
    возвращает прошлый трек, а не играющий."""
    await provider.search(Ctx(), "ветер", "music")  # включи: очередь 101–103, current = 0
    second = await provider.neighbor(Ctx(), "", None, 1)  # «дальше»
    assert second["id"] == "102"
    assert await provider.commit(Ctx(), "102") is True  # колонка подтвердила старт
    third = await provider.neighbor(Ctx(), "", None, 1)  # предзагрузка get_next
    assert third["id"] == "103"
    back = await provider.neighbor(Ctx(), "", None, -1)  # «предыдущий»
    assert back["id"] == "101"  # прошлый, а не играющий
    assert await provider.commit(Ctx(), "42") is None  # чужой трек позицию не двигает
    assert await provider.commit(Ctx(sn="SN-другой"), "101") is None  # очереди нет


async def test_neighbor_link_fetch_is_bounded(provider, fake):
    """Добор ссылки в neighbor при промахе кеша — с коротким таймаутом запроса
    (LINK_TIMEOUT_S): медленный API не тянет ответ колонке (задержка «плей» 10–15 с
    была на этом пути). Фейк отвечает позже таймаута — neighbor вернул бы трек, успей
    запрос дождаться: None доказывает, что оборвал именно таймаут. Поиск ходит со
    стандартным таймаутом библиотеки."""
    await provider.search(Ctx(), "ветер", "music")
    provider.LINKS.clear()  # кеш предзагрузки убрать: проверяем именно добор при промахе
    fake.delay = provider.LINK_TIMEOUT_S + 3
    assert await provider.neighbor(Ctx(), "101", None, 1) is None  # таймаут раньше ответа фейка
    assert fake.infos  # до API сходили — оборвал таймаут


async def test_restart_restores_queue_position_and_links(provider, fake):
    """Перезапуск сервера: очередь и позиция живут в ctx.data (state/, по владельцу) —
    «плей» продолжает последний трек, «дальше/назад» идут от него без новой голосовой
    команды. Подписанных ссылок потока в хранилище нет (живут ограниченное время) —
    первый шаг после рестарта добирает их у Яндекса."""
    shared = Data()
    await provider.search(Ctx(shared_data=shared), "ветер", "music")
    await provider.prefetch_wait()
    assert await provider.commit(Ctx(shared_data=shared), "102") is True  # играет второй
    record = shared.rows[provider.PERSIST_KEY]
    assert record["current"] == 1 and len(record["queue"]) == 3
    assert all("stream" not in t for t in record["queue"])  # подписанных ссылок в хранилище нет

    provider.STATE.clear()  # рестарт: память поставщика умирает с процессом
    provider.LINKS.clear()
    infos_before_restart = list(fake.infos)
    current = await provider.neighbor(Ctx(shared_data=shared), None, None, 0)  # «плей»
    assert current["id"] == "102"
    assert current["stream"]["url"] == EXPECTED_URL  # ссылка добрана заново
    assert fake.infos == infos_before_restart + ["102"]  # один добор — дальше из кеша
    following = await provider.neighbor(Ctx(shared_data=shared), None, None, 1)  # «дальше»
    assert following["id"] == "103"
    assert await provider.commit(Ctx(shared_data=shared), "103") is True
    back = await provider.neighbor(Ctx(shared_data=shared), None, None, -1)  # «назад»
    assert back["id"] == "102"
    again = await provider.search(Ctx(shared_data=shared), "", "music")  # «включи музыку»
    assert again["context"] == {"type": "cache"}  # очередь из хранилища — вопрос не нужен


async def test_commit_prefetches_neighbors_in_background(provider, fake):
    """Подтверждение старта трека предзагружает ссылки соседей в фоне: следующая
    кнопка отвечает мгновенно — добор ссылок не в ответе колонке."""
    await provider.search(Ctx(), "ветер", "music")
    await provider.prefetch_wait()
    provider.LINKS.clear()  # проверяем предзагрузку commit, не поиск
    assert await provider.commit(Ctx(), "101") is True
    await provider.prefetch_wait()
    # поиск + его предзагрузка (101–103); commit добрал соседа справа (102) заранее
    assert fake.infos == ["101", "102", "103", "102"]
    infos_before = list(fake.infos)
    following = await provider.neighbor(Ctx(), None, None, 1)
    assert following["id"] == "102"
    assert fake.infos == infos_before  # из кеша — ни одного обращения к API


async def test_link_cache_expires_and_refetches(provider, fake):
    """Кеш ссылки живёт LINK_TTL_S (подпись ссылки не вечна): протухшая ссылка
    добирается заново при переходе, а не отдаётся колонке."""
    await provider.search(Ctx(), "ветер", "music")
    await provider.prefetch_wait()
    provider.LINKS["SN-T"]["102"]["ts"] -= provider.LINK_TTL_S + 1  # ссылка протухла
    following = await provider.neighbor(Ctx(), "101", None, 1)
    assert following["id"] == "102" and following["stream"]["url"] == EXPECTED_URL
    assert fake.infos.count("102") == 2  # предзагрузка + добор протухшей ссылки


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
