"""Яндекс Музыка — встроенный поставщик музыки (вид music, интерфейс ядра).

Клиент API — библиотека `yandex-music` (yandex-music-api): поиск, download-info и прямые
ссылки mp3 без DRM (`DownloadInfo.direct_link`, `get_direct_links=True`); колонке уходит
адрес после перенаправлений get-mp3 (её плеер не проходит 308). Директивы
(стек виджета) собирает ядро server_emulator/music.py — здесь только поиск, очередь и
навигация.

Авторизация: **OAuth-токен аккаунта не вводится** — его захватывает эмулятор из реальных
обращений колонки к Яндексу (заголовок `Authorization: OAuth …` на HTTP и
`X-UPRX-OAUTH-TOKEN` в WS-handshake; диспетчер/runtime вызывают `music.note_token`) и
хранит в БД состояния (meta `yandex_oauth`). Ядро передаёт токен поставщику как
системное значение вызова (`captured_oauth` в ctx.extension) — по признаку сервиса
`PROVIDER["service"] = "yandex-music"`, а не по id: встроенный (builtin.yandex-music)
и внешний (`yandex-music`) поставщики — разные записи реестра с одним сервисом.
До первого обращения колонки — честный отказ с объяснением.

Поиск (search(ctx, query, kind)): `Client.search(text, type_="track")` → Track
(id/title/artist/album/duration_ms/cover), топ-5 (ядро режет очередь); ссылка — у первого
трека сразу, у соседей — при переходе. kind (tales/podcast/audiobook) — тем же type=track.

neighbor(ctx, track_id, context, step): очередь — память модуля по владельцу (как у
встроенного Spotify): «включи музыку» и колбэк «дальше» — разные запросы; step ±1 по
сохранённому списку, 0 — текущий трек (продолжение с паузы).

rate(ctx, track_id, like): «Мне нравится» или дизлайк в аккаунте — uid из
`Client.account_status()`, затем `users_likes_tracks_add` / `users_dislikes_tracks_add`.

Вызовы библиотеки синхронные — в потоке (asyncio.to_thread). BASE_URL — константа модуля:
тесты подменяют её локальным подменным HTTP, настоящий Яндекс из автотестов не вызывается.
"""
from __future__ import annotations

import asyncio
import random
import time

import requests
from yandex_music import Client

PROVIDER = {"id": "builtin.yandex-music", "title": "Яндекс Музыка",
            "service": "yandex-music"}

QUEUE_LIMIT = 5
BASE_URL = None  # None — адрес API по умолчанию библиотеки (api.music.yandex.net)
COVER_SIZE = "200x200"
REDIRECT_TIMEOUT_S = 5
LINK_TIMEOUT_S = 2  # добор ссылки в neighbor (шаги, «плей», предзагрузка) — короткий таймаут запроса

STATE = {}  # владелец → {"queue": [Track], "current": int}

NOTE_NO_TOKEN = ("Токен Яндекс.Музыки ещё не захвачен: дождитесь обращения колонки к Яндексу "
                 "(или скажите колонке любую фразу) и повторите.")
NOTE_SEARCH_FAIL = "Не удалось выполнить поиск в Яндекс.Музыке."
NOTE_NO_PLAYABLE = "У этого трека нет проигрываемой версии."
NOTE_WHAT = "Что включить?"


class MusicError(Exception):
    """Нет проигрываемой версии → note пользователю (текст — для реплики)."""


def _owner(ctx):
    return str((getattr(ctx, "device", None) or {}).get("sn") or getattr(ctx, "sn", "") or "owner")


def _captured_token(ctx):
    """OAuth-токен аккаунта: ядро инжектирует захваченное в ExtensionContext вызова
    (captured_oauth) — стандартный доступ поставщика к настройкам вызова."""
    ext = getattr(ctx, "extension", None)
    return str(ext.get("captured_oauth") or "") if ext is not None else ""


def _language(ctx):
    settings = getattr(getattr(ctx, "extension", None), "settings", None) or {}
    return str(settings.get("market") or "ru")


def _client(ctx, token, timeout=None):
    """Клиент библиотеки без init(): статус аккаунта для поиска и ссылок не нужен.
    timeout — свой таймаут каждого запроса (у библиотеки 5 с); neighbor добирает
    ссылку коротким (LINK_TIMEOUT_S): задержку ответа колонке он не должен тянуть."""
    if timeout is None:
        return Client(token, base_url=BASE_URL, language=_language(ctx))
    from yandex_music.utils.request import Request
    return Client(token, base_url=BASE_URL, language=_language(ctx),
                  request=Request(timeout=timeout))


