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
- запроса нет («включи музыку»): плейлист по умолчанию — настройка default_playlist
  (id, ссылка open.spotify.com/playlist/<id> или spotify:playlist:<id>; нужны ключи
  Web API, плейлист публичный): GET /v1/playlists/<id>/tracks, очередь — первые
  QUEUE_LIMIT треков с превью в порядке плейлиста, играет первый. Плейлиста нет (не задан,
  пуст или без превью) — случайный трек из кеша прошлых поисков (вся очередь со
  случайным start); кеша нет — вопрос «что включить?». Любимые треки аккаунта
  (user-library-read) не используются: пользовательский токен поставщику не передаётся.

Очередь — память модуля (STATE, ключ — владелец ctx.sn): «включи музыку» и колбэк
«дальше» (@@mm_stack_engine_get_next) — разные запросы. Очередь и позиция переживают
перезапуск сервера: дубликат в ctx.data сценария media.music (ключ «music_queue/spotify»,
хранилище state/, по владельцу-пользователю/колонке) при каждом изменении, гидратация
при первом обращении после рестарта — «плей» продолжает последний трек, «дальше/назад»
идут от него. TTL кеша — неделя (превью-URL живут долго, но не вечно).

rate — None: лайков без Premium API нет (честно, интерфейс ядра).

Вход аккаунта (login_start/login_callback/login_status — точки login поставщика,
кнопка «Войти в Spotify» в UI): Authorization Code + PKCE через authlib
(OAuth2Session), redirect — loopback `http://127.0.0.1:<порт>` (Spotify разрешает
любой порт; владелец регистрирует `http://127.0.0.1` в Dashboard без порта; кнопка
работает при входе в UI по 127.0.0.1). Токены (access/refresh/expires_at) возвращает
вызывающему (API-домен providers) для персистентности в state/ (meta) — в браузер и
журнал не идут, наружу только статус. refresh-токен обновляется сам: login_status
обновляет истёкший access через refresh_token.

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

from authlib.common.security import generate_token
from authlib.integrations.requests_client import OAuth2Session

PROVIDER = {"id": "builtin.spotify", "title": "Spotify"}

# URL сервисов — константы с возможностью подмены (тесты ставят локальный сервер)
ACCOUNTS_URL = "https://accounts.spotify.com/api/token"
PROFILE_URL = "https://api.spotify.com/v1/me"
SEARCH_URL = "https://api.spotify.com/v1/search"
TRACK_URL = "https://api.spotify.com/v1/tracks/"
EMBED_URL = "https://open.spotify.com/embed/track/{}"
PLAYLIST_URL = "https://api.spotify.com/v1/playlists/{}/tracks"

TIMEOUT_S = 10  # один запрос
FETCH_LIMIT = 10  # сколько запрашивать у поиска (часть результатов без превью)
QUEUE_LIMIT = 30  # окно очереди (MAX_QUEUE ядра)
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
PERSIST_KEY = "music_queue/spotify"  # ctx.data сценария media.music: очередь переживает перезапуск


class SpotifyError(Exception):
    """Сбой Spotify: сообщение — готовый текст для пользователя (note)."""


def _persist(ctx, owner):
    """Дубликат очереди и позиции в ctx.data (state/, по владельцу) — переживает
    перезапуск сервера. Нет ctx.data (тесты без двойника) или dry-run — не пишем."""
    state = STATE.get(owner)
    data = getattr(ctx, "data", None)
    if state is None or data is None:
        return
    try:
        data.set(PERSIST_KEY, {"queue": state["queue"], "current": state.get("current", 0),
                               "saved_ts": state.get("saved_ts")})
    except Exception:
        pass  # сбой записи не влияет на ответ колонке


