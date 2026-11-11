"""Расширение/плагин «Spotify» (music/spotify.py) на ядре поставщика музыки.

Две группы тестов, один подменный HTTP (настоящий Spotify из тестов не дёргается):

- модульные — код встроенного плагина (server_emulator/plugins/spotify/music/spotify.py)
  грузится напрямую (importlib): поиск/очередь/embed в изоляции, URL-константы модуля
  (ACCOUNTS_URL/SEARCH_URL/TRACK_URL/EMBED_URL) подменяются на локальный подменный
  HTTP — фейковые /api/token, /v1/search, /v1/tracks/<id> и embed-страница
  __NEXT_DATA__ с audioPreview;
- интеграционные — внешнее расширение «Spotify» подключается режимом локальной папки
  (docs/contracts/extensions.md): install({"url": папка}, confirm=True) + enable —
  манифест валидирует настоящий менеджер, модуль грузит реестр music/*.py, ключи
  Web API — настройки расширения (set_values); встроенный builtin.spotify живёт
  рядом, поэтому выбор — явно settings["providers"]["music"] = "spotify".

Очередь поставщика — память модуля: «включи музыку» и колбэк «дальше» — разные
запросы (разные message_id), очередь переживает их в STATE.
"""
import datetime
import importlib.util
import json
import os
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import unused_port
from google.protobuf import json_format

import server_emulator
from server_emulator import music, providers
from server_emulator.aapi_handlers import route as callback_route
from server_emulator.runtime import Runtime
from server_emulator.schema import load_type, schemas_dir, station_file
from tests.conftest import types

CLIENT, SERVER = types()

QUEUE_PROTO = "NAlice.TMusicPlatformQueueCapability.TApplyMusicPlatformQueueDirective"
SCREEN_PROTO = "NAlice.TAudioPlayerScreenCapability.TShowAudioPlayerScreenDirective"
SEQSTATE_PROTO = "NAlice.TAudioPlayerCapability.TSetSequenceStateDirective"

KEYS = {"client_id": "test-id", "client_secret": "test-secret", "market": "RU"}

API_ITEMS = [
    {"id": f"sp{i}", "name": f"Трек {i}", "artists": [{"name": "Артист"}],
     "album": {"name": f"Альбом {i}", "images": [{"url": f"https://image-cdn.spotifycdn.com/img{i}"}]},
     "duration_ms": 30000 + i, "preview_url": f"https://p.scdn.co/mp3-preview/prev{i}"}
    for i in range(1, 6)
]

EMBED_ID = "3n3Ppam7vgaVa1iaRUc9Lp"
EMBED_ENTITY = {"type": "track", "id": EMBED_ID, "name": "Mr. Brightside",
                "artists": [{"name": "The Killers"}], "duration": 222200,
                "audioPreview": {"url": "https://p.scdn.co/mp3-preview/embedprev"},
                "visualIdentity": {"image": [{"url": "https://image-cdn.spotifycdn.com/embedimg"}]}}

SPOTIFY_MODULE = Path(server_emulator.__file__).parent / "plugins" / "spotify" / "music" / "spotify.py"


