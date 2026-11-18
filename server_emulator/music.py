"""Музыка: поставщик контента и сборка стека директорий виджета плеера.

Точка подключения расширений — вид music общего реестра поставщиков
(server_emulator/providers.py: объявление contributes.providers плагина и требования
потребителя; реестр собирает
runtime.rebuild_extension_registries; конфликт имён или битый модуль отключает
расширение целиком, как у actions/datasets):

    PROVIDER = {"id": "spotify", "title": "Spotify"}

    async def search(ctx, query, kind):     # kind: music|tales|podcast|audiobook; "" — «что-нибудь»
        return {"tracks": [Track, ...], "start": 0, "context": {...}}
        # пустой результат с {"note": текст} — честный отказ: ядро передаёт note сценарию,
        # сценарий произносит текст пользователю

    async def neighbor(ctx, track_id, context, step):   # step +1/-1, 0 — текущий; None — дальше нечего
        return Track | None

    async def rate(ctx, track_id, like):    # необязательна; None — не поддерживается

    async def queue(ctx):                   # необязательна: текущая очередь поставщика
        return [Track, ...]                 # окно очереди для виджета (порядок воспроизведения)

    Track = {"id", "title", "artist", "album"?, "duration_ms", "cover_url"?,
             "stream": {"url", "format": "mp3|aac|flac", "decode_key"?}}
    # В очереди (search.tracks, queue) трек без stream.url допустим: ссылку поставщик
    # добирает лениво; играть можно только трек со ссылкой — start указывает на него,
    # neighbor всегда возвращает трек со ссылкой.

Выбранный поставщик — настройка settings["providers"]["music"] (id; пусто — первый
включённый по алфавиту). ctx.music (SDK) — запись выбранного поставщика или None.
Вызов функций поставщика — только через search()/neighbor()/rate() этого модуля:
они ставят коду поставщика его настройки ctx.extension (как у режимов «Интеллекта»,
и приводят Track/результат к контракту.

Своей очереди ядро не хранит (главное правило tasks/PLAN-station-skills): треки отдаёт
поставщик, контекст очереди — его search/neighbor, состояние плеера — колонка
(ctx.player). «дальше/назад» голосом идут в neighbor без track_id (колонка не
сообщает идентификатор в запросе — только Title/SubTitle в ctx.player): поставщик
ведёт свою очередь сам. Очередь переживает перезапуск сервера: поставщик хранит её
в SDK-хранилище сценария (ctx.data, ключ с его id — «music_queue/<id>») и
восстанавливает при первом обращении после рестарта — «плей» с виджета продолжает
последний трек, «дальше/назад» идут от него. В хранилище — идентификаторы очереди
и позиция (и атрибуты для виджета); ссылки потока — только вместе со временем
получения, при чтении отбрасываются по TTL (подписанные URL живут ограниченное
время): свежая после рестарта работает сразу, протухшая добирается заново один
раз. Секретов и токенов в хранилище нет. Ядро в хранении и восстановлении не
участвует.

Директивы — по образцам настоящего сервера (захват 2026-11-02-music,
docs/contracts/player.md): полный стек виджета на «включи музыку» —
tts_play_placeholder + draw_animation(animations/player_play) +
show_audio_player_screen + apply_player_playback_queue (proto
NAlice.TMusicPlatformQueueCapability.TApplyMusicPlatformQueueDirective) +
set_audio_player_sequence_state (NAlice.TAudioPlayerCapability.TSetSequenceStateDirective)
+ audio_play; «дальше/назад» — тройка очередь+состояние+audio_play без экрана
и анимации. Сущности очереди (id, title, artist, cover) — из результатов поиска
поставщика (решение владельца, PLAN-spotify-music): идентификаторы поставщика —
строки, артист/альбом идут и сущностью, и записью меты (карты
ProviderMeta/ProviderTrackMeta по имени из enum Alice.Music.MusicProvider —
yandex-music — MUSIC_PROVIDER_YANDEX_MUSIC, у остальных поставщиков значения в
схеме нет — MUSIC_PROVIDER_UNSPECIFIED).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
import uuid
from pathlib import Path

from . import providers as _providers

MAX_QUEUE = 30  # окно очереди плеера: первые 30 треков списка поставщика, порядок — как у поставщика

ANIMATION_PROTO = "NAlice.TAnimationCapability.TDrawAnimationDirective"
SCREEN_PROTO = "NAlice.TAudioPlayerScreenCapability.TShowAudioPlayerScreenDirective"
QUEUE_PROTO = "NAlice.TMusicPlatformQueueCapability.TApplyMusicPlatformQueueDirective"
SEQSTATE_PROTO = "NAlice.TAudioPlayerCapability.TSetSequenceStateDirective"
S3_BUCKET = "https://quasar.s3.yandex.net"
PLAYER_PLAY_ANIMATION = "animations/player_play"  # образец «включи музыку» (захват 2026-11-02)
PLAYER_LIKE_ANIMATION = "animations/player_like"  # образец «лайк» (захват 2026-11-02)
LIKE_TEXT = "Рада, что вы оценили."  # образец «лайк»: карточка с запятой, речь без
LIKE_SPEECH = "Рада что вы оценили."
PLAYER_DISLIKE_ANIMATION = "animations/player_dislike"  # образец «дизлайк» (захват 2026-11-02)
DISLIKE_TEXT = "Нет проблем, поставила дизлайк."

YANDEX_PROVIDER_ID = "yandex-music"
# имена из enum Alice.Music.MusicProvider для карт меты и поля Entities.MusicProvider:
PROVIDER_KEYS = {YANDEX_PROVIDER_ID: "MUSIC_PROVIDER_YANDEX_MUSIC"}

# OAuth-токены аккаунтов, захваченные из реальных обращений колонок к Яндексу:
# заголовок Authorization: OAuth … (HTTP к quasar и др.) или X-UPRX-OAUTH-TOKEN
# (WS-handshake). Колонки могут быть разных пользователей — токены собираются по
# SN каждой ("oauth" — последний захваченный, запасной источник для колонки,
# чей токен ещё не встречался). Персистентность — StateStore meta.
_CAPTURED = {}
_CAPT_DETAILS = {}  # sn → {"ts", "ip"} — когда и откуда захвачен токен (meta + память)


def note_token(runtime, token, sn=None, ip=None):
    """Захват OAuth-токена из реального запроса колонки: копилка по SN (токены
    колонок разные, ip — адрес колонки из запроса) + meta yandex_oauth_columns в БД
    состояния; "oauth" — последний захваченный (запасной для колонки без своего
    токена). Токен в журнал/лог не пишется; вызывается до маскирования журналом."""
    token = str(token or "").strip()
    if not _is_oauth(token):
        return False
    changed = False
    if sn and _CAPTURED.get(sn) != token:
        _CAPTURED[sn] = token
        changed = True
    if _CAPTURED.get("oauth") != token:
        _CAPTURED["oauth"] = token
        changed = True
    if not changed:
        return True
    if sn:
        _CAPT_DETAILS[sn] = {"ts": time.time(), "ip": str(ip or "")}
    _persist(runtime)
    return True


def forget_token(runtime, sn):
    """Удаление захваченного токена колонки (панель «Авторизация в сервисах»):
    из копилки уходят и запись по SN, и запасной, если это тот же токен. Неизвестный
    SN — ошибка (честный отказ панели). Колонка принесёт токен снова при следующем
    обращении к Яндексу."""
    sn = str(sn or "")
    token = _CAPTURED.pop(sn, None)
    if token is None:
        raise ValueError("Токен этой колонки не захвачен")
    _CAPT_DETAILS.pop(sn, None)
    if _CAPTURED.get("oauth") == token:
        _CAPTURED.pop("oauth", None)
    _persist(runtime)
    return True


def _persist(runtime):
    """Копилка в БД состояния: meta yandex_oauth_columns (по SN, с деталями захвата)
    и запасной yandex_oauth; сбой хранения не влияет на запрос колонки."""
    try:
        store = getattr(runtime, "store", None)
        if store is None:
            return
        store.set_meta("yandex_oauth_columns", json.dumps(
            {sn: {"token": _CAPTURED[sn],
                  "ts": (_CAPT_DETAILS.get(sn) or {}).get("ts") or time.time(),
                  "ip": (_CAPT_DETAILS.get(sn) or {}).get("ip") or ""}
             for sn in _CAPTURED if sn != "oauth"}, ensure_ascii=False))
        oauth = _CAPTURED.get("oauth")
        store.set_meta("yandex_oauth", json.dumps({"token": oauth, "ts": time.time()},
                                                  ensure_ascii=False) if oauth else "")
    except Exception:
        pass  # сбой хранения не влияет на запрос колонки


def _is_oauth(token):
    """Токен из заголовка OAuth: печатные символы без пробелов, не короче 20 — формат
    токенов аккаунта Яндекс не фиксирован (y0_…, AQAAAA…, с точками)."""
    return bool(re.fullmatch(r"[\x21-\x7e]{20,}", token))


def captured_oauth(sn=None):
    """Токен колонки sn из копилки (свой — приоритет), без SN или без своего —
    последний захваченный. Пусто — колонка ещё не обращалась к Яндексу."""
    if sn and _CAPTURED.get(sn):
        return str(_CAPTURED[sn])
    return str(_CAPTURED.get("oauth") or "")


def captured_columns():
    """SN колонок, чьи токены лежат в копилке (без значений токенов)."""
    return sorted(key for key in _CAPTURED if key != "oauth")


def captured_column_details():
    """Детали захвата по каждой колонке копилки: [{sn, ts, ip}] — для панели
    «Авторизация в сервисах» (токены наружу не идут; платформа добирает API из
    реестра устройств)."""
    return [{"sn": sn,
             "ts": (_CAPT_DETAILS.get(sn) or {}).get("ts"),
             "ip": (_CAPT_DETAILS.get(sn) or {}).get("ip") or ""}
            for sn in captured_columns()]


def load_token(store):
    """Гидратация копилки из БД состояния (meta yandex_oauth_columns и запасной
    yandex_oauth) — старт сервера: захваченные раньше токены доступны поставщику
    до первого нового обращения колонок."""
    if store is None:
        return
    try:
        columns = json.loads(store.meta("yandex_oauth_columns") or "{}")
        for sn, item in columns.items():
            token = str((item or {}).get("token") or "")
            if sn != "oauth" and _is_oauth(token):
                _CAPTURED[sn] = token
                _CAPT_DETAILS[sn] = {"ts": item.get("ts"), "ip": str(item.get("ip") or "")}
    except (ValueError, AttributeError):
        pass
    raw = store.meta("yandex_oauth")
    if not raw:
        _CAPTURED.pop("oauth", None)  # пустая БД (другой Runtime) — чужой токен из кеша убрать
        return
    try:
        token = json.loads(raw).get("token")
        if _is_oauth(str(token or "")):
            _CAPTURED["oauth"] = str(token)
    except (ValueError, AttributeError):
        pass

UNSPECIFIED_KEY = "MUSIC_PROVIDER_UNSPECIFIED"


# --- реестр поставщиков музыки (общий реестр видов — server_emulator/providers.py) ----


# потребитель вида music: обязательные функции поставщика (см. providers.require)
_providers.require("music", ("search", "neighbor"), ("rate", "commit", "queue"))


def selected(settings):
    """Выбранный поставщик музыки: settings["providers"]["music"] (id); пусто/нет —
    первый включённый по алфавиту id. Выбранный id не в реестре (расширение
    выключили) — None. Реестр собирает runtime.rebuild_extension_registries."""
    return _providers.selected("music", settings)


def provider_service(provider):
    """Значение PROVIDER["service"] — признак сервиса вместо id: у Яндекс.Музыки
    встроенный (builtin.yandex-music) и внешний (yandex-music) поставщики — разные
    записи реестра с одним service, токен и ключ меты — по нему. Без service —
    совместимость с уже установленным внешним поставщиком по id."""
    return str((provider or {}).get("service") or "").strip()


def is_yandex_music(provider):
    """Поставщик Яндекс.Музыки: service="yandex-music"; без service — по id
    (совместимость с уже установленным внешним расширением)."""
    service = provider_service(provider)
    return service == YANDEX_PROVIDER_ID or \
        (not service and (provider or {}).get("id") == YANDEX_PROVIDER_ID)


class _WithSystemValues:
    """ExtensionContext + системные значения вызова (yandex_oauth), только чтение."""

    def __init__(self, base, values):
        self._base = base
        self._values = dict(values)

    @property
    def id(self):
        return self._base.id

    @property
    def settings(self):
        return {**self._base.settings, **self._values}

    def get(self, key, default=None):
        return self._values.get(key, self._base.get(key, default))


def extension_context(ctx, provider, token=None):
    """ExtensionContext расширения-поставщика на этот вызов (настройки свежие) или None.
    token — OAuth-токен аккаунта для поставщика Яндекс.Музыки (токен этой колонки из
    копилки или из основного аккаунта реестра; пусто — не передавать)."""
    runtime = ctx._services_runtime()
    manager = getattr(runtime, "extensions", None)
    build = getattr(manager, "context", None)
    base = build(provider["ext_id"]) if callable(build) else None
    if base is None:
        return None
    if not is_yandex_music(provider):
        return base  # OAuth аккаунта Яндекса — только поставщику Яндекс.Музыки
    return _WithSystemValues(base, {"yandex_oauth": token}) if token else base


async def _oauth_token(ctx, provider):
    """(токен, обновить) для поставщика: у Яндекс.Музыки — токен своей колонки из
    копилки (выхватили — используем его), нет своего — последний захваченный, и
    только затем токен сервиса основного QR-аккаунта реестра «Авторизации в
    сервисах» (обмен x_token на oauth.mobile.yandex.net). refresh — сервис ответил
    401: реестр обменивает x_token заново мимо кеша; у захваченного токена своего
    обновления нет — колонка принесёт новый сама. Остальным поставщикам — (None, None)."""
    if not is_yandex_music(provider):
        return None, None
    runtime = ctx._services_runtime()
    captured = captured_oauth(getattr(ctx, "sn", None))
    if captured:
        async def refresh():
            try:
                return await _registry_service_token(runtime, refresh=True)
            except Exception:  # noqa: BLE001 — запасной источник недоступен, честный отказ
                return ""
        return captured, refresh

    async def refresh():
        try:
            return await _registry_service_token(runtime, refresh=True)
        except Exception:  # noqa: BLE001 — обмен не удался, честный отказ
            return ""

    try:
        return await _registry_service_token(runtime), refresh
    except Exception:  # noqa: BLE001 — реестр недоступен: пустой токен
        logging.getLogger(__name__).warning("Токен музыки из реестра не получен")
        return "", None


async def _registry_service_token(runtime, refresh=False):
    """Токен сервиса «music» из основного QR-аккаунта Яндекса реестра: обмен x_token
    на oauth.mobile.yandex.net (account.AccountSession.service_token). Входа нет или
    обмен не удался — пусто (честный отказ поставщику, без секретов)."""
    accounts = getattr(runtime, "service_accounts", None)
    record = accounts.default("yandex") if accounts else None
    if record is None:
        return ""
    from . import account
    session = account.AccountSession(runtime, path=accounts.secret_path(record["id"]))
    if not session.status()["login"]:
        return ""
    try:
        return await session.service_token("music", refresh=refresh)
    except account.AccountError as exc:
        logging.getLogger(__name__).warning("Токен музыки аккаунта «%s»: %s", record["title"], exc)
        return ""


async def invoke(ctx, provider, function, *args):
    """Вызов функции поставщика (providers.call_invoker) вне сценария — ступень
    свободных хвостов «включи {X}»: тот же SDK-контекст вызова первым аргументом
    и токен аккаунта с обменом по token_expired, что у music.search (_call);
    без токена Яндекс.Музыка не отвечает, и голая фраза не находит ничего."""
    return await _call(ctx, provider, function, ctx, *args)


_providers.call_invoker("music", invoke)


async def _call(ctx, provider, function, *args):
    """Вызов функции поставщика: его расширение — ctx.extension на время вызова
    (как у режимов «Интеллекта»; прежнее значение восстанавливается). Поставщику
    Яндекс.Музыки — токен аккаунта (владельца колонки или захваченный); его отметка
    token_expired (сервис ответил 401) — обмен заново один раз и повтор вызова."""
    previous = ctx.extension
    token, refresh = await _oauth_token(ctx, provider)
    ctx.extension = extension_context(ctx, provider, token)
    try:
        result = function(*args)
        if inspect.isawaitable(result):
            result = await result
        if refresh is not None and isinstance(result, dict) and result.get("token_expired"):
            ctx.extension = extension_context(ctx, provider, await refresh())
            result = function(*args)
            if inspect.isawaitable(result):
                result = await result
        return result
    finally:
        ctx.extension = previous


# --- вызовы поставщика с приведением к контракту ------------------------------------


async def search(ctx, provider, query, kind="music"):
    """search поставщика → {"tracks": [Track], "start": int, "context": dict, "provider"}
    (окно очереди — первые MAX_QUEUE треков; трек без ссылки потока остаётся в очереди) или None — нет поставщика/пусто/сбой результата.
    Пустой результат с "note" — честный отказ поставщика с объяснением: dict с пустыми
    треками и note передаётся сценарию."""
    if provider is None:
        return None
    raw = await _call(ctx, provider, provider["search"], ctx, str(query or ""), str(kind or ""))
    return normalize_result(raw, provider)


def normalize_result(raw, provider):
    """Результат search поставщика → контракт ответа: треки нормализованы (без ссылки
    потока — остаются в очереди), очередь — первые MAX_QUEUE в порядке поставщика,
    start — играбельный трек (со ссылкой): указанный поставщиком, иначе первый со ссылкой; пустой результат с note — честный отказ с
    объяснением; None — не словарь, пусто или без note. Нужен и сценарию: на ступени
    свободных хвостов поиск делает диспетчер, и исполнитель получает уже сырой ответ
    поставщика."""
    if not isinstance(raw, dict):
        return None
    tracks = [track for track in (normalize_track(item, playable=False) for item in raw.get("tracks") or [])
              if track][:MAX_QUEUE]
    start = raw.get("start")
    start = int(start) if isinstance(start, (int, float)) and 0 <= int(start) < len(tracks) else 0
    if tracks and not (tracks[start].get("stream") or {}).get("url"):
        start = next((i for i, track in enumerate(tracks) if (track.get("stream") or {}).get("url")), None)
        if start is None:
            tracks = []
    if not tracks:
        note = str(raw.get("note") or "").strip()
        if note:
            return {"tracks": [], "start": 0,
                    "context": raw.get("context") if isinstance(raw.get("context"), dict) else {},
                    "note": note, "provider": provider}
        return None
    context = raw.get("context") if isinstance(raw.get("context"), dict) else {}
    return {"tracks": tracks, "start": start, "context": context, "provider": provider}


async def neighbor(ctx, provider, track_id, context, step=1):
    """neighbor поставщика → Track или None (дальше нечего/сбой)."""
    if provider is None:
        return None
    raw = await _call(ctx, provider, provider["neighbor"],
                      ctx, track_id, context if isinstance(context, dict) else None, int(step))
    return normalize_track(raw)


async def queue(ctx, provider):
    """Окно очереди поставщика (необязательная функция queue) для виджета: [Track]
    в порядке воспроизведения, пусто — функции нет или очереди нет."""
    if provider is None or provider.get("queue") is None:
        return []
    raw = await _call(ctx, provider, provider["queue"], ctx)
    tracks = (normalize_track(item, playable=False) for item in raw or [])
    return [track for track in tracks if track][:MAX_QUEUE]


async def rate(ctx, provider, track_id, like=True):
    """rate поставщика (необязательная) → True — поставщик подтвердил, иначе None."""
    if provider is None or provider.get("rate") is None:
        return None
    raw = await _call(ctx, provider, provider["rate"], ctx, track_id, bool(like))
    return True if raw else None


async def commit(ctx, provider, track_id):
    """commit поставщика (необязательная): колонка подтвердила старт трека (recoverCommit) —
    поставщик сверяет позицию очереди с играющим треком. Позицию двигает только он:
    neighbor предзагрузку (@@mm_stack_engine_get_next) и явные шаги её не сдвигают.
    True — подтвердил (трек из его очереди), иначе None."""
    if provider is None or provider.get("commit") is None:
        return None
    raw = await _call(ctx, provider, provider["commit"], ctx, str(track_id or ""))
    return True if raw else None


def normalize_track(raw, playable=True):
    """Track поставщика → контракт (недостающие обязательные — None): без id или
    title трек не показывается; без stream.url он не играет — playable=True (по
    умолчанию: neighbor, воспроизведение) отбрасывает такой трек, playable=False
    (очередь) оставляет его без ключа stream, ссылку добирает поставщик."""
    if not isinstance(raw, dict):
        return None
    track_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or "").strip()
    stream = raw.get("stream") if isinstance(raw.get("stream"), dict) else {}
    url = str(stream.get("url") or "").strip()
    if not track_id or not title or (playable and not url):
        return None
    track = {"id": track_id,
             "title": title,
             "artist": str(raw.get("artist") or ""),
             "duration_ms": int(raw.get("duration_ms") or 0) if raw.get("duration_ms") else 0}
    if url:
        track["stream"] = {"url": url,
                           "format": str(stream.get("format") or "mp3").strip().lower() or "mp3"}
        if stream.get("decode_key"):
            track["stream"]["decode_key"] = str(stream["decode_key"])
    for optional, key in (("album", "album"), ("cover_url", "cover_url")):
        if raw.get(key):
            track[optional] = str(raw[key])
    return track


# --- сборка стека директорий виджета (по образцу захвата 2026-11-02) -----------------


def provider_key(provider):
    """Имя из enum Alice.Music.MusicProvider для карт меты и поля Entities.MusicProvider:
    Яндекс.Музыка (service "yandex-music"; без service — id, совместимость) —
    MUSIC_PROVIDER_YANDEX_MUSIC, остальным значения в схеме нет — MUSIC_PROVIDER_UNSPECIFIED (0)."""
    if isinstance(provider, dict):
        pid = provider_service(provider) or str(provider.get("id") or "")
    else:  # строка-id (прежние вызовы)
        pid = str(provider or "")
    return PROVIDER_KEYS.get(pid, UNSPECIFIED_KEY)


def tts_placeholder():
    """tts_play_placeholder — каркас ответа (как у паузы, media_directives)."""
    from .media_directives import tts_placeholder as placeholder
    return placeholder()


def draw_player_play():
    """draw_animation_directive — анимация старта плеера (образец «включи музыку»:
    S3Directory animations/player_play)."""
    return _draw_animation(PLAYER_PLAY_ANIMATION)


def _draw_animation(path):
    return {"name": "draw_animation_directive", "type": "client_action", "is_led_silent": True,
            "analytics_type": "draw_animation_directive", "proto": ANIMATION_PROTO,
            "fields": {"Name": "draw_animation_directive",
                       "Animations": [{"S3Directory": {"Bucket": S3_BUCKET, "Path": path}}]}}


def show_player_screen(track):
    """show_audio_player_screen_directive — экран плеера с треком (образец:
    TShowAudioPlayerScreenDirective{TrackId})."""
    return {"name": "show_audio_player_screen_directive", "type": "client_action",
            "is_led_silent": True, "proto": SCREEN_PROTO, "fields": {"TrackId": str(track["id"])}}


def _track_meta(track):
    """Alice.Music.TrackMeta трека (образец sequence_state: id, album_id, artist_ids,
    title, image_url, duration_ms, artist_relations). Артист/альбом поставщика — строки:
    их имя и есть идентификатор в картах меты (свойства поставщика честнее выдуманных id)."""
    meta = {"id": str(track["id"]), "title": str(track.get("title") or "")}
    duration = int(track.get("duration_ms") or 0)
    if duration:
        meta["duration_ms"] = duration
    cover = str(track.get("cover_url") or "")
    if cover:
        meta["image_url"] = cover
        meta["background_image_url"] = cover
    artist = str(track.get("artist") or "")
    if artist:
        meta["artist_ids"] = [artist]
        meta["artist_relations"] = [{"id": artist}]
    album = str(track.get("album") or "")
    if album:
        meta["album_id"] = album
    return meta


def queue_directive(tracks, index, provider):
    """apply_player_playback_queue_directive — очередь из сущностей поиска (решение
    владельца, PLAN-spotify-music) по структуре образца: Entities (по треку, с провайдером),
    CurrentEntityIndex, карта ProviderMeta{провайдер: {TrackMetaRecords}}."""
    key = provider_key(provider)
    fields = {"MusicPlatformQueue": {
        "Entities": [{"MusicProvider": key,
                      "PlaybackEntity": {"TrackEntity": {"Track": {"Id": str(t["id"]),
                                                                   "Uuid": str(t["id"])}}}}
                     for t in tracks],
        "MusicPlatformMetaStorage": {"ProviderMeta": {key: {"TrackMetaRecords": {
            str(t["id"]): {"TrackMeta": _track_meta(t)} for t in tracks}}}}}}
    if tracks:
        fields["MusicPlatformQueue"]["CurrentEntityIndex"] = max(0, min(int(index), len(tracks) - 1))
    return {"name": "apply_player_playback_queue_directive", "type": "client_action",
            "is_led_silent": True, "proto": QUEUE_PROTO, "fields": fields}


def sequence_state_directive(current, tracks, provider, like_status=None, repeat_type=None):
    """set_audio_player_sequence_state_directive (TSetSequenceStateDirective):
    CurrentItem — играющий трек, MetaStorage — меты треков/артистов/альбомов очереди
    (карты ProviderTrackMeta/ProviderArtistMeta/ProviderAlbumMeta по образцу).
    repeat_type — повтор трека (запись .85 2026-11-12 13:57:05: RepeatType=
    REPEAT_TYPE_TRACK у CurrentItem.Track)."""
    key = provider_key(provider)
    # ProviderTrackMeta — карта провайдер → TrackMeta → track_id (map в proto; провайдер →
    # ключ из enum Alice.Music.MusicProvider, у не-Яндекса MUSIC_PROVIDER_UNSPECIFIED)
    meta = {"ProviderTrackMeta": {key: {"TrackMeta": {str(t["id"]): _track_meta(t) for t in tracks}}}}
    artists = {}
    for t in tracks:
        name = str(t.get("artist") or "")
        if name and name not in artists:
            artists[name] = {"id": name, "name": name}
    if artists:
        meta["ProviderArtistMeta"] = {key: {"ArtistMeta": artists}}
    albums = {}
    for t in tracks:
        album = str(t.get("album") or "")
        if album and album not in albums:
            record = {"id": album, "title": album}
            if t.get("cover_url"):
                record["image_url"] = str(t["cover_url"])
            albums[album] = record
    if albums:
        meta["ProviderAlbumMeta"] = {key: {"AlbumMeta": albums}}
    item = {"Id": str(current["id"]), "Uuid": str(current["id"])}
    if like_status:  # оценка играющего трека (образец «лайк»: LikeStatus текущего)
        item["LikeStatus"] = like_status
    if repeat_type:  # повтор играющего трека (образец «поставь на повтор»: RepeatType)
        item["RepeatType"] = repeat_type
    return {"name": "set_audio_player_sequence_state_directive", "type": "client_action",
            "is_led_silent": True, "proto": SEQSTATE_PROTO,
            "fields": {"SequenceState": {"CurrentItem": {"Track": item}, "MetaStorage": meta}}}


def audio_play_directive(track, offset_ms=0, commit_token=None):
    """audio_play со Struct-payload по образцу (захват 2026-11-02; минимум полей пробы 098
    играл ссылку): stream{url, type: Track, format, duration_ms?, decode_key?} +
    metadata{title, subtitle, art_image_url?} + screen_type Music. decode_key нужен только
    шифрованным потокам (PLAN-spotify-music, факты). commit_token — recoverCommit-колбэк
    (callbacks.on_started.payload.music): колонка сама подтверждает переход трека
    (TextInput @@mm_semantic_frame с тем же токеном)."""
    stream = dict(track.get("stream") or {})
    payload_stream = {"url": str(stream.get("url") or ""), "type": "Track",
                      "format": str(stream.get("format") or "mp3").upper()}
    duration = int(track.get("duration_ms") or 0)
    if duration:
        payload_stream["duration_ms"] = duration
    if stream.get("decode_key"):
        payload_stream["decode_key"] = str(stream["decode_key"])
    if offset_ms > 0:  # продолжение с места паузы (образец «продолжи»: stream.offset_ms)
        payload_stream["offset_ms"] = int(offset_ms)
    metadata = {"title": str(track.get("title") or ""),
                "subtitle": str(track.get("artist") or ""),
                "hide_progress_bar": False}
    if track.get("cover_url"):
        metadata["art_image_url"] = str(track["cover_url"])
    payload = {"stream": payload_stream, "metadata": metadata,
               "screen_type": "Music", "background_mode": "Ducking",
               "set_pause": False, "playback_speed": 1.0}
    if commit_token:
        payload["callbacks"] = {"on_started": {"payload": {"music": {
            "type": "recoverCommit", "recoverCommitToken": str(commit_token)}}}}
    return {"name": "audio_play", "type": "client_action", "is_led_silent": True,
            "payload": payload}


def start_reply(result, text="", commit_token=None):
    """Ответ на «включи музыку <что-то>»: полный стек виджета из результата search
    (образец «включи музыку»: анимация + экран + очередь + состояние + audio_play)."""
    tracks = result["tracks"]
    start = result["start"]
    provider = result["provider"]
    return {"text": text, "card": False, "directives": [
        tts_placeholder(), draw_player_play(), show_player_screen(tracks[start]),
        queue_directive(tracks, start, provider),
        sequence_state_directive(tracks[start], tracks, provider),
        audio_play_directive(tracks[start], commit_token=commit_token)]}


def _window(track, window):
    """(очередь, индекс трека): окно поставщика, если играющий трек в нём есть
    (виджет видит весь список), иначе очередь из одного трека."""
    tracks = list(window or [])
    index = next((i for i, item in enumerate(tracks) if str(item["id"]) == str(track["id"])), None)
    if index is None:
        return [track], 0
    tracks[index] = track  # у играющего — свежая ссылка потока
    return tracks, index


def step_reply(track, provider, text="", offset_ms=0, commit_token=None, window=None):
    """Ответ на «дальше/назад», колбэк @@mm_stack_engine_get_next и
    продолжение с паузы: тройка очередь+состояние+audio_play без экрана и анимации
    (образцы «дальше» и «продолжи»). window — окно очереди поставщика (music.queue):
    виджет получает весь список с индексом играющего трека; без окна — очередь из
    одного трека. offset_ms — позиция продолжения; commit_token — recoverCommit-колбэк."""
    tracks, index = _window(track, window)
    return {"text": text, "card": False, "directives": [
        tts_placeholder(),
        queue_directive(tracks, index, provider),
        sequence_state_directive(track, tracks, provider),
        audio_play_directive(track, offset_ms, commit_token)]}


def like_reply(track, provider):
    """Ответ на «лайк» (образец захвата 2026-11-02): анимация player_like, состояние
    очереди и мета очереди с LikeStatus играющего трека, tts_play_placeholder последним;
    audio_play нет — трек играет дальше. divui_patch_view/set_div_variables образца
    правят div-карточку плеера облака (CardId music_scenario_player_key_<трек>_key),
    которой эмулятор не показывает, — в ответ не входят."""
    key = provider_key(provider)
    queue = {"name": "apply_player_playback_queue_directive", "type": "client_action",
             "is_led_silent": True, "proto": QUEUE_PROTO,
             "fields": {"MusicPlatformQueue": {"MusicPlatformMetaStorage": {"ProviderMeta": {key: {
                 "TrackMetaRecords": {str(track["id"]): {"TrackMeta": {}, "LikeStatus": "LIKE_STATUS_LIKE"}}}}}}}}
    return {"text": LIKE_TEXT, "speech": LIKE_SPEECH, "directives": [
        _draw_animation(PLAYER_LIKE_ANIMATION),
        sequence_state_directive(track, [track], provider, "LIKE_STATUS_LIKE"),
        queue, tts_placeholder()]}


def dislike_reply(next_track, provider, commit_token=None, window=None):
    """Ответ на «дизлайк» (образец захвата 2026-11-02): анимация player_dislike, затем
    стек «дальше» на следующий трек (очередь, состояние, tts_play_placeholder, audio_play)
    с речью. Следующего нет — только анимация и речь. set_div_variables образца правит
    div-карточку облака — в ответ не входит."""
    directives = [_draw_animation(PLAYER_DISLIKE_ANIMATION)]
    if next_track is not None:
        tracks, index = _window(next_track, window)
        directives += [queue_directive(tracks, index, provider),
                       sequence_state_directive(next_track, tracks, provider),
                       tts_placeholder(), audio_play_directive(next_track, 0, commit_token)]
    else:
        directives.append(tts_placeholder())
    return {"text": DISLIKE_TEXT, "card": False, "directives": directives}  # карточки в образце нет


# --- recoverCommit: подтверждённый переход трека -----------------------------------
# audio_play несёт callbacks.on_started.payload.music {type: recoverCommit,
# recoverCommitToken}; колонка при старте трека возвращает токен TextInput'ом —
# сервер снимает токен и отвечает тихо (audio_play в ответ заменил бы начавшийся трек). Записи живут в памяти процесса (колонка могла
# переподключиться — сессия по SN, токены общие), чужой/устаревший токен — тихий ack.
COMMITS = {}  # token → {current_id, ext_id, ts}
COMMIT_KEEP_S = 600  # неполученный токен живёт 10 минут


def keep_commit(current, ext_id=""):
    """Токен для audio_play: запись с текущим треком (колонка вернёт его в recoverCommit)."""
    token = uuid.uuid4().hex
    now = time.time()
    for key in [k for k, v in COMMITS.items() if now - v["ts"] > COMMIT_KEEP_S]:
        COMMITS.pop(key, None)
    COMMITS[token] = {"current_id": str(current["id"]), "ext_id": str(ext_id or ""), "ts": now}
    return token


def take_commit(token):
    """Запись токена или None (чужой/устаревший); запись снимается при получении."""
    return COMMITS.pop(str(token or ""), None)