def _hydrate(ctx, owner):
    """Очередь после перезапуска: память пуста — восстанавливаем из ctx.data
    (последний трек и позиция: «плей» продолжает, «дальше/назад» идут от него)."""
    if _cached_queue(owner) is not None:
        return
    data = getattr(ctx, "data", None)
    record = data.get(PERSIST_KEY) if data is not None else None
    if not isinstance(record, dict):
        return
    queue = [t for t in (record.get("queue") or [])
             if isinstance(t, dict) and str(t.get("id") or "").strip()]
    if not queue:
        return
    STATE[owner] = {"queue": queue, "current": min(int(record.get("current") or 0), len(queue) - 1),
                    "saved_ts": record.get("saved_ts") or 0}


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


def _save_queue(ctx, owner, tracks, start):
    """Очередь поиска → память модуля (+ дубликат в ctx.data); результат search для ядра."""
    queue = list(tracks)[:QUEUE_LIMIT]
    start = max(0, min(int(start), len(queue) - 1)) if queue else 0
    STATE[owner] = {"queue": queue, "current": start, "saved_ts": time.time()}
    _persist(ctx, owner)
    return {"tracks": queue, "start": start, "context": {"type": "search"}}


def _cached_queue(owner):
    """Кеш прошлых поисков (неделя) — для «включи музыку» без запроса."""
    state = STATE.get(owner)
    if not state or time.time() - state.get("saved_ts", 0) > QUEUE_TTL_S:
        return None
    return state if state.get("queue") else None


class _NoExt:
    @staticmethod
    def get(key, default=None):
        return default


def _note(text):
    """Честный отказ с объяснением: ядро (search) передаёт note сценарию, сценарий
    произносит текст пользователю."""
    return {"tracks": [], "note": str(text)}


def _playlist_id(value):
    """id плейлиста из настройки: id, ссылка open.spotify.com/playlist/<id> или spotify:playlist:<id>."""
    match = re.search(r"(?:playlist/|spotify:playlist:)([0-9A-Za-z]{22})", str(value or ""))
    if match:
        return match.group(1)
    value = str(value or "").strip()
    return value if re.fullmatch(r"[0-9A-Za-z]{22}", value) else ""


async def _playlist_tracks(client_id, client_secret, playlist_id, market):
    """GET /v1/playlists/<id>/tracks → [Track] с preview_url в порядке плейлиста, топ QUEUE_LIMIT."""
    token = await _access_token(client_id, client_secret)
    params = {"limit": str(FETCH_LIMIT * 3)}
    if market:
        params["market"] = market
    url = PLAYLIST_URL.format(urllib.parse.quote(playlist_id)) + "?" + urllib.parse.urlencode(params)
    try:
        data = await _json(url, headers={"Authorization": "Bearer " + token})
    except SpotifyError as exc:
        raise SpotifyError(f"Spotify недоступен ({exc}). Попробуйте позже.") from exc
    items = [(item or {}).get("track") for item in (data or {}).get("items") or [] if isinstance(item, dict)]
    return [track for track in (_api_track(item) for item in items) if track][:QUEUE_LIMIT]


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

    if not query:  # «включи музыку» без запроса — плейлист по умолчанию, иначе кеш прошлых поисков
        playlist_id = _playlist_id((getattr(ctx, "extension", None) or _NoExt()).get("default_playlist"))
        if keys and playlist_id:
            try:
                tracks = await _playlist_tracks(client_id, client_secret, playlist_id, market)
            except SpotifyError as exc:
                return _note(exc)
            if tracks:
                saved = _save_queue(ctx, owner, tracks, 0)
                saved["context"] = {"type": "default", "id": playlist_id}
                return saved
        _hydrate(ctx, owner)  # после перезапуска очередь живёт в ctx.data
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
        return _save_queue(ctx, owner, [track], 0)

    try:  # строка поиска — только Web API по client credentials
        tracks = await _search_tracks(client_id, client_secret, query, market)
    except SpotifyError as exc:
        return _note(exc)
    if not tracks:
        return {"tracks": []}  # пусто: ядро отвечает «не нашла» (без note)
    return _save_queue(ctx, owner, tracks, 0)


