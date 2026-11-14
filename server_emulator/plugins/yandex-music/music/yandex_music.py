"""Яндекс Музыка — встроенный поставщик музыки (вид music, интерфейс ядра).

Клиент API — библиотека `yandex-music` (yandex-music-api): поиск, download-info и прямые
ссылки mp3 без DRM (`DownloadInfo.direct_link`, `get_direct_links=True`); колонке уходит
адрес после перенаправлений get-mp3 (её плеер не проходит 308). Директивы
(стек виджета) собирает ядро server_emulator/music.py — здесь только поиск, очередь и
навигация.

Авторизация: **OAuth-токен аккаунта не вводится**. Основной источник — токен аккаунта
владельца колонки из привязанной сессии (ядро: account.AccountSession.service_token,
обмен x_token на oauth.mobile.yandex.net); не привязан — токен, захваченный эмулятором
из реальных обращений колонки к Яндексу (заголовок `Authorization: OAuth …` на HTTP и
`X-UPRX-OAUTH-TOKEN` в WS-handshake; диспетчер/runtime вызывают `music.note_token`) и
хранённый в БД состояния (meta `yandex_oauth`). Ядро передаёт токен поставщику как
системное значение вызова (`yandex_oauth` в ctx.extension) — по признаку сервиса
`PROVIDER["service"] = "yandex-music"`, а не по id: встроенный (builtin.yandex-music)
и внешний (`yandex-music`) поставщики — разные записи реестра с одним сервисом.
Сервис ответил 401 — ядро обменивает токен заново (отметка `token_expired`) и повторяет
вызов; повторный 401 — честный отказ с объяснением.

Поиск (search(ctx, query, kind)): `Client.search(text, type_="track")` → Track
(id/title/artist/album/duration_ms/cover), первые QUEUE_LIMIT (30) результатов в порядке
выдачи; ссылка потока — у первого трека сразу, у следующих PREFETCH_AHEAD — предзагрузкой
в фоне, у остальных — лениво при переходе (трек без ссылки остаётся в очереди).
kind (tales/podcast/audiobook) — тем же type=track.

«Включи музыку» без запроса — плейлист по умолчанию (настройка `default_playlist`):
«Мне нравится» (по умолчанию; порядок — новые лайки первыми, по timestamp), «Моя волна»
(`user:onyourwave`, первая порция станции) или название плейлиста пользователя
(порядок плейлиста). Названного плейлиста нет или он пуст — «Мне нравится», затем волна;
пусто всё — вопрос «Что включить?». Сбой API — честный отказ с типом ошибки.
Очередь плеера — первые QUEUE_LIMIT треков выбранного списка, стартует первый.

neighbor(ctx, track_id, context, step): очередь — память модуля по владельцу (как у
встроенного Spotify): «включи музыку» и колбэк «дальше» — разные запросы; step ±1 по
сохранённому списку от трека track_id (нет в очереди — от текущей позиции), 0 — текущий
трек (продолжение с паузы). Следующий всегда следующий по порядку списка; позицию двигает
только commit (подтверждённый старт трека). queue(ctx) — всё окно очереди для виджета. Очередь и ссылки потока
переживают перезапуск сервера: гидратация из ctx.data сценария media.music
(ключ «music_queue/yandex-music», хранилище state/, по владельцу-пользователю/колонке)
при первом обращении; очередь и позиция дублируются туда при каждом изменении,
ссылки потока — вместе со временем получения (при чтении отбрасываются по TTL,
протухшая добирается заново один раз). Минимум после рестарта: «плей» продолжает
последний трек, «дальше/назад» идут от него; свежие ссылки работают сразу.

Отклик кнопок: ссылка потока кешируется с TTL (LINK_TTL_S, подпись ссылки живёт
часами — запас 10 минут), соседи текущего трека предзагружаются заранее — в фоне
после поиска (PREFETCH_AHEAD следующих) и при подтверждении старта трека (±1). «дальше/назад/
продолжить» из кеша отвечают без обращений к Яндексу; нет в кеше — добор ссылки
коротким таймаутом (LINK_TIMEOUT_S), как раньше. Предзагрузка позицию очереди не
двигает (её двигает только commit) и о себе не сообщает: сбой — просто нет кеша.

rate(ctx, track_id, like): «Мне нравится» или дизлайк в аккаунте — uid из
`Client.account_status()`, затем `users_likes_tracks_add` / `users_dislikes_tracks_add`.

Вызовы библиотеки синхронные — в потоке (asyncio.to_thread). BASE_URL — константа модуля:
тесты подменяют её локальным подменным HTTP, настоящий Яндекс из автотестов не вызывается.
"""
from __future__ import annotations

