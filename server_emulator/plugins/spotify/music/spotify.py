"""Spotify — встроенный поставщик музыки (вид music, интерфейс ядра).

Что играет: 30-секундные превью mp3 (128 kbps, p.scdn.co) — это единственный
контент Spotify, доступный вне их приложений (полные треки закрыты лицензиями и
DRM); плеер колонки играет такие прямые ссылки штатным audio_play (проверено на
колонке). Директивы (стек виджета) собирает ядро server_emulator/music.py —
здесь только поиск, очередь и навигация.

Поиск (search(ctx, query, kind)):
- ключи Web API заданы (client_id/client_secret в настройках плагина) —
  client credentials POST accounts.spotify.com/api/token (токен кешируется до
  истечения) и GET api.spotify.com/v1/search?type=track&market=...&limit=10:
  из результатов берутся треки с preview_url, топ-5 (QUEUE_LIMIT);
- в реплике ссылка/URI трека Spotify (open.spotify.com/track/<id>,
  spotify:track:<id>) — точный трек: GET /v1/tracks/<id> по ключам или, БЕЗ
  ключей, открытый embed open.spotify.com/embed/track/<id> — из его HTML
  (__NEXT_DATA__, стандартные urllib+json) берётся audioPreview.url, название,
  артист, длительность и обложка;
- ключей нет, запрос — строка: embed по строке искать не умеет, Web API без
  ключей не ищет — честный отказ {"tracks": [], "note": "..."} (ядро и сценарий
  media.music произносят note пользователю);
- запроса нет («включи музыку»): случайный трек из кеша прошлых поисков —
  вся очередь с случайным start; кеша нет — вопрос «что включить?».

Очередь — память модуля (STATE, ключ — владелец ctx.sn): «включи музыку» и колбэк
«дальше» (@@mm_stack_engine_get_next) — разные запросы; переживать рестарт очереди
не нужно: перезапуск сервера завершает сессию прослушивания. TTL кеша — неделя
(превью-URL живут долго, но не вечно).

rate — None: лайков без Premium API нет (честно, интерфейс ядра).

Сеть — стандартная библиотека (urllib.request в asyncio.to_thread, json):
python_packages плагина пуст. URL сервисов — константы модуля
(ACCOUNTS_URL/SEARCH_URL/TRACK_URL/EMBED_URL): тесты подменяют их на локальный
подменный HTTP-сервер, настоящий Spotify из автотестов не вызывается.

Настройки — ctx.extension (ExtensionContext, только чтение; строки env:ИМЯ
раскрыты менеджером): client_id, client_secret, market (по умолчанию RU).
Хостов сети касается сам код плагина: accounts/api/open.spotify.com — они
объявлены в permissions.network; потоки и обложки (p.scdn.co,
image-cdn.spotifycdn.com) колонка качает сама, эмулятор их не трогает.
"""
from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

PROVIDER = {"id": "builtin.spotify", "title": "Spotify"}

# URL сервисов — константы с возможностью подмены (тесты ставят локальный сервер)
ACCOUNTS_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
TRACK_URL = "https://api.spotify.com/v1/tracks/"
EMBED_URL = "https://open.spotify.com/embed/track/{}"

TIMEOUT_S = 10  # один запрос
FETCH_LIMIT = 10  # сколько запрашивать у поиска (часть результатов без превью)
QUEUE_LIMIT = 5  # очередь — топ-5 (MAX_QUEUE ядра)
QUEUE_TTL_S = 7 * 24 * 3600  # кеш поиска живёт неделю (превью не вечны)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) alice-emulator"

NOTE_NO_KEYS = ("Поиск в Spotify требует ключей Web API: задайте client_id и "
                "client_secret в настройках плагина «Spotify» "
                "(developer.spotify.com, раздел Dashboard).")
NOTE_WHAT = "Что включить?"
NOTE_NO_PREVIEW = "У этого трека нет 30-секундного превью — играть нечего."
NOTE_EMBED_FAIL = "Не удалось получить данные трека из Spotify."