async def neighbor(ctx, track_id, context, step):
    """Соседний трек очереди (интерфейс ядра): Track или None — дальше нечего.
    Позицию очереди двигает только commit (recoverCommit — подтверждённый старт трека):
    предзагрузка @@mm_stack_engine_get_next и явные шаги её не сдвигают."""
    owner = _owner(ctx)
    _hydrate(ctx, owner)  # после перезапуска очередь живёт в ctx.data
    state = STATE.get(owner)
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
    return queue[index]


async def commit(ctx, track_id):
    """Сверка позиции очереди с подтверждённым стартом трека (интерфейс ядра, recoverCommit):
    чужого трека в очереди нет — позиция не меняется."""
    owner = _owner(ctx)
    _hydrate(ctx, owner)
    state = STATE.get(owner)
    if not state:
        return None
    queue = [track for track in state.get("queue") or [] if isinstance(track, dict)]
    index = next((i for i, track in enumerate(queue) if str(track.get("id")) == str(track_id)), None)
    if index is None:
        return None
    state["current"] = index
    _persist(ctx, owner)
    return True


async def rate(ctx, track_id, like):
    """Лайк — не поддерживается: API оценок без Premium нет (честно None)."""
    return None


# --- вход аккаунта: Authorization Code + PKCE (authlib) ---------------------------------

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
LOGIN_SCOPE = "user-read-private user-library-read"
LOGIN_TTL_S = 600  # владелец должен успеть войти; Spotify гасит code за ~10 минут

PENDING = {}  # state → {"client_id", "redirect_uri", "verifier", "ts"} — старт → колбэк

NOTE_LOGIN_NO_CLIENT = ("Для кнопки входа задайте Client ID в настройках плагина «Spotify» "
                        "(developer.spotify.com, Dashboard; redirect http://127.0.0.1 — "
                        "без порта).")
NOTE_LOGIN_EXPIRED = "Время на вход истекло — нажмите кнопку ещё раз."
NOTE_LOGIN_NO_CODE = "Spotify не вернул код входа."


def _login_client_id(ctx):
    """Client ID для точек login: вход получает ExtensionContext (настройки в get/settings),
    у сценарного ctx.extension — вложение. Оба вида контекста читаются одинаково."""
    ext = getattr(ctx, "extension", None)
    source = ctx if ext is None else ext
    return str((source or {}).get("client_id") or "").strip()


def _login_session(client_id, redirect_uri=None):
    """authlib-сессия для OAuth-потока (PKCE): S256-челлендж считается при старте."""
    return OAuth2Session(client_id, redirect_uri=redirect_uri, code_challenge_method="S256")


def _tokens_of(token):
    """OAuth2Token authlib → словарь токенов для персистентности (в state/, наружу нет)."""
    return {"access_token": str(token.get("access_token") or ""),
            "refresh_token": str(token.get("refresh_token") or ""),
            "token_type": str(token.get("token_type") or "Bearer"),
            "expires_at": time.time() + int(token.get("expires_in") or 3600)}


async def login_start(ctx, redirect_uri):
    """URL страницы входа Spotify (точка login): Authorization Code + PKCE.
    Заявка (state → client_id/redirect_uri/verifier) хранится в памяти модуля до колбэка.
    Без client_id — SpotifyError с готовым текстом (API отдаёт 400 с ним)."""
    client_id = _login_client_id(ctx)
    if not client_id:
        raise SpotifyError(NOTE_LOGIN_NO_CLIENT)
    session = _login_session(client_id, redirect_uri)
    verifier = generate_token(48)
    url, state = session.create_authorization_url(AUTHORIZE_URL, code_verifier=verifier,
                                                  scope=LOGIN_SCOPE)
    now = time.time()
    for key in [key for key, item in PENDING.items() if now - item["ts"] > LOGIN_TTL_S]:
        PENDING.pop(key, None)
    PENDING[state] = {"client_id": client_id, "redirect_uri": redirect_uri,
                      "verifier": verifier, "ts": now}
    return {"url": url, "state": state}