import asyncio
import time

import requests
from yandex_music import Client
from yandex_music.exceptions import UnauthorizedError

PROVIDER = {"id": "builtin.yandex-music", "title": "Яндекс Музыка",
            "service": "yandex-music"}

QUEUE_LIMIT = 30  # окно очереди плеера (MAX_QUEUE ядра)
PREFETCH_AHEAD = 2  # ссылки скольких следующих треков берутся заранее; остальные — лениво
DEFAULT_PLAYLIST = "Мне нравится"  # значение настройки default_playlist по умолчанию
LIKES_NAMES = ("мне нравится", "likes", "любимое")
WAVE_NAMES = ("моя волна", "волна", "wave")
WAVE_STATION = "user:onyourwave"
BASE_URL = None  # None — адрес API по умолчанию библиотеки (api.music.yandex.net)
COVER_SIZE = "200x200"
REDIRECT_TIMEOUT_S = 5
LINK_TIMEOUT_S = 2  # добор ссылки в neighbor (шаги, «плей», предзагрузка) — короткий таймаут запроса
LINK_TTL_S = 600  # кеш ссылки потока: подпись ссылки живёт часами, запас — 10 минут
PERSIST_KEY = "music_queue/yandex-music"  # ctx.data сценария media.music: очередь, позиция и ссылки переживают перезапуск (ключ — с id поставщика: у каждого своё)

STATE = {}  # владелец → {"queue": [Track], "current": int}
LINKS = {}  # владелец → {track_id: {"url", "format", "ts"}} — кеш ссылок потока (TTL LINK_TTL_S)
_PREFETCH = set()  # фоновые задачи предзагрузки ссылок (сами завершаются; тесты ждут prefetch_wait)

NOTE_NO_TOKEN = ("Токен Яндекс.Музыки недоступен: не привязан аккаунт владельца "
                 "(кнопка «Яндекс» у пользователя) и ещё не захвачено обращение колонки "
                 "к Яндексу.")
NOTE_TOKEN_EXPIRED = "Токен Яндекс.Музыки не принят — не удалось обновить."
NOTE_SEARCH_FAIL = "Не удалось выполнить поиск в Яндекс.Музыке."
NOTE_NO_PLAYABLE = "У этого трека нет проигрываемой версии."
NOTE_WHAT = "Что включить?"


class MusicError(Exception):
    """Нет проигрываемой версии → note пользователю (текст — для реплики)."""


def _owner(ctx):
    return str((getattr(ctx, "device", None) or {}).get("sn") or getattr(ctx, "sn", "") or "owner")


def _yandex_token(ctx):
    """OAuth-токен аккаунта (владельца колонки из привязанной сессии или захваченный):
    ядро инжектирует его в ExtensionContext вызова (yandex_oauth) — стандартный доступ
    поставщика к настройкам вызова."""
    ext = getattr(ctx, "extension", None)
    return str(ext.get("yandex_oauth") or "") if ext is not None else ""


def _language(ctx):
    settings = getattr(getattr(ctx, "extension", None), "settings", None) or {}
    return str(settings.get("market") or "ru")


def _client(token, language, timeout=None):
    """Клиент библиотеки без init(): статус аккаунта для поиска и ссылок не нужен.
    language — до запроса, не из ctx (в фоновой предзагрузке ctx уже мёртв).
    timeout — свой таймаут каждого запроса (у библиотеки 5 с); neighbor добирает
    ссылку коротким (LINK_TIMEOUT_S): задержку ответа колонке он не должен тянуть."""
    if timeout is None:
        return Client(token, base_url=BASE_URL, language=language)
    from yandex_music.utils.request import Request
    return Client(token, base_url=BASE_URL, language=language,
                  request=Request(timeout=timeout))


def _fresh_link(owner, track_id):
    """Свежая ссылка из кеша (TTL LINK_TTL_S) или None: «дальше/продолжить» из кеша
    отвечают без обращений к Яндексу — это и есть быстрый отклик кнопок."""
    record = LINKS.get(owner, {}).get(str(track_id))
    if record and time.time() - record["ts"] <= LINK_TTL_S:
        return {"url": record["url"], "format": record["format"]}
    return None


def _remember_link(owner, track_id, url, fmt="mp3"):
    LINKS.setdefault(owner, {})[str(track_id)] = {"url": url, "format": str(fmt), "ts": time.time()}


