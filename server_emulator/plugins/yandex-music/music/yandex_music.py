"""Яндекс Музыка — встроенный поставщик музыки (вид music, интерфейс ядра).

Что играет: **полные треки** — прямые ссылки storage.yandex.net из
`api.music.yandex.net/tracks/<id>/download-info` (mp3 без DRM; построение ссылки
`url + "3/" + sign` по схеме download-info). Директивы (стек виджета) собирает ядро
server_emulator/music.py — здесь только поиск, очередь и навигация.

Авторизация: **OAuth-токен аккаунта не вводится** — его захватывает эмулятор из реальных
обращений колонки к Яндексу (заголовок `Authorization: OAuth …` на HTTP и
`X-UPRX-OAUTH-TOKEN` в WS-handshake; диспетчер/runtime вызывают `music.note_token`) и
хранит в БД состояния (meta `yandex_oauth`). Ядро передаёт токен поставщику как
системное значение вызова (`captured_oauth` в ctx.extension) — по признаку сервиса
`PROVIDER["service"] = "yandex-music"`, а не по id: встроенный (builtin.yandex-music)
и внешний (`yandex-music`) поставщики — разные записи реестра с одним сервисом.
До первого обращения колонки — честный отказ с объяснением.

Поиск (search(ctx, query, kind)): GET api.music.yandex.net/search?type=track&text=…
→ result.tracks.items → Track (id/title/artist/album/duration_ms/cover), топ-5 (ядро
режет очередь). kind (tales/podcast/audiobook) — тем же type=track.

neighbor(ctx, track_id, context, step): очередь — память модуля по владельцу (как у
встроенного Spotify): «включи музыку» и колбэк «дальше» — разные запросы; step ±1 по
сохранённому списку. rate — None (лайки Яндекса через захват
колбэков колонки здесь не поддержаны).

Сеть — стандартная библиотека (urllib.request); URL сервисов — константы модуля
(SEARCH_URL/DOWNLOAD_INFO): тесты подменяют их локальным подменным HTTP, настоящий
Яндекс из автотестов не вызывается.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
import urllib.parse
import urllib.request

PROVIDER = {"id": "builtin.yandex-music", "title": "Яндекс Музыка",
            "service": "yandex-music"}

QUEUE_LIMIT = 5
SEARCH_URL = "https://api.music.yandex.net/search"
DOWNLOAD_INFO = "https://api.music.yandex.net/tracks/{}/download-info"

STATE = {}  # владелец → {"queue": [Track], "current": int}

NOTE_NO_TOKEN = ("Токен Яндекс.Музыки ещё не захвачен: дождитесь обращения колонки к Яндексу "
                 "(или скажите колонке любую фразу) и повторите.")
NOTE_SEARCH_FAIL = "Не удалось выполнить поиск в Яндекс.Музыке."
NOTE_NO_PLAYABLE = "У этого трека нет проигрываемой версии."
NOTE_WHAT = "Что включить?"


class MusicError(Exception):
    """Сбой сети/ответа API → note пользователю (текст — для реплики)."""


def _owner(ctx):
    return str((getattr(ctx, "device", None) or {}).get("sn") or getattr(ctx, "sn", "") or "owner")


def _captured_token(ctx):
    """OAuth-токен аккаунта: ядро инжектирует захваченное в ExtensionContext вызова
    (captured_oauth) — стандартный доступ поставщика к настройкам вызова."""
    ext = getattr(ctx, "extension", None)
    return str(ext.get("captured_oauth") or "") if ext is not None else ""


def _api(url, token, timeout=15.0):
    """GET c Authorization: OAuth → разобранный JSON; не-200/битый JSON → MusicError."""
    req = urllib.request.Request(url, headers={"Authorization": "OAuth " + token,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()

    def work():
        return json.loads(raw.decode("utf-8"))

    try:
        return asyncio.get_running_loop().run_in_executor(None, work)
    except RuntimeError:
        return work()


def _track_meta(item):
    """Элемент search/download-info → Track (контракт ядра: stream{url, format})."""
    cover = str(item.get("ogImage") or item.get("coverUri") or "")
    if "%@" in cover:
        cover = cover.replace("%@", "200x200")
    return {"id": str(item.get("id") or ""),
            "title": str(item.get("title") or "").strip(),
            "artist": ", ".join(a.get("name", "") for a in item.get("artists") or []),
            "album": ", ".join(a.get("title", "") for a in item.get("albums") or []),
            "duration_ms": int(item.get("durationMs") or 0),
            "cover_url": cover,
            "stream": {"url": "", "format": "mp3"}}


def _search(query, token, market):
    params = urllib.parse.urlencode({"type": "track", "text": query, "market": market or "ru"})
    return _api(f"{SEARCH_URL}?{params}", token)


def _download_link(track_id, token):
    """download-info трека → прямая ссылка mp3 (url + "3/" + sign, схема API)."""
    data = _api(DOWNLOAD_INFO.format(track_id), token)
    items = ((data.get("result") or {}).get("items") if isinstance(data, dict) else None) or []
    for item in items:
        if str(item.get("codec") or item.get("encoder") or "").lower() != "mp3":
            continue
        url, sign = str(item.get("url") or ""), str(item.get("sign") or "")
        if url and sign:
            return url + "3/" + sign
    raise MusicError(NOTE_NO_PLAYABLE)


def _with_stream(track, token):
    """Track с добранной ссылкой потока (stream.url — форма контракта ядра)."""
    track = dict(track)
    track["stream"] = dict(track.get("stream") or {"format": "mp3"})
    track["stream"]["url"] = _download_link(track["id"], token)
    track["stream"]["format"] = "mp3"
    return track


def _cached_queue(owner):
    entry = STATE.get(owner)
    return entry if entry and entry.get("queue") else None


def _save_queue(owner, tracks, start=0):
    entry = {"queue": tracks, "current": int(start), "ts": time.time()}
    STATE[owner] = entry
    return entry


async def search(ctx, query, kind="music"):
    """Поиск треков (интерфейс ядра) или честный отказ {"tracks": [], "note"}.
    Тело целиком в потоке: HTTP блокирует цикл, а мок-сервер тестов живёт на нём."""
    return await asyncio.to_thread(_search_sync, ctx, str(query or ""), str(kind or "music"))


def _search_sync(ctx, query, kind):
    token = _captured_token(ctx)
    if not token:
        return {"tracks": [], "note": NOTE_NO_TOKEN}
    owner = _owner(ctx)

    if not query:  # «включи музыку» без запроса — случайный трек из кеша прошлых поисков
        cached = _cached_queue(owner)
        if cached:
            start = random.randrange(len(cached["queue"]))
            cached["current"] = start
            return {"tracks": cached["queue"], "start": start, "context": {"type": "cache"}}
        return {"tracks": [], "note": NOTE_WHAT}

    try:
        result = _search(query, token, _market(ctx))
    except MusicError as exc:
        return {"tracks": [], "note": str(exc)}
    except Exception as exc:
        return {"tracks": [], "note": f"{NOTE_SEARCH_FAIL} ({type(exc).__name__})"}
    items = ((result.get("result") or {}).get("tracks") or {}).get("items") if isinstance(result, dict) else None
    tracks = [_track_meta(item) for item in (items or []) if item.get("id")]
    tracks = [t for t in tracks if t["title"]]
    if not tracks:
        return {"tracks": []}  # пусто: ядро отвечает «не нашла» (без note)
    tracks = tracks[:QUEUE_LIMIT]
    try:  # ссылку первого трека добираем сразу (сбой — note)
        tracks[0] = _with_stream(tracks[0], token)
    except MusicError as exc:
        return {"tracks": [], "note": str(exc)}
    _save_queue(owner, tracks, 0)
    return {"tracks": tracks, "start": 0, "context": {"type": "search"}}


def _market(ctx):
    settings = getattr(getattr(ctx, "extension", None), "settings", None) or {}
    return str(settings.get("market") or "ru")


async def neighbor(ctx, track_id, context, step):
    """Соседний трек очереди (интерфейс ядра): Track или None — дальше нечего."""
    return await asyncio.to_thread(_neighbor_sync, ctx, track_id, int(step))


def _neighbor_sync(ctx, track_id, step):
    token = _captured_token(ctx)
    if not token:
        return None
    entry = _cached_queue(_owner(ctx))
    if not entry:
        return None
    queue, current = entry["queue"], entry.get("current", 0)
    index = next((i for i, t in enumerate(queue) if str(t["id"]) == str(track_id)), current)
    target = index + int(step)
    if not 0 <= target < len(queue):
        return None
    entry["current"] = target
    track = queue[target]
    try:  # ссылку соседнего трека добираем при переходе
        track = _with_stream(track, token)
        entry["queue"][target] = track
    except MusicError:
        return None
    return track


async def rate(ctx, track_id, like):
    """Лайки Яндекса через захват колбэков колонки — здесь не поддержаны (честно None)."""
    return None