async def _login_profile(access_token):
    """Имя входа аккаунта Spotify (GET /me, display_name) — заголовок карточки
    аккаунта в реестре «Авторизации в сервисах». Сбой сети имя не портит: будет
    технический заголовок, вход всё равно состоялся."""
    try:
        profile = await _json(PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"})
    except SpotifyError:
        return ""
    return str(profile.get("display_name") or profile.get("email") or "")


async def login_callback(ctx, params):
    """Обмен кода на токены (точка login): params — query-параметры колбэка. Заявку ищет
    по state (authlib дополнительно валидирует state против authorization_response).
    Токены — в результате вызывающему (API пишет в state/); браузеру их не видно.
    Отказ Spotify (error=...) или потерянная заявка — authorized: False с текстом."""
    state = str(params.get("state") or "")
    pending = PENDING.pop(state, None)
    if not pending or time.time() - pending["ts"] > LOGIN_TTL_S:
        return {"authorized": False, "error": NOTE_LOGIN_EXPIRED}
    error = str(params.get("error") or "").strip()
    if error:
        return {"authorized": False, "error": f"Spotify отклонил вход ({error})."}
    if not str(params.get("code") or "").strip():
        return {"authorized": False, "error": NOTE_LOGIN_NO_CODE}

    session = _login_session(pending["client_id"], pending["redirect_uri"])
    response_url = pending["redirect_uri"] + "?" + urllib.parse.urlencode(params)

    def exchange():
        # authlib сам проверяет state против authorization_response и кладёт
        # code_verifier в тело запроса (PKCE); POST идёт через requests
        return session.fetch_token(ACCOUNTS_URL, state=state,
                                   authorization_response=response_url,
                                   code_verifier=pending["verifier"])

    try:
        token = await asyncio.to_thread(exchange)
    except SpotifyError:
        raise
    except Exception as exc:  # noqa: BLE001 — наружу готовый текст
        raise SpotifyError(f"Не удалось обменять код входа на токен "
                           f"({type(exc).__name__}: {str(exc)[:120]}).") from exc
    tokens = _tokens_of(token)
    if not tokens["access_token"]:
        return {"authorized": False, "error": "Spotify вернул пустой токен входа."}
    return {"authorized": True, "tokens": tokens,
            "login": await _login_profile(tokens["access_token"])}


async def login_status(ctx, tokens):
    """Статус входа (точка login): tokens — сохранённое из state/ (None — не входил).
    Истёкший access при живом refresh_token обновляется сам (authlib refresh_token);
    новый набор — в результате result['tokens'], вызывающий перезаписывает state/."""
    if not isinstance(tokens, dict) or not tokens.get("refresh_token"):
        return {"authorized": False}
    remaining = float(tokens.get("expires_at") or 0) - time.time()
    if remaining > 60:
        return {"authorized": True, "expires_in": int(remaining)}
    client_id = _login_client_id(ctx)
    if not client_id:
        return {"authorized": False, "error": NOTE_LOGIN_NO_CLIENT}

    def refresh():
        session = OAuth2Session(client_id)
        return session.refresh_token(ACCOUNTS_URL, refresh_token=str(tokens["refresh_token"]))

    try:
        token = await asyncio.to_thread(refresh)
    except SpotifyError:
        raise
    except Exception as exc:  # noqa: BLE001 — наружу готовый текст
        raise SpotifyError(f"Не удалось обновить токен входа Spotify "
                           f"({type(exc).__name__}: {str(exc)[:120]}).") from exc
    fresh = _tokens_of(token)
    if not fresh["access_token"]:
        return {"authorized": False, "error": "Spotify вернул пустой токен входа."}
    return {"authorized": True, "expires_in": int(fresh["expires_at"] - time.time()),
            "tokens": fresh}