def load_provider():
    """Модуль встроенного плагина как отдельный (без импорта остального кода ядра)."""
    spec = importlib.util.spec_from_file_location("builtin_spotify", SPOTIFY_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def embed_page(entity):
    """Embed-страница трека: скрипт __NEXT_DATA__ с entity."""
    payload = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return ("<!DOCTYPE html><script id=\"__NEXT_DATA__\" type=\"application/json\">"
            + json.dumps(payload) + "</script>")


# --- подменный Spotify: фиксированные ответы + журнал вызовов ---------------------------


class FakeSpotify:

    def __init__(self):
        self.token_calls = []  # [(Authorization, body)]
        self.token_status = 200
        self.search_calls = []  # [dict(query)]
        self.search_headers = []
        self.search_status = 200
        self.search_items = None  # None — обычная выдача API_ITEMS; иначе список items
        self.track_calls = []  # [track_id]
        self.track_by_id = None
        self.embed_calls = []  # [track_id]
        self.embed_entity = dict(EMBED_ENTITY)
        self.embed_page = None  # None — страница из embed_entity

    async def _token(self, request):
        self.token_calls.append((request.headers.get("Authorization", ""),
                                 (await request.read()).decode("utf-8")))
        return web.json_response({"access_token": "stub-token", "expires_in": 3600},
                                 status=self.token_status)

    async def _search(self, request):
        self.search_calls.append(dict(request.query))
        self.search_headers.append(dict(request.headers))
        if self.search_status != 200:
            return web.json_response({"error": {}}, status=self.search_status)
        items = API_ITEMS if self.search_items is None else self.search_items
        return web.json_response({"tracks": {"items": items}})

    async def _track(self, request):
        track_id = request.match_info["tid"]
        self.track_calls.append(track_id)
        if self.track_by_id is None:
            return web.json_response({"error": {}}, status=404)
        return web.json_response(self.track_by_id)

    async def _embed(self, request):
        track_id = request.match_info["tid"]
        self.embed_calls.append(track_id)
        if self.embed_page is not None:
            return web.Response(text=self.embed_page, content_type="text/html")
        return web.Response(text=embed_page(self.embed_entity), content_type="text/html")


@pytest.fixture()
def provider():
    module = load_provider()
    module.STATE.clear()
    module.TOKENS.clear()
    yield module
    module.STATE.clear()
    module.TOKENS.clear()


class ProviderCtx:
    """Минимальный ctx поставщика: extension (настройки) и sn (владелец очереди)."""

    def __init__(self, values=None, sn="SN-1"):
        self.sn = sn
        values = dict(values or {})
        self.extension = type("Ext", (), {
            "get": staticmethod(lambda key, default=None: values.get(key, default))})()


@pytest.fixture()
async def stub(provider, monkeypatch):
    """Локальный подменный HTTP вместо настоящего Spotify + подмена констант модуля."""
    fake = FakeSpotify()
    app = web.Application()
    app.router.add_post("/api/token", fake._token)
    app.router.add_get("/v1/search", fake._search)
    app.router.add_get("/v1/tracks/{tid}", fake._track)
    app.router.add_get("/embed/track/{tid}", fake._embed)
    runner = web.AppRunner(app)
    await runner.setup()
    port = unused_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(provider, "ACCOUNTS_URL", base + "/api/token")
    monkeypatch.setattr(provider, "SEARCH_URL", base + "/v1/search")
    monkeypatch.setattr(provider, "TRACK_URL", base + "/v1/tracks/")
    monkeypatch.setattr(provider, "EMBED_URL", base + "/embed/track/{}")
    yield fake
    await runner.cleanup()


# --- модульные: поиск по строке, очередь, embed -------------------------------------------


async def test_provider_search_without_keys_notes(provider, stub):
    """Без ключей строковый запрос — честный отказ с подсказкой, к Spotify не ходили."""
    result = await provider.search(ProviderCtx({"market": "RU"}), "тестовый исполнитель", "music")
    assert result["tracks"] == []
    assert "client_id" in result["note"] and "client_secret" in result["note"]
    assert stub.token_calls == [] and stub.search_calls == []


async def test_provider_empty_query_without_cache_asks(provider, stub):
    """«Включи музыку» без запроса и без кеша — «Что включить?»."""
    result = await provider.search(ProviderCtx(), "", "music")
    assert result == {"tracks": [], "note": provider.NOTE_WHAT}


async def test_provider_empty_query_with_stale_cache_asks(provider):
    """Кеш старше TTL (неделя) — не используется."""
    provider.STATE["SN-1"] = {"queue": [{"id": "x", "title": "T", "stream": {"url": "u", "format": "mp3"}}],
                              "current": 0, "saved_ts": 0}
    provider.STATE["SN-1"]["saved_ts"] -= provider.QUEUE_TTL_S + 86400
    result = await provider.search(ProviderCtx(), "", "music")
    assert result == {"tracks": [], "note": provider.NOTE_WHAT}


async def test_provider_search_full_flow(provider, stub):
    """С ключами: токен (Basic + client_credentials) → поиск (Bearer, q/type/market/limit) →
    топ-5 с превью, очередь в STATE."""
    ctx = ProviderCtx(KEYS)
    result = await provider.search(ctx, "тестовый исполнитель", "music")
    assert result["start"] == 0 and result["context"] == {"type": "search"}
    assert [t["id"] for t in result["tracks"]] == [f"sp{i}" for i in range(1, 6)]
    first = result["tracks"][0]
    assert first == {"id": "sp1", "title": "Трек 1", "artist": "Артист", "album": "Альбом 1",
                     "duration_ms": 30001, "cover_url": "https://image-cdn.spotifycdn.com/img1",
                     "stream": {"url": "https://p.scdn.co/mp3-preview/prev1", "format": "mp3"}}
    queue = provider.STATE["SN-1"]
    assert len(queue["queue"]) == 5 and queue["current"] == 0
    [(auth, body)] = stub.token_calls
    assert auth.startswith("Basic ") and "grant_type=client_credentials" in body
    assert stub.search_headers[0]["Authorization"] == "Bearer stub-token"
    assert stub.search_calls[0]["q"] == "тестовый исполнитель"
    assert stub.search_calls[0]["market"] == "RU"
    assert stub.search_calls[0]["limit"] == "10" and stub.search_calls[0]["type"] == "track"


async def test_provider_empty_market_not_sent(provider, stub):
    """Пустой market — параметр не передаётся."""
    values = dict(KEYS, market="")
    await provider.search(ProviderCtx(values), "джаз", "music")
    assert "market" not in stub.search_calls[0]


async def test_provider_search_filters_no_preview_and_cuts_queue(provider, stub):
    """Треки без preview_url не играют и не показываются; очередь режется до 5."""
    stub.search_items = [{"id": f"x{i}", "name": f"X {i}", "artists": [{"name": "A"}],
                          "duration_ms": 1000} for i in range(3)] + API_ITEMS[:4]
    result = await provider.search(ProviderCtx(KEYS), "тест", "music")
    assert [t["id"] for t in result["tracks"]] == [f"sp{i}" for i in range(1, 5)]
    assert len(result["tracks"]) <= provider.QUEUE_LIMIT


async def test_provider_empty_result_exact_shape(provider, stub):
    """Пустая выдача — ровно {"tracks": []} без note: ядро отвечает «не нашла»."""
    stub.search_items = []
    result = await provider.search(ProviderCtx(KEYS), "тест", "music")
    assert result == {"tracks": []}


async def test_provider_token_cached_between_searches(provider, stub):
    """Токен кешируется: два поиска — один запрос токена."""
    await provider.search(ProviderCtx(KEYS), "один", "music")
    await provider.search(ProviderCtx(KEYS), "два", "music")
    assert len(stub.token_calls) == 1
    assert len(stub.search_calls) == 2


async def test_provider_search_http_error_notes(provider, stub):
    """Сбой поиска (HTTP 500) — честный отказ «Spotify недоступен»."""
    stub.search_status = 500
    result = await provider.search(ProviderCtx(KEYS), "тест", "music")
    assert result["tracks"] == [] and "Spotify недоступен" in result["note"]


async def test_provider_token_error_notes_keys_hint(provider, stub):
    """Сбой входа (токен 400) — отказ с подсказкой про client_id/client_secret."""
    stub.token_status = 400
    result = await provider.search(ProviderCtx(KEYS), "тест", "music")
    assert result["tracks"] == []
    assert "client_id" in result["note"] and "client_secret" in result["note"]


async def test_provider_link_with_keys_uses_web_api(provider, stub):
    """Ссылка на трек с ключами — точный трек через /v1/tracks/<id>, поиск не дёргается."""
    stub.track_by_id = dict(API_ITEMS[0], id=EMBED_ID)
    result = await provider.search(ProviderCtx(KEYS), f"https://open.spotify.com/track/{EMBED_ID}", "music")
    assert result["tracks"][0]["id"] == EMBED_ID and result["start"] == 0
    assert stub.track_calls == [EMBED_ID] and stub.search_calls == []
    assert stub.embed_calls == []


async def test_provider_uri_with_keys_uses_web_api(provider, stub):
    """URI spotify:track:<id> с ключами — тот же путь /v1/tracks/<id>."""
    stub.track_by_id = dict(API_ITEMS[0], id=EMBED_ID)
    result = await provider.search(ProviderCtx(KEYS), f"spotify:track:{EMBED_ID}", "music")
    assert result["tracks"][0]["id"] == EMBED_ID
    assert stub.track_calls == [EMBED_ID]


async def test_provider_link_without_keys_uses_embed(provider, stub):
    """Без ключей ссылка на трек — открытый embed: превью, название, артист, обложка."""
    result = await provider.search(ProviderCtx({}), f"https://open.spotify.com/track/{EMBED_ID}", "music")
    assert result["tracks"][0] == {
        "id": EMBED_ID, "title": "Mr. Brightside", "artist": "The Killers",
        "duration_ms": 222200, "cover_url": "https://image-cdn.spotifycdn.com/embedimg",
        "stream": {"url": "https://p.scdn.co/mp3-preview/embedprev", "format": "mp3"}}
    assert stub.embed_calls == [EMBED_ID] and stub.token_calls == []


async def test_provider_intl_link_without_keys_uses_embed(provider, stub):
    """Ссылка с языковым сегментом /intl-ru/track/ — тот же embed-парсер."""
    result = await provider.search(ProviderCtx({}), f"https://open.spotify.com/intl-ru/track/{EMBED_ID}", "music")
    assert result["tracks"][0]["id"] == EMBED_ID


async def test_provider_embed_without_preview_notes(provider, stub):
    """Embed без audioPreview — отказ «нет 30-секундного превью»."""
    stub.embed_entity = {"id": EMBED_ID, "name": "X"}
    result = await provider.search(ProviderCtx({}), f"https://open.spotify.com/track/{EMBED_ID}", "music")
    assert result == {"tracks": [], "note": provider.NOTE_NO_PREVIEW}


async def test_provider_embed_unexpected_page_notes(provider, stub):
    """Неожиданная embed-страница — отказ «Не удалось получить данные трека»."""
    stub.embed_page = "<html>не то</html>"
    result = await provider.search(ProviderCtx({}), f"https://open.spotify.com/track/{EMBED_ID}", "music")
    assert result == {"tracks": [], "note": provider.NOTE_EMBED_FAIL}


async def test_provider_neighbor_steps_and_edges(provider):
    """Очередь: вперёд/назад по шагу, края — None, позиция по играющему track_id."""
    queue = [{"id": f"q{i}", "title": f"Q{i}", "stream": {"url": f"u{i}", "format": "mp3"}}
             for i in range(3)]
    provider.STATE["SN-1"] = {"queue": queue, "current": 0, "saved_ts": 0}
    ctx = ProviderCtx()
    assert (await provider.neighbor(ctx, "q0", None, 1))["id"] == "q1"
    assert (await provider.neighbor(ctx, "q1", None, 1))["id"] == "q2"
    assert await provider.neighbor(ctx, "q2", None, 1) is None  # край
    assert (await provider.neighbor(ctx, "q2", None, -1))["id"] == "q1"
    assert await provider.neighbor(ctx, "q0", None, -1) is None  # назад с первого
    assert (await provider.neighbor(ctx, "q0", None, 1))["id"] == "q1"  # позиция по track_id
    assert await provider.neighbor(ProviderCtx(sn="SN-другой"), None, None, 1) is None  # без очереди


async def test_provider_prev_after_next_and_prefetch_returns_previous_track(provider):
    """Позицию очереди двигает только подтверждённый старт трека (recoverCommit → commit):
    «дальше» и предзагрузка @@mm_stack_engine_get_next её не сдвигают — «предыдущий»
    возвращает прошлый трек, а не играющий."""
    queue = [{"id": f"q{i}", "title": f"Q{i}", "stream": {"url": f"u{i}", "format": "mp3"}}
             for i in range(3)]
    provider.STATE["SN-1"] = {"queue": queue, "current": 0, "saved_ts": 0}
    ctx = ProviderCtx()
    assert (await provider.neighbor(ctx, "", None, 1))["id"] == "q1"  # «дальше»
    assert await provider.commit(ctx, "q1") is True  # колонка подтвердила старт
    assert (await provider.neighbor(ctx, "", None, 1))["id"] == "q2"  # предзагрузка get_next
    assert (await provider.neighbor(ctx, "", None, -1))["id"] == "q0"  # «предыдущий» — прошлый, а не играющий q1
    assert await provider.commit(ctx, "иное") is None  # чужой трек позицию не двигает
    assert await provider.commit(ProviderCtx(sn="SN-другой"), "q0") is None  # очереди нет


async def test_provider_rate_not_supported(provider):
    assert await provider.rate(ProviderCtx(), "q0", True) is None


async def test_provider_empty_query_plays_from_cache(provider):
    """Кеш прошлых поисков: вся очередь, случайный start, позиция сохраняется."""
    queue = [{"id": f"c{i}", "title": f"C{i}", "stream": {"url": f"u{i}", "format": "mp3"}}
             for i in range(4)]
    provider.STATE["SN-1"] = {"queue": queue, "current": 0, "saved_ts": time.time()}
    result = await provider.search(ProviderCtx(), "", "music")
    assert result["context"] == {"type": "cache"}
    assert result["tracks"] == queue
    assert 0 <= result["start"] < len(queue)
    assert provider.STATE["SN-1"]["current"] == result["start"]


# --- интеграционные: внешнее расширение на настоящем менеджере ---------------------------


def spotify_ext_dir():
    """Папка расширения: ALICA_SPOTIFY_EXT или соседний alice-emulator-spotify."""
    override = os.environ.get("ALICA_SPOTIFY_EXT")
    if override and (Path(override) / "extension.json").is_file():
        return Path(override)
    candidate = Path(__file__).resolve().parents[1].parent / "alice-emulator-spotify"
    return candidate if (candidate / "extension.json").is_file() else None


async def say(runtime, text, route_mode="emulate"):
    """Реплика колонке в формате voice.py (как в test_music_provider)."""
    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dlg", "sn": "SN-M",
                     "text": text},
           "clock": {"now": datetime.datetime.now()}, "sequence": 1,
           "route_mode": route_mode, "player_owner": None,
           **runtime._scenario_context(sn="SN-M")}
    return await runtime.dialog.handle_text(text, ctx, "dlg", None, deliver=False)


