"""Медиа-библиотека и плеер колонки (TASK-071): свои файлы вместо Яндекс.Музыки.

Библиотека — папки на диске: state/media/music/ и state/media/tales/ (подпапки —
плейлисты/жанры); эмулятор раздаёт файлы по http://<адрес колонки>/media/… с
поддержкой Range (перемотку делает aiohttp FileResponse). Интернет-радио —
список «имя → URL» в настройках (settings.media.radio). Метаданные — без
зависимостей: имя файла как название, подпапка как плейлист.

Директива плеера — audio_play с Struct-полезной нагрузкой (образец — журнал
2026-11-01, обмен 155): stream.url/type/format, metadata.title/subtitle,
screen_type Music, background_mode Ducking, set_pause. Очередь плеера — в памяти
runtime (media_state[sn]): сценарии «дальше/назад/пауза/продолжи/что играет» и
поставщики колбэков плеера (media.playback_ended, media.next_requested) читают
и двигают её через Player. Всё, что здесь, — публичные функции: py-сценарии
доступаются плеера через sdk (ctx.media, ctx.media_base).
"""
from __future__ import annotations

import re
import time
from pathlib import Path

KINDS = ("music", "tales")
KIND_TITLES = {"music": "музыка", "tales": "сказки"}
KIND_GENITIVE = {"music": "музыки", "tales": "сказок"}
AUDIO_TYPES = {".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".opus": "audio/opus",
               ".m4a": "audio/mp4", ".flac": "audio/flac", ".wav": "audio/wav", ".aac": "audio/aac"}
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
AUDIO_EXTENSIONS = tuple(AUDIO_TYPES)
MAX_FILE_MB = 200
MAX_RADIO = 20

# stream.format в директиве audio_play (образец: MP3) — по расширению файла.
FORMATS = {".mp3": "MP3", ".ogg": "OGG", ".opus": "OPUS", ".m4a": "M4A", ".aac": "AAC",
           ".flac": "FLAC", ".wav": "WAV"}


def content_type(path):
    return AUDIO_TYPES.get(path.suffix.lower()) or IMAGE_TYPES.get(path.suffix.lower()) or "application/octet-stream"


def base_url(host):
    """Адрес раздачи медиа для колонки: хост, по которому она достаёт эмулятор
    (хост WS-соединения, из записи сессии), порт по умолчанию — HTTP-порт сервера."""
    return "http://" + (host or "quasar.yandex.net") + "/media"