def _persist(ctx, owner):
    """Дубликат очереди, позиции и ссылок в ctx.data (state/, по владельцу) —
    переживает перезапуск сервера. Ссылки потока — вместе со временем получения
    (уточнение владельца): при чтении отбрасываются по TTL, протухшая добирается
    заново один раз. Секретов и токенов в хранилище нет. Нет ctx.data (прямые
    вызовы) или dry-run — просто не пишем."""
    entry = STATE.get(owner)
    data = getattr(ctx, "data", None)
    if entry is None or data is None:
        return
    try:
        # stream в самих треках не пишем: подписанные ссылки живут ограниченное
        # время — хранятся отдельно, со временем получения (TTL при чтении)
        data.set(PERSIST_KEY, {"queue": [{k: v for k, v in t.items() if k != "stream"}
                                         for t in entry["queue"]],
                               "current": entry.get("current", 0),
                               "ts": entry.get("ts"),
                               "links": dict(LINKS.get(owner) or {})})
    except Exception:
        pass  # сбой записи не влияет на ответ колонке


def _hydrate(ctx, owner):
    """Очередь после перезапуска: память пуста — восстанавливаем из ctx.data
    (последний трек и позиция: «плей» продолжает, «дальше/назад» идут от него).
    Ссылки — со временем получения; свежие (TTL) работают сразу, протухшая
    добирается заново при первом шаге — один раз."""
    if _cached_queue(owner) is not None:
        return
    data = getattr(ctx, "data", None)
    record = data.get(PERSIST_KEY) if data is not None else None
    if not isinstance(record, dict):
        return
    tracks = [t for t in (record.get("queue") or [])
              if isinstance(t, dict) and str(t.get("id") or "").strip() and str(t.get("title") or "").strip()]
    if not tracks:
        return
    STATE[owner] = {"queue": tracks, "current": min(int(record.get("current") or 0), len(tracks) - 1),
                    "ts": record.get("ts") or 0}
    links = {str(tid): rec for tid, rec in (record.get("links") or {}).items()
             if isinstance(rec, dict) and rec.get("url")}
    if links:
        LINKS[owner] = links


def _prefetch(ctx, owner, track_ids, loop):
    """Ссылки треков заранее — в фоне (кнопки отвечают мгновенно). Токен и язык
    снимаются до старта: ctx живёт только в обработчике запроса. Позицию очереди
    не двигает; сбой — просто нет кеша. loop — цикл вызывающего запроса: из
    рабочего потока задачи не создаются, поэтому scheduling через него."""
    token = _yandex_token(ctx)
    ids = [str(tid) for tid in track_ids if tid and not _fresh_link(owner, tid)]
    if not token or not ids or loop is None:
        return
    language = _language(ctx)

    def run():
        client = _client(token, language, LINK_TIMEOUT_S)
        for tid in ids:
            if _fresh_link(owner, tid):  # пока добирали — ссылка появилась
                continue
            try:
                _remember_link(owner, tid, _download_link(client, tid))
            except Exception:
                pass  # нет кеша — neighbor доберёт при переходе

    task = asyncio.run_coroutine_threadsafe(asyncio.to_thread(run), loop)
    _PREFETCH.add(task)
    task.add_done_callback(_PREFETCH.discard)


async def prefetch_wait():
    """Ждать завершения фоновых предзагрузок (тесты, замер): детерминированные
    проверки кеша и счётчиков обращений."""
    while _PREFETCH:
        pending = [asyncio.wrap_future(task) for task in list(_PREFETCH)]
        await asyncio.gather(*pending, return_exceptions=True)


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
    """Прямая ссылка mp3: лучший битрейт без превью. Список вариантов — один запрос; XML со ссылкой
    берётся только у выбранного (get_direct_links=True тянул его у каждого варианта по очереди:
    секунды лишнего ожидания перед ответом колонке)."""
    infos = client.tracks_download_info(track_id) or []
    variants = [info for info in infos
                if str(info.codec or "").lower() == "mp3" and not info.preview and info.download_info_url]
    if not variants:
        raise MusicError(NOTE_NO_PLAYABLE)
    best = max(variants, key=lambda info: int(info.bitrate_in_kbps or 0))
    return _final_link(best.get_direct_link())


def _final_link(url):
    """Конечный адрес потока: get-mp3 отвечает 308 на сервер потоков, а плеер колонки
    (gstreamer souphttpsrc) перенаправление 308 не проходит — колонке уходит адрес
    после перенаправлений. Сбой запроса — исходная ссылка."""
    try:
        with requests.get(url, allow_redirects=True, stream=True, timeout=REDIRECT_TIMEOUT_S) as response:
            return response.url or url
    except requests.RequestException:
        return url