def _track_meta(track):
    """yandex_music.Track → Track контракта ядра (stream{url, format}; ссылка — позже)."""
    cover = track.get_og_image_url(COVER_SIZE) if track.og_image else ""
    return {"id": str(track.id or ""),
            "title": str(track.title or "").strip(),
            "artist": ", ".join(a.name or "" for a in track.artists or []),
            "album": ", ".join(a.title or "" for a in track.albums or []),
            "duration_ms": int(track.duration_ms or 0),
            "cover_url": cover,
            "stream": {"url": "", "format": "mp3"}}


def _download_link(client, track_id):
    """Прямая ссылка mp3: лучший битрейт без превью (download-info библиотеки)."""
    infos = client.tracks_download_info(track_id, get_direct_links=True) or []
    variants = [info for info in infos
                if str(info.codec or "").lower() == "mp3" and not info.preview and info.direct_link]
    if not variants:
        raise MusicError(NOTE_NO_PLAYABLE)
    return _final_link(max(variants, key=lambda info: int(info.bitrate_in_kbps or 0)).direct_link)


def _final_link(url):
    """Конечный адрес потока: get-mp3 отвечает 308 на сервер потоков, а плеер колонки
    (gstreamer souphttpsrc) перенаправление 308 не проходит — колонке уходит адрес
    после перенаправлений. Сбой запроса — исходная ссылка."""
    try:
        with requests.get(url, allow_redirects=True, stream=True, timeout=REDIRECT_TIMEOUT_S) as response:
            return response.url or url
    except requests.RequestException:
        return url


def _with_stream(client, track):
    """Track с добранной ссылкой потока (stream.url — форма контракта ядра)."""
    track = dict(track)
    track["stream"] = {"url": _download_link(client, track["id"]), "format": "mp3"}
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
    Тело целиком в потоке: библиотека синхронная, а мок-сервер тестов живёт на цикле."""
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

    client = _client(ctx, token)
    try:
        found = client.search(query, type_="track")
        tracks = [_track_meta(t) for t in ((found.tracks.results if found and found.tracks else None) or [])]
        tracks = [t for t in tracks if t["id"] and t["title"]][:QUEUE_LIMIT]
        if not tracks:
            return {"tracks": []}  # пусто: ядро отвечает «не нашла» (без note)
        tracks[0] = _with_stream(client, tracks[0])  # ссылку первого — сразу (сбой — note)
    except MusicError as exc:
        return {"tracks": [], "note": str(exc)}
    except Exception as exc:  # сеть/API библиотеки — честный отказ с типом ошибки
        return {"tracks": [], "note": f"{NOTE_SEARCH_FAIL} ({type(exc).__name__})"}
    _save_queue(owner, tracks, 0)
    return {"tracks": tracks, "start": 0, "context": {"type": "search"}}


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
    # позицию очереди двигает только commit (recoverCommit — подтверждённый старт трека):
    # предзагрузка @@mm_stack_engine_get_next и явные шаги её не сдвигают
    try:  # ссылку соседнего трека добираем при переходе (подпись ссылки живёт недолго)
        track = _with_stream(_client(ctx, token, LINK_TIMEOUT_S), queue[target])
    except Exception:
        return None
    entry["queue"][target] = track
    return track


def _commit_sync(ctx, track_id):
    """Колонка подтвердила старт трека (recoverCommit) — позиция очереди сверяется
    с играющим треком; чужого трека в очереди нет — позиция не меняется."""
    entry = _cached_queue(_owner(ctx))
    if not entry:
        return None
    index = next((i for i, t in enumerate(entry["queue"]) if str(t["id"]) == str(track_id)), None)
    if index is None:
        return None
    entry["current"] = index
    return True


async def commit(ctx, track_id):
    """Сверка позиции очереди с подтверждённым стартом трека (интерфейс ядра, recoverCommit)."""
    return await asyncio.to_thread(_commit_sync, ctx, str(track_id or ""))


async def rate(ctx, track_id, like):
    """Оценка трека в аккаунте (интерфейс ядра): like — «Мне нравится», иначе дизлайк.
    True — Яндекс подтвердил; нет токена, трека или сбой API — None."""
    return await asyncio.to_thread(_rate_sync, ctx, str(track_id or ""), bool(like))


def _rate_sync(ctx, track_id, like):
    token = _captured_token(ctx)
    if not token or not track_id:
        return None
    client = _client(ctx, token)
    try:
        status = client.account_status()
        uid = status.account.uid if status and status.account else None
        if uid is None:
            return None
        add = client.users_likes_tracks_add if like else client.users_dislikes_tracks_add
        return True if add(track_id, user_id=uid) else None
    except Exception:  # сеть/API библиотеки — оценка не подтверждена
        return None
