"""Музыка: поставщик контента и сборка стека директорий виджета плеера (TASK-099).

Точка подключения расширений music/*.py — по образцу datasets/*.py (реестр собирает
runtime.rebuild_extension_registries; конфликт имён или битый модуль отключает
расширение целиком, как у actions/datasets):

    PROVIDER = {"id": "spotify", "title": "Spotify"}

    async def search(ctx, query, kind):     # kind: music|tales|podcast|audiobook; "" — «что-нибудь»
        return {"tracks": [Track, ...], "start": 0, "context": {...}}

    async def neighbor(ctx, track_id, context, step):   # step +1/-1; None — дальше нечего
        return Track | None

    async def rate(ctx, track_id, like):    # необязательна; None — не поддерживается

    Track = {"id", "title", "artist", "album"?, "duration_ms", "cover_url"?,
             "stream": {"url", "format": "mp3|aac|flac", "decode_key"?}}

Выбранный поставщик — настройка settings["music"]["provider"] (id; пусто — первый
включённый по алфавиту). ctx.music (SDK) — запись выбранного поставщика или None.
Вызов функций поставщика — только через search()/neighbor()/rate() этого модуля:
они ставят коду поставщика его настройки ctx.extension (как у режимов «Интеллекта»,
TASK-053) и приводят Track/результат к контракту.

Своей очереди ядро не хранит (главное правило tasks/PLAN-station-skills): треки отдаёт
поставщик, контекст очереди — его search/neighbor, состояние плеера — колонка
(ctx.player). «дальше/назад» голосом идут в neighbor без track_id (колонка не
сообщает идентификатор в запросе — только Title/SubTitle в ctx.player): поставщик
ведёт свою очередь сам.

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

import inspect
from pathlib import Path

MAX_QUEUE = 5  # очередь поиска: топ-5 результатов (решение владельца, PLAN-spotify-music)

ANIMATION_PROTO = "NAlice.TAnimationCapability.TDrawAnimationDirective"
SCREEN_PROTO = "NAlice.TAudioPlayerScreenCapability.TShowAudioPlayerScreenDirective"
QUEUE_PROTO = "NAlice.TMusicPlatformQueueCapability.TApplyMusicPlatformQueueDirective"
SEQSTATE_PROTO = "NAlice.TAudioPlayerCapability.TSetSequenceStateDirective"
S3_BUCKET = "https://quasar.s3.yandex.net"
PLAYER_PLAY_ANIMATION = "animations/player_play"  # образец «включи музыку» (захват 2026-11-02)

YANDEX_PROVIDER_ID = "yandex-music"
# имена из enum Alice.Music.MusicProvider для карт меты и поля Entities.MusicProvider:
PROVIDER_KEYS = {YANDEX_PROVIDER_ID: "MUSIC_PROVIDER_YANDEX_MUSIC"}
UNSPECIFIED_KEY = "MUSIC_PROVIDER_UNSPECIFIED"

PROVIDERS: dict[str, dict] = {}  # id → запись поставщика (реестр пересобирает Runtime)
_COLLECTED: list = []  # накопитель за пересборку реестра (как drop_extensions у aapi_handlers)


# --- реестр поставщиков (пересборка из music/*.py включённых расширений) -------------

def reset():
    """Начать пересборку реестра (runtime.rebuild_extension_registries)."""
    _COLLECTED.clear()


def collect(ext_id, paths, runner):
    """Модули music/*.py одного расширения → накопитель. Битый модуль, PROVIDER без id
    или без search/neighbor — ValueError: расширение не включается (как у actions)."""
    for path in paths:
        for file in sorted(Path(path).glob("*.py")):
            try:
                module = runner._module(file)
            except Exception as exc:  # noqa: BLE001 — битый модуль отключает расширение
                raise ValueError(f"{file.name}: модуль не загружается "
                                 f"({type(exc).__name__}: {str(exc)[:150]})") from exc
            provider = getattr(module, "PROVIDER", None)
            if provider is None:
                continue  # вспомогательный модуль без PROVIDER — не участвует
            if not isinstance(provider, dict) or not str(provider.get("id") or "").strip():
                raise ValueError(f"{file.name}: PROVIDER — объект с непустым id")
            pid = str(provider["id"]).strip()
            search, neighbor = getattr(module, "search", None), getattr(module, "neighbor", None)
            if not callable(search) or not callable(neighbor):
                raise ValueError(f"{file.name}: нужны async search(ctx, query, kind) и "
                                 f"neighbor(ctx, track_id, context, step)")
            if any(item["id"] == pid for item in _COLLECTED):
                raise ValueError(f"Имя поставщика {pid!r} уже занято другим расширением")
            rate = getattr(module, "rate", None)
            _COLLECTED.append({"id": pid, "title": str(provider.get("title") or pid),
                               "ext_id": str(ext_id), "file": str(file),
                               "search": search, "neighbor": neighbor,
                               "rate": rate if callable(rate) else None})


def apply():
    """Завершить пересборку: реестр = накопленное за успешный проход."""
    PROVIDERS.clear()
    PROVIDERS.update({item["id"]: item for item in _COLLECTED})
    _COLLECTED.clear()


def selected(settings):
    """Выбранный поставщик: settings["music"]["provider"] (id); пусто/нет — первый
    включённый по алфавиту id. Выбранный id не в реестре (расширение выключили) — None."""
    want = str(((settings or {}).get("music") or {}).get("provider") or "").strip()
    if want:
        return PROVIDERS.get(want)
    for pid in sorted(PROVIDERS):
        return PROVIDERS[pid]
    return None


def extension_context(ctx, provider):
    """ExtensionContext расширения-поставщика на этот вызов (настройки свежие) или None."""
    runtime = ctx._services_runtime()
    manager = getattr(runtime, "extensions", None)
    build = getattr(manager, "context", None)
    return build(provider["ext_id"]) if callable(build) else None


async def _call(ctx, provider, function, *args):
    """Вызов функции поставщика: его расширение — ctx.extension на время вызова
    (как у режимов «Интеллекта»; прежнее значение восстанавливается)."""
    previous = ctx.extension
    ctx.extension = extension_context(ctx, provider)
    try:
        result = function(*args)
        if inspect.isawaitable(result):
            result = await result
        return result
    finally:
        ctx.extension = previous


# --- вызовы поставщика с приведением к контракту ------------------------------------


async def search(ctx, provider, query, kind="music"):
    """search поставщика → {"tracks": [Track], "start": int, "context": dict} (очередь
    режется до MAX_QUEUE) или None — нет поставщика/пусто/сбой результата."""
    if provider is None:
        return None
    raw = await _call(ctx, provider, provider["search"], ctx, str(query or ""), str(kind or ""))
    if not isinstance(raw, dict):
        return None
    tracks = [track for track in (normalize_track(item) for item in raw.get("tracks") or [])
              if track][:MAX_QUEUE]
    if not tracks:
        return None
    start = raw.get("start")
    start = int(start) if isinstance(start, (int, float)) and 0 <= int(start) < len(tracks) else 0
    context = raw.get("context") if isinstance(raw.get("context"), dict) else {}
    return {"tracks": tracks, "start": start, "context": context, "provider": provider}


async def neighbor(ctx, provider, track_id, context, step=1):
    """neighbor поставщика → Track или None (дальше нечего/сбой)."""
    if provider is None:
        return None
    raw = await _call(ctx, provider, provider["neighbor"],
                      ctx, track_id, context if isinstance(context, dict) else None, int(step))
    return normalize_track(raw)


async def rate(ctx, provider, track_id, like=True):
    """rate поставщика (необязательная) → True — поставщик подтвердил, иначе None."""
    if provider is None or provider.get("rate") is None:
        return None
    raw = await _call(ctx, provider, provider["rate"], ctx, track_id, bool(like))
    return True if raw else None


def normalize_track(raw):
    """Track поставщика → контракт (недостающие обязательные — None): без id,
    title или stream.url трек не играет и не показывается."""
    if not isinstance(raw, dict):
        return None
    track_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or "").strip()
    stream = raw.get("stream") if isinstance(raw.get("stream"), dict) else {}
    url = str(stream.get("url") or "").strip()
    if not track_id or not title or not url:
        return None
    track = {"id": track_id,
             "title": title,
             "artist": str(raw.get("artist") or ""),
             "duration_ms": int(raw.get("duration_ms") or 0) if raw.get("duration_ms") else 0,
             "stream": {"url": url,
                        "format": str(stream.get("format") or "mp3").strip().lower() or "mp3"}}
    if stream.get("decode_key"):
        track["stream"]["decode_key"] = str(stream["decode_key"])
    for optional, key in (("album", "album"), ("cover_url", "cover_url")):
        if raw.get(key):
            track[optional] = str(raw[key])
    return track


# --- сборка стека директорий виджета (по образцу захвата 2026-11-02) -----------------


def provider_key(provider_id):
    """Имя из enum Alice.Music.MusicProvider для карт меты и поля Entities.MusicProvider:
    yandex-music — MUSIC_PROVIDER_YANDEX_MUSIC, остальным значения в схеме нет —
    MUSIC_PROVIDER_UNSPECIFIED (0)."""
    return PROVIDER_KEYS.get(str(provider_id or ""), UNSPECIFIED_KEY)


def tts_placeholder():
    """tts_play_placeholder — каркас ответа (как у паузы, media_directives)."""
    from .media_directives import tts_placeholder as placeholder
    return placeholder()


def draw_player_play():
    """draw_animation_directive — анимация старта плеера (образец «включи музыку»:
    S3Directory animations/player_play)."""
    return {"name": "draw_animation_directive", "type": "client_action", "is_led_silent": True,
            "analytics_type": "draw_animation_directive", "proto": ANIMATION_PROTO,
            "fields": {"Name": "draw_animation_directive",
                       "Animations": [{"S3Directory": {"Bucket": S3_BUCKET,
                                                       "Path": PLAYER_PLAY_ANIMATION}}]}}


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


def queue_directive(tracks, index, provider_id):
    """apply_player_playback_queue_directive — очередь из сущностей поиска (решение
    владельца, PLAN-spotify-music) по структуре образца: Entities (по треку, с провайдером),
    CurrentEntityIndex, карта ProviderMeta{провайдер: {TrackMetaRecords}}."""
    key = provider_key(provider_id)
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


def sequence_state_directive(current, tracks, provider_id):
    """set_audio_player_sequence_state_directive (TSetSequenceStateDirective):
    CurrentItem — играющий трек, MetaStorage — меты треков/артистов/альбомов очереди
    (карты ProviderTrackMeta/ProviderArtistMeta/ProviderAlbumMeta по образцу)."""
    key = provider_key(provider_id)
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
    return {"name": "set_audio_player_sequence_state_directive", "type": "client_action",
            "is_led_silent": True, "proto": SEQSTATE_PROTO,
            "fields": {"SequenceState": {
                "CurrentItem": {"Track": {"Id": str(current["id"]), "Uuid": str(current["id"])}},
                "MetaStorage": meta}}}


def audio_play_directive(track):
    """audio_play со Struct-payload по образцу (захват 2026-11-02; минимум полей пробы 098
    играл ссылку): stream{url, type: Track, format, duration_ms?, decode_key?} +
    metadata{title, subtitle, art_image_url?} + screen_type Music. decode_key нужен только
    шифрованным потокам (PLAN-spotify-music, факты)."""
    stream = dict(track.get("stream") or {})
    payload_stream = {"url": str(stream.get("url") or ""), "type": "Track",
                      "format": str(stream.get("format") or "mp3").upper()}
    duration = int(track.get("duration_ms") or 0)
    if duration:
        payload_stream["duration_ms"] = duration
    if stream.get("decode_key"):
        payload_stream["decode_key"] = str(stream["decode_key"])
    metadata = {"title": str(track.get("title") or ""),
                "subtitle": str(track.get("artist") or ""),
                "hide_progress_bar": False}
    if track.get("cover_url"):
        metadata["art_image_url"] = str(track["cover_url"])
    return {"name": "audio_play", "type": "client_action", "is_led_silent": True,
            "payload": {"stream": payload_stream, "metadata": metadata,
                        "screen_type": "Music", "background_mode": "Ducking",
                        "set_pause": False, "playback_speed": 1.0}}


def start_reply(result, text=""):
    """Ответ на «включи музыку <что-то>»: полный стек виджета из результата search
    (образец «включи музыку»: анимация + экран + очередь + состояние + audio_play)."""
    tracks = result["tracks"]
    start = result["start"]
    provider_id = result["provider"]["id"]
    return {"text": text, "card": False, "directives": [
        tts_placeholder(), draw_player_play(), show_player_screen(tracks[start]),
        queue_directive(tracks, start, provider_id),
        sequence_state_directive(tracks[start], tracks, provider_id),
        audio_play_directive(tracks[start])]}


def step_reply(track, provider, text=""):
    """Ответ на «дальше/назад» и колбэк @@mm_stack_engine_get_next: тройка
    очередь+состояние+audio_play без экрана и анимации (образец «дальше»; очередь из
    одного трека — neighbor по контракту 089 отдаёт один Track)."""
    provider_id = provider["id"] if provider else ""
    tracks = [track]
    return {"text": text, "card": False, "directives": [
        tts_placeholder(),
        queue_directive(tracks, 0, provider_id),
        sequence_state_directive(track, tracks, provider_id),
        audio_play_directive(track)]}