def _with_stream(ctx, owner, track, timeout=None):
    """Track со ссылкой потока: кеш свеж — без обращений к Яндексу; нет — добор
    (ссылка запоминается: следующие шаги и предзагрузка её переиспользуют)."""
    track = dict(track)
    fresh = _fresh_link(owner, track["id"])
    if fresh is not None:
        track["stream"] = fresh
        return track
    client = _client(_yandex_token(ctx), _language(ctx), timeout)
    track["stream"] = {"url": _download_link(client, track["id"]), "format": "mp3"}
    _remember_link(owner, track["id"], track["stream"]["url"])
    return track


def _queue_window(ctx):
    owner = _owner(ctx)
    _hydrate(ctx, owner)
    entry = _cached_queue(owner)
    return [dict(track) for track in entry["queue"]] if entry else []


async def queue(ctx):
    """Окно очереди для виджета (интерфейс ядра): все треки очереди в порядке воспроизведения."""
    return _queue_window(ctx)


def _cached_queue(owner):
    entry = STATE.get(owner)
    return entry if entry and entry.get("queue") else None


def _save_queue(ctx, owner, tracks, start=0, loop=None):
    entry = {"queue": tracks, "current": int(start), "ts": time.time()}
    STATE[owner] = entry
    _persist(ctx, owner)
    # ссылку первого добрали сразу, ближайшие следующие — заранее в фоне: «дальше» без задержки
    _prefetch(ctx, owner, [t["id"] for t in tracks[start + 1:start + 1 + PREFETCH_AHEAD]], loop)
    return entry


async def search(ctx, query, kind="music"):
    """Поиск треков (интерфейс ядра) или честный отказ {"tracks": [], "note"}.
    Тело целиком в потоке: библиотека синхронная, а мок-сервер тестов живёт на цикле."""
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(_search_sync, ctx, str(query or ""), str(kind or "music"), loop)


def _search_sync(ctx, query, kind, loop):
    token = _yandex_token(ctx)
    if not token:
        return {"tracks": [], "note": NOTE_NO_TOKEN}
    owner = _owner(ctx)

    client = _client(token, _language(ctx))
    try:
        if query:
            found = client.search(query, type_="track")
            raw = (found.tracks.results if found and found.tracks else None) or []
            context = {"type": "search"}
        else:  # «включи музыку»: плейлист по умолчанию
            raw, source = _default_tracks(client, _default_playlist(ctx))
            if not raw:
                return {"tracks": [], "note": NOTE_WHAT}  # плейлиста нет физически — спросить
            context = {"type": "default", "source": source}
        tracks = [_track_meta(t) for t in raw]
        tracks = [t for t in tracks if t["id"] and t["title"]][:QUEUE_LIMIT]
        if not tracks:
            return {"tracks": []}  # пусто: ядро отвечает «не нашла» (без note)
        tracks[0] = _with_stream(ctx, owner, tracks[0])  # ссылку первого — сразу (сбой — note)
    except MusicError as exc:
        return {"tracks": [], "note": str(exc)}
    except UnauthorizedError:
        # сервис ответил 401 — ядро обменивает токен заново (отметка token_expired) и
        # повторяет вызов; повторный 401 доходит пользователю честной репликой
        return {"tracks": [], "note": NOTE_TOKEN_EXPIRED, "token_expired": True}
    except Exception as exc:  # сеть/API библиотеки — честный отказ с типом ошибки
        return {"tracks": [], "note": f"{NOTE_SEARCH_FAIL} ({type(exc).__name__})"}
    _save_queue(ctx, owner, tracks, 0, loop)
    return {"tracks": tracks, "start": 0, "context": context}


def _default_playlist(ctx):
    settings = getattr(getattr(ctx, "extension", None), "settings", None) or {}
    return str(settings.get("default_playlist") or DEFAULT_PLAYLIST).strip() or DEFAULT_PLAYLIST


def _uid(client):
    status = client.account_status()
    uid = status.account.uid if status and status.account else None
    if uid is None:
        raise MusicError("Не удалось определить аккаунт Яндекс.Музыки.")
    return uid


def _full_tracks(client, shorts):
    """TrackShort → полные Track одним запросом, порядок — как в списке."""
    ids = [str(short.track_id) for short in shorts[:QUEUE_LIMIT] if short.track_id]
    by_id = {str(track.id): track for track in (client.tracks(ids) if ids else []) or []}
    return [by_id[str(short.id)] for short in shorts[:QUEUE_LIMIT] if str(short.id) in by_id]


def _likes(client):
    """«Мне нравится»: новые лайки первыми (по timestamp, при равенстве — порядок API)."""
    liked = client.users_likes_tracks(user_id=_uid(client))
    shorts = sorted(liked.tracks or [], key=lambda short: str(short.timestamp or ""), reverse=True) if liked else []
    return _full_tracks(client, shorts)