def service_ctx(runtime, event_name, route_mode="emulate"):
    """Контекст колбэка @@mm_stack_engine_get_next (как собирает voice.py)."""
    return {"input": {"message_id": "msg-1", "request_id": "req-1", "dialog_id": "dlg-1",
                      "sn": "SN-M", "text": "", "event_name": event_name,
                      "callback": "", "purpose": "", "frame": ""},
            "clock": {"now": datetime.datetime.now()}, "sequence": 1,
            "route_mode": route_mode, "player_owner": None,
            **runtime._scenario_context(sn="SN-M")}


def _reply_names(message):
    return [d.Name for d in message.Event.AliceResponse.Response.Directives]


def _reply_directive(message, name):
    return next(d for d in message.Event.AliceResponse.Response.Directives if d.Name == name)


def _reply_speech(message):
    alice = message.Event.AliceResponse
    return alice.VoiceResponse.OutputSpeech.Text if alice.HasField("VoiceResponse") else ""


def _decoded(message, name, proto):
    raw = _reply_directive(message, name).PayloadRaw
    parsed = load_type(schemas_dir(), station_file(), proto)()
    parsed.ParseFromString(bytes(raw))
    return json_format.MessageToDict(parsed, preserving_proto_field_name=True)


def _payload_of(message, name):
    return json_format.MessageToDict(_reply_directive(message, name).Payload,
                                     preserving_proto_field_name=True)