# ссылки/URI трека: open.spotify.com/track/<id>, /intl-ru/track/, /embed/track/, spotify:track:<id>
_TRACK_ID = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z]{2,5}/)?(?:embed/)?track/|spotify:track:)([0-9A-Za-z]{22})")

STATE = {}  # владелец (ctx.sn) → {"queue": [Track], "current": int, "saved_ts": float}
TOKENS = {}  # client_id → {"value": str, "expires_ts": float} — client credentials кешируются


class SpotifyError(Exception):
    """Сбой Spotify: сообщение — готовый текст для пользователя (note)."""


# --- HTTP (stdlib urllib, без блокировки цикла событий) -------------------------------


def _http(url, data=None, headers=None):
    """Синхронный GET (data=None) или POST form (data=dict) — urllib.request."""
    payload = urllib.parse.urlencode(data).encode("utf-8") if data else None
    request = urllib.request.Request(url, data=payload,
                                     headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return response.read().decode("utf-8", "replace")


async def _request(url, data=None, headers=None):
    """HTTP в отдельном потоке — событийный цикл сервера ждёт, не блокируется."""
    try:
        return await asyncio.to_thread(_http, url, data, headers)
    except urllib.error.HTTPError as exc:  # subclasses URLError — раньше
        raise SpotifyError(f"HTTP {exc.code} от {urllib.parse.urlsplit(url).netloc}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SpotifyError(f"{urllib.parse.urlsplit(url).netloc} не отвечает "
                           f"({type(exc).__name__})") from exc


async def _json(url, data=None, headers=None):
    """HTTP + разбор JSON; не-JSON — SpotifyError."""
    body = await _request(url, data, headers)
    try:
        return json.loads(body)
    except ValueError as exc:
        raise SpotifyError(f"{urllib.parse.urlsplit(url).netloc}: ответ не JSON") from exc


# --- треки: приведение к контракту ядра (Track) ---------------------------------------


def _api_track(item):
    """item из Web API → Track (id/title/artist/album?/duration_ms/cover_url?/stream)
    или None — без preview_url трек не играет (и не показывается)."""
    if not isinstance(item, dict):
        return None
    preview = str(item.get("preview_url") or "").strip()
    track_id = str(item.get("id") or "").strip()
    title = str(item.get("name") or "").strip()
    if not (preview and track_id and title):
        return None
    artists = [str(a.get("name") or "").strip() for a in item.get("artists") or []
               if isinstance(a, dict)]
    album = item.get("album") if isinstance(item.get("album"), dict) else {}
    cover = ""
    for image in album.get("images") or []:
        if isinstance(image, dict) and image.get("url"):
            cover = str(image["url"])
            break
    track = {"id": track_id, "title": title, "artist": ", ".join(n for n in artists if n),
             "duration_ms": int(item.get("duration_ms") or 0),
             "stream": {"url": preview, "format": "mp3"}}
    if album.get("name"):
        track["album"] = str(album["name"])
    if cover:
        track["cover_url"] = cover
    return track


def _embed_entity(html):
    """Разобрать открытую embed-страницу трека: скрипт __NEXT_DATA__ (JSON) →
    props.pageProps.state.data.entity. Сбой разбора — SpotifyError."""
    try:
        marker = html.index("__NEXT_DATA__")
        start = html.index(">", marker) + 1
        end = html.index("</script>", start)
        data = json.loads(html[start:end])
        entity = ((data.get("props") or {}).get("pageProps") or {}).get("state") or {}
        entity = (entity.get("data") or {}).get("entity")
    except (ValueError, AttributeError) as exc:
        raise SpotifyError(NOTE_EMBED_FAIL) from exc
    return entity if isinstance(entity, dict) else {}


def _embed_track_from_entity(entity):
    """entity embed-страницы → Track или None (нет audioPreview/названия)."""
    if not isinstance(entity, dict):
        return None
    preview = str((entity.get("audioPreview") or {}).get("url") or "").strip()
    track_id = str(entity.get("id") or "").strip()
    title = str(entity.get("name") or entity.get("title") or "").strip()
    if not (preview and track_id and title):
        return None
    artists = [str(a.get("name") or "").strip() for a in entity.get("artists") or []
               if isinstance(a, dict)]
    images = (entity.get("visualIdentity") or {}).get("image") or []
    cover = ""
    for image in images:
        if isinstance(image, dict) and image.get("url"):
            cover = str(image["url"])
            break
    track = {"id": track_id, "title": title, "artist": ", ".join(n for n in artists if n),
             "duration_ms": int(entity.get("duration") or 0),
             "stream": {"url": preview, "format": "mp3"}}
    if cover:
        track["cover_url"] = cover
    return track


# --- Web API: токен и поиск -------------------------------------------------------------

async def _access_token(client_id, client_secret):
    """Токен client credentials; кеш до истечения (expires_in минус минута)."""
    cached = TOKENS.get(client_id)
    if cached and cached.get("expires_ts", 0) > time.time():
        return str(cached["value"])
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    try:
        data = await _json(ACCOUNTS_URL, data={"grant_type": "client_credentials"},
                           headers={"Authorization": "Basic " + auth})
        token = str((data or {}).get("access_token") or "").strip()
    except SpotifyError as exc:
        raise SpotifyError(f"Не удалось войти в Spotify Web API ({exc}). Проверьте "
                           "client_id и client_secret в настройках плагина.") from exc
    if not token:
        raise SpotifyError("Не удалось войти в Spotify Web API. Проверьте client_id "
                           "и client_secret в настройках плагина.")
    lifetime = int((data or {}).get("expires_in") or 3600)
    TOKENS[client_id] = {"value": token, "expires_ts": time.time() + max(60, lifetime - 60)}
    return token


async def _search_tracks(client_id, client_secret, query, market):
    """GET /v1/search?type=track → [Track] с preview_url, топ QUEUE_LIMIT."""
    token = await _access_token(client_id, client_secret)
    params = {"q": query, "type": "track", "limit": str(FETCH_LIMIT)}
    if market:
        params["market"] = market
    try:
        data = await _json(SEARCH_URL + "?" + urllib.parse.urlencode(params),
                           headers={"Authorization": "Bearer " + token})
    except SpotifyError as exc:
        raise SpotifyError(f"Spotify недоступен ({exc}). Попробуйте позже.") from exc
    items = ((data or {}).get("tracks") or {}).get("items") or []
    return [track for track in (_api_track(item) for item in items) if track][:QUEUE_LIMIT]


async def _track_by_id(client_id, client_secret, track_id, market):
    """GET /v1/tracks/<id> → Track или None (нет превью)."""
    token = await _access_token(client_id, client_secret)
    url = TRACK_URL + urllib.parse.quote(track_id)
    if market:
        url += "?" + urllib.parse.urlencode({"market": market})
    try:
        data = await _json(url, headers={"Authorization": "Bearer " + token})
    except SpotifyError as exc:
        raise SpotifyError(f"Spotify недоступен ({exc}). Попробуйте позже.") from exc
    return _api_track(data if isinstance(data, dict) else {})


async def _embed_track(track_id):
    """Открытый embed-трек без ключей: GET /embed/track/<id> → Track или None."""
    try:
        html = await _request(EMBED_URL.format(urllib.parse.quote(track_id)))
    except SpotifyError as exc:
        raise SpotifyError(f"Spotify недоступен ({exc}). Попробуйте позже.") from exc
    return _embed_track_from_entity(_embed_entity(html))


# --- очередь (память модуля) и интерфейс поставщика -------------------------------------


def _owner(ctx):
    """Ключ владельца очереди: SN колонки (одиночный пользователь эмулятора)."""
    return str(getattr(ctx, "sn", "") or "default")


def _settings(ctx):
    """(client_id, client_secret, market) из ExtensionContext (env: уже раскрыты)."""
    ext = getattr(ctx, "extension", None)

    def value(key):
        return str(ext.get(key) or "").strip() if ext is not None else ""

    return value("client_id"), value("client_secret"), value("market")


def _save_queue(owner, tracks, start):
    """Очередь поиска → память модуля; результат search для ядра."""
    queue = list(tracks)[:QUEUE_LIMIT]
    start = max(0, min(int(start), len(queue) - 1)) if queue else 0
    STATE[owner] = {"queue": queue, "current": start, "saved_ts": time.time()}
    return {"tracks": queue, "start": start, "context": {"type": "search"}}


def _cached_queue(owner):
    """Кеш прошлых поисков (неделя) — для «включи музыку» без запроса."""
    state = STATE.get(owner)
    if not state or time.time() - state.get("saved_ts", 0) > QUEUE_TTL_S:
        return None
    return state if state.get("queue") else None


def _note(text):
    """Честный отказ с объяснением: ядро (search) передаёт note сценарию, сценарий
    произносит текст пользователю."""
    return {"tracks": [], "note": str(text)}


def _track_id_from_query(query):
    """id трека из ссылки/URI в реплике или None."""
    match = _TRACK_ID.search(query)
    return match.group(1) if match else None


async def search(ctx, query, kind="music"):
    """Поиск треков (интерфейс ядра): {"tracks": [Track], "start", "context"}
    или честный отказ {"tracks": [], "note"}. kind (tales/podcast/audiobook)
    ищется тем же type=track: у Spotify превью есть только у музыкальных треков."""
    query = str(query or "").strip()
    owner = _owner(ctx)
    client_id, client_secret, market = _settings(ctx)
    keys = bool(client_id and client_secret)
    track_id = _track_id_from_query(query)

    if not query:  # «включи музыку» без запроса — случайный трек из кеша прошлых поисков
        cached = _cached_queue(owner)
        if cached:
            start = random.randrange(len(cached["queue"]))
            cached["current"] = start
            return {"tracks": cached["queue"], "start": start, "context": {"type": "cache"}}
        return _note(NOTE_WHAT)

    if not keys and not track_id:
        return _note(NOTE_NO_KEYS)  # embed по строке не ищет, Web API без ключей не ищет

    if track_id:  # ссылка/URI: точный трек — Web API по ключам или embed без ключей
        try:
            track = await (_track_by_id(client_id, client_secret, track_id, market) if keys
                           else _embed_track(track_id))
        except SpotifyError as exc:
            return _note(exc)
        if track is None:
            return _note(NOTE_NO_PREVIEW)
        return _save_queue(owner, [track], 0)

    try:  # строка поиска — только Web API по client credentials
        tracks = await _search_tracks(client_id, client_secret, query, market)
    except SpotifyError as exc:
        return _note(exc)
    if not tracks:
        return {"tracks": []}  # пусто: ядро отвечает «не нашла» (без note)
    return _save_queue(owner, tracks, 0)


async def neighbor(ctx, track_id, context, step):
    """Соседний трек очереди (интерфейс ядра): Track или None — дальше нечего."""
    state = STATE.get(_owner(ctx))
    if not state:
        return None
    queue = [track for track in state.get("queue") or [] if isinstance(track, dict)]
    if not queue:
        return None
    current = int(state.get("current") or 0)
    if track_id:  # колонка сообщает играющий трек (Title/SubTitle) — позиция по нему
        found = next((i for i, track in enumerate(queue)
                      if str(track.get("id")) == str(track_id)), None)
        if found is not None:
            current = found
    step = int(step or 0)
    index = current + (0 if step == 0 else 1 if step > 0 else -1)  # 0 — текущий (продолжение)
    if not 0 <= index < len(queue):
        return None
    state["current"] = index
    return queue[index]


async def rate(ctx, track_id, like):
    """Лайк — не поддерживается: API оценок без Premium нет (честно None)."""
    return None