class MediaLibrary:
    """Файлы библиотеки: state/media/<kind>/, подпапки — плейлисты. Имя файла — название."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def root(self, kind):
        return self.directory / "media" / str(kind)

    def list(self, kind):
        """[(относительный путь, размер)] всех аудиофайлов вида; папки — пути."""
        root = self.root(kind)
        if not root.is_dir():
            return []
        found = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in AUDIO_TYPES:
                found.append((path.relative_to(root).as_posix(), path.stat().st_size))
        return found

    def folders(self, kind):
        """Подпапки (плейлисты), в которых есть файлы."""
        return sorted({posix.split("/", 1)[0] for posix, _size in self.list(kind) if "/" in posix})

    def search(self, kind, query):
        """Файлы с подстрокой запроса в пути (без учёта регистра, ё=е)."""
        query = (query or "").strip().lower().replace("ё", "е")
        if not query:
            return self.list(kind)
        return [(posix, size) for posix, size in self.list(kind) if query in posix.lower().replace("ё", "е")]

    def resolve(self, kind, relative):
        """Путь файла внутри библиотеки (без выхода за папку) или None."""
        root, target = self.root(kind).resolve(), (self.root(kind) / relative).resolve()
        if root not in target.parents and target != root:
            return None
        return target if target.is_file() else None

    def art_for(self, kind, relative):
        """Обложка рядом с файлом: <имя>.jpg/png/webp или cover.* в той же папке."""
        track = self.resolve(kind, relative)
        if track is None:
            return None
        for candidate in [track.with_suffix(suffix) for suffix in IMAGE_TYPES] + \
                [track.parent / ("cover" + suffix) for suffix in IMAGE_TYPES]:
            if candidate.is_file():
                return candidate
        return None


def play_payload(url, title, subtitle="", duration_ms=None, radio=False, art=None, pause=False):
    """Полезная нагрузка audio_play по образцу Яндекса (обмен 155, минимум полей)."""
    stream = {"url": url, "type": "Radio" if radio else "Track",
              "format": FORMATS.get(Path(url).suffix.lower(), "MP3")}
    if duration_ms:
        stream["duration_ms"] = round(float(duration_ms))
    metadata = {"title": str(title or "")}
    if subtitle:
        metadata["subtitle"] = str(subtitle)
    if art:
        metadata["art_image_url"] = str(art)
    return {"stream": stream, "metadata": metadata, "screen_type": "Music",
            "background_mode": "Ducking", "set_pause": bool(pause), "playback_speed": 1.0}


def audio_play_reply(payload, text=""):
    """Ответ сценария: реплика + директива audio_play (формат compose-ответа)."""
    return {"text": text, "card": False,
            "directives": [{"name": "audio_play", "analytics_type": "music", "is_led_silent": True,
                            "payload": payload}]}


class Player:
    """Плеер колонки: очередь в памяти runtime (media_state[sn]). Публичный фасад
    для py-сценариев (sdk: ctx.media). Все методы возвращают compose-ответ (или None —
    сказать «ничего не играет»)."""

    def __init__(self, runtime, sn, base=""):
        self.runtime = runtime
        self.sn = str(sn or "")
        self.base = (base or base_url("")).rstrip("/")

    def state(self):
        store = getattr(self.runtime, "media_state", None)
        if store is None:  # упрощённый runtime без плеера (реплей, dry-run)
            store = self.runtime.media_state = {}
        return store.setdefault(self.sn, {"queue": [], "index": 0})

    # --- библиотека → очередь ---------------------------------------------------
    def play_library(self, library, kind, query="", text=""):
        """Включить файлы вида (по запросу — подстрока): очередь = найденные, ответ с директивой.
        Пусто — честный ответ «библиотека пуста» (None — сказал вызывающий)."""
        found = library.search(kind, query)
        if not found:
            return None
        queue, index = self._queue_from(library, kind, found), 0
        return self.start(queue, index, text)

    def play_radio(self, name, url, text=""):
        """Поток радио: очередь из одного «трека» без перемотки."""
        track = {"kind": "radio", "name": name, "url": url, "radio": True}
        self.runtime.media_state[self.sn] = {"queue": [track], "index": 0, "kind": "radio"}
        payload = play_payload(url, title=name, radio=True)
        return audio_play_reply(payload, text)

    def _queue_from(self, library, kind, found):
        from urllib.parse import quote
        queue = []
        for posix, _size in found:
            art = library.art_for(kind, posix)
            queue.append({"kind": kind, "name": Path(posix).stem, "relative": posix,
                          "url": f"{self.base}/{kind}/{quote(posix)}", "radio": False,
                          "title": Path(posix).stem,
                          "subtitle": Path(posix).parent.as_posix() if "/" in posix else "",
                          "art": f"{self.base}/{kind}/{quote(art.relative_to(library.root(kind)).as_posix())}" if art else ""})
        return queue

    # --- управление -------------------------------------------------------------
    def start(self, queue, index=0, text=""):
        """Поставить очередь и включить трек index."""
        self.runtime.media_state[self.sn] = {"queue": list(queue), "index": max(0, min(index, len(queue) - 1)),
                                             "ts": time.time()}
        track = self.state()["queue"][self.state()["index"]]
        payload = play_payload(track["url"], title=track.get("title") or track.get("name") or "",
                               subtitle=track.get("subtitle") or "", art=track.get("art") or None,
                               radio=track.get("radio"), duration_ms=track.get("duration_ms"))
        return audio_play_reply(payload, text)

    def current(self):
        state = self.state()
        if state.get("queue") and 0 <= state["index"] < len(state["queue"]):
            return state["queue"][state["index"]]
        return None

    def step(self, delta):
        """Следующий/предыдущий трек очереди; очередь кончилась — None."""
        state = self.state()
        target = state["index"] + delta
        if not state.get("queue") or not 0 <= target < len(state["queue"]):
            return None
        state["index"] = target
        return self.start(state["queue"], target)

    def pause(self):
        track = self.current()
        if not track:
            return None
        payload = play_payload(track["url"], title=track.get("title") or "", pause=True,
                               radio=track.get("radio"))
        return audio_play_reply(payload)

    def resume(self):
        return self.step(0)

    # --- ответы сценариям ---------------------------------------------------------
    def now_playing(self):
        """«Что играет» или None."""
        track = self.current()
        if not track:
            return None
        title = track.get("title") or track.get("name") or ""
        subtitle = track.get("subtitle") or ""
        return {"text": ("Сейчас играет " + subtitle + " — " + title) if subtitle else ("Сейчас играет " + title),
                "card": False}