def _entity_ids(message):
    queue = _decoded(message, "apply_player_playback_queue_directive", QUEUE_PROTO)
    return [e["PlaybackEntity"]["TrackEntity"]["Track"]["Id"]
            for e in queue["MusicPlatformQueue"]["Entities"]]


@pytest.fixture()
def ext_dir():
    path = spotify_ext_dir()
    if path is None:
        pytest.skip("Расширение alice-emulator-spotify не найдено "
                    "(ALICA_SPOTIFY_EXT или соседняя папка)")
    return path


@pytest.fixture()
async def spotify_runtime(tmp_path, ext_dir, monkeypatch):
    """Runtime с включённым внешним расширением «Spotify» (локальная папка) и подменным
    HTTP вместо настоящего Spotify; встроенный builtin.spotify включён рядом — выбор
    внешнего делается явно (settings["providers"]["music"])."""
    runtime = Runtime(tmp_path)
    runner = None
    try:
        # соседний репозиторий не трогаем: работаем с копией в своей tmp —
        # установленная копия получает объявление и файл справочника фраз
        import shutil
        ext_copy = tmp_path / "spotify-ext"
        shutil.copytree(ext_dir, ext_copy, ignore=shutil.ignore_patterns(".git", "tests", "pytest.ini"))
        install = await runtime.extensions.install({"url": str(ext_copy)}, confirm=True)
        assert install["installed"] == "spotify" and install["class"] == "code"
        await runtime.extensions.enable("spotify")
        await runtime.extensions.set_values("spotify", dict(KEYS))
        runtime.settings["providers"]["music"] = "spotify"  # авто выбрал бы builtin.spotify
        # фразы музыки переехали в справочник плагина: установленной копии дописываем
        # объявление и файл (соседний репозиторий получит их своей поставкой)
        installed = Path(runtime.extensions.loaded["spotify"]["dir"])
        manifest = json.loads((installed / "extension.json").read_text(encoding="utf-8"))
        manifest["contributes"] = {"providers": [{"kind": "music", "title": "Музыка",
                                                  "module": "music/spotify.py",
                                                  "phrases": "phrases/music.json"}]}
        (installed / "extension.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        phrases_dir = installed / "phrases"
        phrases_dir.mkdir(exist_ok=True)
        (phrases_dir / "music.json").write_text(json.dumps({
            "entries": [
                {"id": "play", "sentences": ["включи музыку [{query}]",
                                             "(включи|поставь|запусти) музыку [{query}]",
                                             "давай послушаем музыку [{query}]",
                                             "(включи|поставь) (песню|трек) {query}"],
                 "handler": "core:media.music"},
                {"id": "podcast", "sentences": ["(включи|поставь) (подкаст|подкасты) [{query}]"],
                 "handler": "core:media.music"},
                {"id": "search", "sentences": ["(включи|запусти|поставь|послушай) {query}"],
                 "handler": "core:media.music", "search": True}]},
            ensure_ascii=False), encoding="utf-8")
        # встроенные поставщики выключены: записи справочника — только внешнего
        # плагина (их мир — tests/test_music_provider.py, интеграции — здесь)
        state = runtime.settings.setdefault("extensions", {})
        for ext_id in list(state.get("enabled") or {}):
            if ext_id.startswith("builtin."):
                state["enabled"][ext_id] = False
        runtime.extensions.load_enabled()

        fake = FakeSpotify()
        app = web.Application()
        app.router.add_post("/api/token", fake._token)
        app.router.add_get("/v1/search", fake._search)
        app.router.add_get("/v1/tracks/{tid}", fake._track)
        app.router.add_get("/embed/track/{tid}", fake._embed)
        runner = web.AppRunner(app)
        await runner.setup()
        port = unused_port()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        base = f"http://127.0.0.1:{port}"

        provider = music.selected(runtime.settings)
        assert provider["ext_id"] == "spotify"
        module = provider["search"].__globals__  # константы модуля — точка подмены
        monkeypatch.setitem(module, "ACCOUNTS_URL", base + "/api/token")
        monkeypatch.setitem(module, "SEARCH_URL", base + "/v1/search")
        monkeypatch.setitem(module, "TRACK_URL", base + "/v1/tracks/")
        monkeypatch.setitem(module, "EMBED_URL", base + "/embed/track/{}")
        module["STATE"].clear()  # очередь/токены поставщика — с чистого листа
        module["TOKENS"].clear()
        yield runtime, fake
    finally:
        if runner is not None:
            await runner.cleanup()
        await runtime.close()


async def test_extension_installed_from_local_folder(spotify_runtime):
    """install({"url": папка}, confirm=True) валидирует манифест настоящим менеджером,
    общий реестр видов собирает поставщика (выбор — settings["providers"]["music"])."""
    runtime, _ = spotify_runtime
    assert runtime.extensions.loaded["spotify"]["manifest"]["version"] == "1.0.0"
    assert runtime.extensions.loaded["spotify"]["manifest"]["author"] == "Angell Fear"
    assert providers.PROVIDERS["music"]["spotify"]["title"] == "Spotify"
    assert providers.PROVIDERS["music"]["spotify"]["origin"] == "extension"
    # встроенный — рядом (в этом мире он выключен: объявление остаётся, поставщика
    # собирает включённый плагин; вместе они в tests/test_providers.py)
    from pathlib import Path as _Path
    import server_emulator as _pkg
    manifest = json.loads(_Path(_pkg.__file__).parent.joinpath(
        "plugins/spotify/extension.json").read_text(encoding="utf-8"))
    assert manifest["contributes"]["providers"][0]["kind"] == "music"  # встроенный объявлен
    assert music.selected(runtime.settings)["ext_id"] == "spotify"
    values = runtime.extensions.values("spotify")
    assert values["client_id"] == "test-id" and values["market"] == "RU"


async def test_search_plays_full_widget_stack(spotify_runtime):
    """«включи музыку <что-то>»: token → search → топ-5 с preview_url, играет первый;
    audio_play несёт прямую ссылку p.scdn.co (плеер колонки играет её)."""
    runtime, fake = spotify_runtime
    report = await say(runtime, "включи музыку тестовый исполнитель")
    assert report["handled"], report

    message = report["messages"][0]
    assert _reply_names(message) == [
        "tts_play_placeholder", "draw_animation_directive", "show_audio_player_screen_directive",
        "apply_player_playback_queue_directive", "set_audio_player_sequence_state_directive",
        "audio_play"]
    assert _reply_speech(message) == "Включаю Трек 1."
    assert _entity_ids(message) == [f"sp{i}" for i in range(1, 6)]
    screen = _decoded(message, "show_audio_player_screen_directive", SCREEN_PROTO)
    assert screen == {"TrackId": "sp1"}
    state = _decoded(message, "set_audio_player_sequence_state_directive", SEQSTATE_PROTO)
    meta = state["SequenceState"]["MetaStorage"]["ProviderTrackMeta"]["MUSIC_PROVIDER_UNSPECIFIED"]["TrackMeta"]
    assert meta["sp1"]["title"] == "Трек 1"
    assert meta["sp1"]["artist_ids"] == ["Артист"]
    play = _payload_of(message, "audio_play")
    assert play["stream"]["url"] == "https://p.scdn.co/mp3-preview/prev1"
    assert play["stream"]["format"] == "MP3" and play["stream"]["type"] == "Track"
    assert play["stream"]["duration_ms"] == 30001
    assert play["metadata"]["title"] == "Трек 1" and play["metadata"]["subtitle"] == "Артист"
    assert play["metadata"]["art_image_url"] == "https://image-cdn.spotifycdn.com/img1"
    assert play["screen_type"] == "Music"
    # подменный Spotify получил ровно запрос поставщика
    [(auth, body)] = fake.token_calls
    assert auth.startswith("Basic ") and "grant_type=client_credentials" in body
    assert fake.search_headers[0]["Authorization"] == "Bearer stub-token"
    assert fake.search_calls[0]["q"] == "тестовый исполнитель"
    assert fake.search_calls[0]["market"] == "RU"
    assert fake.search_calls[0]["limit"] == "10" and fake.search_calls[0]["type"] == "track"


async def test_next_callback_serves_next_track_from_queue(spotify_runtime):
    """Колбэк @@mm_stack_engine_get_next («дальше») после поиска: очередь поставщика
    переживает запрос (память модуля), второй запрос играет sp2."""
    runtime, fake = spotify_runtime
    await say(runtime, "включи музыку тестовый исполнитель")
    sent = []

    async def send(data):
        sent.append(data)

    assert callback_route("@@mm_stack_engine_get_next", {}) == "music.stack_next"
    report = await runtime.dialog.handle_event("music.stack_next",
                                               service_ctx(runtime, "@@mm_stack_engine_get_next"),
                                               "dlg-1", send)
    assert report["handled"], report
    [message] = [SERVER.FromString(raw[4:]) for raw in sent]
    assert _reply_names(message) == ["tts_play_placeholder",
                                     "apply_player_playback_queue_directive",
                                     "set_audio_player_sequence_state_directive", "audio_play"]
    play = _payload_of(message, "audio_play")
    assert play["stream"]["url"] == "https://p.scdn.co/mp3-preview/prev2"


async def test_empty_phrase_plays_random_track_from_cache(spotify_runtime):
    """«включи музыку» без запроса: случайный трек из кеша прошлых поисков, очередь та же."""
    runtime, fake = spotify_runtime
    await say(runtime, "включи музыку тестовый исполнитель")
    report = await say(runtime, "включи музыку")
    assert report["handled"]
    message = report["messages"][0]
    assert _entity_ids(message) == [f"sp{i}" for i in range(1, 6)]  # очередь из кеша
    play = _payload_of(message, "audio_play")
    assert play["stream"]["url"] in {f"https://p.scdn.co/mp3-preview/prev{i}" for i in range(1, 6)}
    assert len(fake.search_calls) == 1  # повторного поиска не было


async def test_without_keys_honest_note(spotify_runtime):
    """Ключей нет: поиск по строке — честный отказ, текст note произносится
    (ядро search передаёт note, сценарий media.music отвечает им)."""
    runtime, fake = spotify_runtime
    await runtime.extensions.set_values("spotify", {"client_id": "", "client_secret": ""})
    report = await say(runtime, "включи музыку тестовый исполнитель")
    assert report["handled"]
    message = report["messages"][0]
    assert _reply_names(message) == ["tts_play_placeholder"]  # директив плеера нет
    speech = _reply_speech(message)
    assert "client_id" in speech and "client_secret" in speech
    assert fake.token_calls == [] and fake.search_calls == []  # к Spotify не ходили


async def test_track_link_without_keys_resolves_via_embed(spotify_runtime):
    """«включи музыку <ссылка на трек>» без ключей: открытый embed отдаёт превью —
    играет точный трек, очередь из одного элемента."""
    runtime, fake = spotify_runtime
    await runtime.extensions.set_values("spotify", {"client_id": "", "client_secret": ""})
    report = await say(runtime, f"включи музыку https://open.spotify.com/track/{EMBED_ID}")
    assert report["handled"], report
    message = report["messages"][0]
    assert _reply_speech(message) == "Включаю Mr. Brightside."
    assert fake.embed_calls == [EMBED_ID] and fake.token_calls == []
    play = _payload_of(message, "audio_play")
    assert play["stream"]["url"] == "https://p.scdn.co/mp3-preview/embedprev"
    assert play["metadata"]["title"] == "Mr. Brightside"
    assert play["metadata"]["subtitle"] == "The Killers"