def _wave(client):
    """«Моя волна»: первая порция станции user:onyourwave в порядке выдачи."""
    result = client.rotor_station_tracks(WAVE_STATION)
    return [item.track for item in (result.sequence if result else None) or [] if item.track]


def _user_playlist(client, title):
    """Плейлист пользователя по названию (без учёта регистра), порядок — как в плейлисте."""
    uid = _uid(client)
    wanted = title.strip().lower()
    for item in client.users_playlists_list(user_id=uid) or []:
        if str(item.title or "").strip().lower() == wanted:
            playlist = client.users_playlists(item.kind, user_id=uid)
            playlist = playlist[0] if isinstance(playlist, list) and playlist else playlist
            return _full_tracks(client, list(getattr(playlist, "tracks", None) or []))
    return []


def _default_tracks(client, name):
    """(треки yandex_music, источник) плейлиста по умолчанию: названный, затем «Мне нравится»,
    затем «Моя волна»; ([], "") — не нашлось ничего."""
    lowered = name.lower()
    chain = []
    if lowered in LIKES_NAMES:
        chain.append(("likes", _likes))
    elif lowered in WAVE_NAMES:
        chain.append(("wave", _wave))
    else:
        chain.append(("playlist", lambda c: _user_playlist(c, name)))
    chain += [item for item in (("likes", _likes), ("wave", _wave)) if item[0] != chain[0][0]]
    for source, load in chain:
        tracks = load(client)
        if tracks:
            return tracks, source
    return [], ""


async def neighbor(ctx, track_id, context, step):
    """Соседний трек очереди (интерфейс ядра): Track или None — дальше нечего."""
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(_neighbor_sync, ctx, track_id, int(step), loop)


def _neighbor_sync(ctx, track_id, step, loop):
    token = _yandex_token(ctx)
    if not token:
        return None
    owner = _owner(ctx)
    _hydrate(ctx, owner)  # после перезапуска очередь живёт в ctx.data
    entry = _cached_queue(owner)
    if not entry:
        return None
    queue, current = entry["queue"], entry.get("current", 0)
    index = next((i for i, t in enumerate(queue) if str(t["id"]) == str(track_id)), current)
    target = index + int(step)
    if not 0 <= target < len(queue):
        return None
    # позицию очереди двигает только commit (recoverCommit — подтверждённый старт трека):
    # предзагрузка @@mm_stack_engine_get_next и явные шаги её не сдвигают
    try:  # кеш ссылки свеж — ответ сразу; нет — добор коротким таймаутом
        track = _with_stream(ctx, owner, queue[target], LINK_TIMEOUT_S)
    except Exception:
        return None
    entry["queue"][target] = track
    _persist(ctx, owner)
    return track


def _commit_sync(ctx, track_id, loop):
    """Колонка подтвердила старт трека (recoverCommit) — позиция очереди сверяется
    с играющим треком; чужого трека в очереди нет — позиция не меняется. Соседи
    новой позиции предзагружаются в фоне: «дальше/назад» ответят мгновенно."""
    owner = _owner(ctx)
    _hydrate(ctx, owner)
    entry = _cached_queue(owner)
    if not entry:
        return None
    index = next((i for i, t in enumerate(entry["queue"]) if str(t["id"]) == str(track_id)), None)
    if index is None:
        return None
    entry["current"] = index
    _persist(ctx, owner)
    neighbors = [entry["queue"][i]["id"] for i in (index - 1, index + 1) if 0 <= i < len(entry["queue"])]
    _prefetch(ctx, owner, neighbors, loop)
    return True


async def commit(ctx, track_id):
    """Сверка позиции очереди с подтверждённым стартом трека (интерфейс ядра, recoverCommit)."""
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(_commit_sync, ctx, str(track_id or ""), loop)


async def rate(ctx, track_id, like):
    """Оценка трека в аккаунте (интерфейс ядра): like — «Мне нравится», иначе дизлайк.
    True — Яндекс подтвердил; нет токена, трека или сбой API — None."""
    return await asyncio.to_thread(_rate_sync, ctx, str(track_id or ""), bool(like))


def _rate_sync(ctx, track_id, like):
    token = _yandex_token(ctx)
    if not token or not track_id:
        return None
    client = _client(token, _language(ctx))
    try:
        status = client.account_status()
        uid = status.account.uid if status and status.account else None
        if uid is None:
            return None
        add = client.users_likes_tracks_add if like else client.users_dislikes_tracks_add
        return True if add(track_id, user_id=uid) else None
    except Exception:  # сеть/API библиотеки — оценка не подтверждена
        return None
