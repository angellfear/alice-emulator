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

import json
import re
import time
from pathlib import Path

KINDS = ("music", "tales", "sounds")
KIND_TITLES = {"music": "музыка", "tales": "сказки", "sounds": "звуки природы"}
KIND_GENITIVE = {"music": "музыки", "tales": "сказок", "sounds": "звуков природы"}
AUDIO_TYPES = {".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".opus": "audio/opus",
               ".m4a": "audio/mp4", ".flac": "audio/flac", ".wav": "audio/wav", ".aac": "audio/aac"}
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
AUDIO_EXTENSIONS = tuple(AUDIO_TYPES)
MAX_FILE_MB = 200
MAX_RADIO = 20

# stream.format в директиве audio_play (образец: MP3) — по расширению файла.
FORMATS = {".mp3": "MP3", ".ogg": "OGG", ".opus": "OPUS", ".m4a": "M4A", ".aac": "AAC",
           ".flac": "FLAC", ".wav": "WAV"}


def _shuffled(queue):
    """Перемешанная копия очереди."""
    import random
    order = list(queue)
    random.Random(time.time_ns()).shuffle(order)
    return order


def content_type(path):
    return AUDIO_TYPES.get(path.suffix.lower()) or IMAGE_TYPES.get(path.suffix.lower()) or "application/octet-stream"


def base_url(host):
    """Адрес раздачи медиа для колонки: хост, по которому она достаёт эмулятор
    (хост WS-соединения, из записи сессии), порт по умолчанию — HTTP-порт сервера."""
    return "http://" + (host or "quasar.yandex.net") + "/media"


class MediaLibrary:
    """Файлы библиотеки: state/media/<kind>/, подпапки — плейлисты. Название —
    тег файла (ID3/Vorbis), если читается, иначе имя файла; поиск — по тегам
    и путям с склонением по основе («майкла» найдёт «Майкл»)."""

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

    def tags(self, kind, relative):
        """Теги файла {title, artist, album, genre} — пустые, если не читаются."""
        path = self.resolve(kind, relative)
        if path is None:
            return {}
        return read_tags(path)

    def search(self, kind, query):
        """Файлы по запросу: каждое слово запроса должно найтись по основе среди
        слов пути и тегов (регистр и ё не важны, «майкла» → «майкл»); предлоги и
        союзы поиска не касаются. Пустой запрос — вся библиотека."""
        words = [word for word in clean_words(query) if word not in SEARCH_SKIP]
        if not words:
            return self.list(kind)
        found = []
        for posix, size in self.list(kind):
            tokens = clean_words(posix.rsplit(".", 1)[0].replace("/", " "))
            tokens += [word for tag in self.tags(kind, posix).values()
                       for word in clean_words(tag)]
            if all(_word_found(word, tokens) for word in words):
                found.append((posix, size))
        return found

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


def clean_words(text):
    """Слова запроса/пути: нижний регистр, ё→е, пунктуация в пробелы."""
    text = re.sub(r"[^\w\s]", " ", str(text or "").lower().replace("ё", "е"))
    return [word for word in text.split(" ") if word]


def _stem(word):
    """Основа слова: без хвостовых гласных (падежные окончания) — «майкла» → «майкл»,
    «русскую» → «русск» (= «русская» → «русск»); короткие слова целиком."""
    if len(word) <= 3:
        return word
    cut = word.rstrip("аеёиоуыэюя")
    return cut if len(cut) >= 3 else word


# Служебные слова запроса: поиска не касаются (в файлах и тегах их нет).
SEARCH_SKIP = {"для", "и", "с", "со", "в", "во", "на", "по", "из", "под", "the", "а"}


def _word_found(word, tokens):
    """Слово запроса найдено: основа слова — начало какого-то токена или основа
    токена — начало слова («майкла»/«майкл», «ромашки»/«ромашка»)."""
    stem = _stem(word)
    return any(token.startswith(stem) or word.startswith(_stem(token)) for token in tokens)


_TAG_CACHE = {}


def read_tags(path):
    """Теги аудиофайла без зависимостей: ID3v2 (mp3), Vorbis-комментарии (ogg, flac).
    Битый/нечитаемый файл — пустые теги; результат кешируется по пути и mtime."""
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key in _TAG_CACHE:
        return _TAG_CACHE[key]
    tags = {}
    try:
        data = path.read_bytes()
        if data[:3] == b"ID3":
            tags = _id3_tags(data)
        elif data[:4] == b"fLaC":
            tags = _vorbis_tags(data[data.find(b"fLaC") + 4:], flac=True)
        elif data[:4] == b"OggS":
            tags = _vorbis_tags(data)
        tags = {key: value for key, value in tags.items() if value}
    except Exception:  # noqa: BLE001 — побитый тег не должен ломать библиотеку
        tags = {}
    if len(_TAG_CACHE) > 512:
        _TAG_CACHE.clear()
    _TAG_CACHE[key] = tags
    return tags


_ID3_TEXT = {"TIT2": "title", "TT2": "title", "TPE1": "artist", "TP1": "artist",
             "TALB": "album", "TAL": "album", "TCON": "genre", "TCO": "genre"}


def _id3_text(raw):
    """Строка текстового кадра ID3: первый байт — кодировка."""
    encodings = ((0, "latin-1"), (1, "utf-16"), (2, "utf-16-be"), (3, "utf-8"))
    if not raw:
        return ""
    codec = next((name for byte, name in encodings if raw[0] == byte), "latin-1")
    body = raw[1:].split(b"\x00", 1)[0] if raw[0] == 0 else raw[1:].split(b"\x00\x00", 1)[0] \
        if codec.startswith("utf-16") else raw[1:].split(b"\x00", 1)[0]
    try:
        return body.decode(codec, "replace").strip().strip("\ufeff")
    except LookupError:
        return ""


def _id3_tags(data):
    """Текстовые кадры ID3v2 (2.2–2.4): заголовок, размер тега synchsafe (2.3+)."""
    version = data[3]
    size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
    tags, offset = {}, 10
    end = min(10 + size, len(data))
    id_length = 3 if version <= 2 else 4
    header = 6 if version <= 2 else 10
    while offset + header <= end:
        frame_id = data[offset:offset + id_length].decode("ascii", "replace").upper()
        if frame_id not in _ID3_TEXT:
            break  # за пределами текстовых кадров (или паддинг) — дальше не читаем
        if version <= 2:
            frame_size = int.from_bytes(data[offset + 3:offset + 6], "big")
        else:
            raw = data[offset + 4:offset + 8]
            frame_size = int.from_bytes(raw, "big")
            if version == 3:
                frame_size = int.from_bytes(raw, "big")
            else:  # v2.4 — synchsafe
                frame_size = (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]
        if frame_size <= 0 or offset + header + frame_size > end:
            break
        value = _id3_text(data[offset + header:offset + header + frame_size])
        if value:
            tags[_ID3_TEXT[frame_id.upper()]] = value
        offset += header + frame_size
    return tags


def _vorbis_comments(chunk):
    """Vorbis-комментарии: vendor-строка, счётчик, «KEY=value» UTF-8."""
    tags, offset = {}, 0
    vendor_len = int.from_bytes(chunk[offset:offset + 4], "little")
    offset += 4 + vendor_len
    count = int.from_bytes(chunk[offset:offset + 4], "little")
    offset += 4
    for _ in range(min(count, 64)):
        if offset + 4 > len(chunk):
            break
        length = int.from_bytes(chunk[offset:offset + 4], "little")
        offset += 4
        entry = chunk[offset:offset + length].decode("utf-8", "replace")
        offset += length
        key, _, value = entry.partition("=")
        name = {"TITLE": "title", "ARTIST": "artist", "ALBUM": "album", "GENRE": "genre"}.get(key.upper())
        if name and value and name not in tags:
            tags[name] = value
    return tags


def _vorbis_tags(data, flac=False):
    """Комментарии из Ogg (заголовок \x03vorbis / OpusTags) или FLAC (блок type 4;
    data — уже после метки fLaC)."""
    if flac:
        offset = 0  # блоки {last, type, size} сразу за меткой
        while offset + 4 <= len(data):
            head = data[offset]
            size = int.from_bytes(data[offset + 1:offset + 4], "big")
            if head & 0x7F == 4:
                return _vorbis_comments(data[offset + 4:offset + 4 + size])
            offset += 4 + size
        return {}
    for marker in (b"\x03vorbis", b"OpusTags"):
        at = data.find(marker)
        if at != -1:
            return _vorbis_comments(data[at + len(marker):])
    return {}


# --- избранное и история прослушивания ----------------------------------------------

def _marks_file(library):
    return library.directory / "media" / "likes.json"


def load_marks(library):
    """Отметки треков {user: {kind: {"like": [путь], "dislike": [путь]}}}; битый файл — пусто."""
    try:
        data = json.loads(_marks_file(library).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_marks(library, marks):
    _marks_file(library).write_text(json.dumps(marks, ensure_ascii=False, indent=1), encoding="utf-8")


def mark_track(library, user, kind, relative, mark):
    """Отметка трека: like (избранное), dislike (не предлагать) или None (снять)."""
    marks = load_marks(library)
    owner = marks.setdefault(str(user or ""), {})
    shelf = owner.setdefault(str(kind), {})
    for name in ("like", "dislike"):
        listed = [item for item in shelf.get(name, []) if item != relative]
        shelf[name] = listed
    if mark in ("like", "dislike"):
        shelf.setdefault(mark, []).append(relative)
    save_marks(library, marks)


def marked_tracks(library, user, kind, mark):
    return list(load_marks(library).get(str(user or ""), {}).get(str(kind), {}).get(mark, []))


def search_marked(library, kind, query, mark, user="", exclude=()):
    """Отмеченные файлы вида (среди них — поиск по запросу, если он есть)."""
    wanted = set(marked_tracks(library, user, kind, mark))
    found = [(posix, size) for posix, size in library.search(kind, query)
             if posix in wanted and posix not in exclude]
    return found


def play_history(runtime):
    """История прослушивания {user: {«kind/путь»: timestamp}} — в памяти runtime."""
    store = getattr(runtime, "media_history", None)
    if not isinstance(store, dict):
        store = runtime.media_history = {}
    return store


def note_played(runtime, user, kind, relative):
    play_history(runtime).setdefault(str(user or ""), {})["{}/{}".format(kind, relative)] = time.time()


def least_played(library, runtime, user, kind):
    """Файл, который давно не играли этому пользователю: из неигранных — случайный."""
    history = play_history(runtime).get(str(user or ""), {})
    played = {key.split("/", 1)[1]: ts for key, ts in history.items()
              if key.startswith(str(kind) + "/")}
    never = [(posix, size) for posix, size in library.list(kind) if posix not in played]
    if never:
        return never[time.time_ns() % len(never)]
    oldest = min(played.values(), default=0)
    candidates = [posix for posix, ts in played.items() if ts == oldest]
    return next(((posix, size) for posix, size in library.list(kind) if posix in candidates), None)


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
    def play_library(self, library, kind, query="", text="", user="", mode=None):
        """Включить файлы вида (по запросу — поиск по тегам и путям). Режимы:
        «shuffle» (вперемешку), «repeat» (первый найденный — на повторе), «new»
        (давно не игранное). Без запроса вся библиотека играется вперемешку;
        дизлайки не предлагаются. Пусто — None (сказал вызывающий)."""
        if mode == "new":
            one = least_played(library, self.runtime, user, kind)
            found = [one] if one else []
        else:
            found = library.search(kind, query)
        disliked = set(marked_tracks(library, user, kind, "dislike"))
        found = [item for item in found if item[0] not in disliked]
        if not found:
            return None
        queue = self._queue_from(library, kind, found)
        if mode == "repeat":
            queue = queue[:1]
        elif mode == "shuffle" or (not query and len(queue) > 1):
            queue = _shuffled(queue)
        note_played(self.runtime, user, kind, found[0][0])  # «что-нибудь новенькое» считает это
        return self.start(queue, 0, text)

    def play_marked(self, library, kind, user, mark, text=""):
        """Избранное («нравится»): отмеченные файлы вперемешку. Пусто — None."""
        wanted = set(marked_tracks(library, user, kind, mark))
        found = [(posix, size) for posix, size in library.list(kind) if posix in wanted]
        if not found:
            return None
        queue = self._queue_from(library, kind, found)
        return self.start(_shuffled(queue) if len(queue) > 1 else queue, 0, text)

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
            tags = library.tags(kind, posix)
            title = tags.get("title") or Path(posix).stem
            subtitle = tags.get("artist") or (Path(posix).parent.as_posix() if "/" in posix else "")
            queue.append({"kind": kind, "name": Path(posix).stem, "relative": posix,
                          "url": f"{self.base}/{kind}/{quote(posix)}", "radio": False,
                          "title": title, "subtitle": subtitle,
                          "art": f"{self.base}/{kind}/{quote(art.relative_to(library.root(kind)).as_posix())}" if art else ""})
        return queue

    def _play_current(self, pause=False, speed=None):
        """Директива для текущего трека (пауза/продолжение/скорость)."""
        track = self.current()
        if not track:
            return None
        payload = play_payload(track["url"], title=track.get("title") or track.get("name") or "",
                               subtitle=track.get("subtitle") or "", art=track.get("art") or None,
                               radio=track.get("radio"), duration_ms=track.get("duration_ms"),
                               pause=pause)
        if speed is not None:
            payload["playback_speed"] = round(float(speed), 2)
        return audio_play_reply(payload)

    # --- управление -------------------------------------------------------------
    def start(self, queue, index=0, text=""):
        """Поставить очередь и включить трек index."""
        self.runtime.media_state[self.sn] = {"queue": list(queue), "index": max(0, min(index, len(queue) - 1)),
                                             "ts": time.time()}
        track = self.state()["queue"][self.state()["index"]]
        payload = play_payload(track["url"], title=track.get("title") or track.get("name") or "",
                               subtitle=track.get("subtitle") or "", art=track.get("art") or None,
                               radio=track.get("radio"), duration_ms=track.get("duration_ms"))
        if track.get("speed") is not None:
            payload["playback_speed"] = track["speed"]
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

    def restart(self):
        """«Начни сначала»: та же очередь и трек, директива заново (с начала файла)."""
        state = self.state()
        if not state.get("queue"):
            return None
        return self.start(state["queue"], state.get("index", 0))

    def repeat(self):
        """«Зациклить трек»: очередь из одного текущего трека."""
        track = self.current()
        if not track:
            return None
        return self.start([dict(track)], 0)

    def pause(self):
        return self._play_current(pause=True) if self.current() else None

    def resume(self):
        return self.step(0) if self.current() else None

    def set_speed(self, speed):
        """Скорость воспроизведения (поле playback_speed из образца директивы)."""
        try:
            speed = round(float(str(speed).replace(",", ".")), 2)
        except (TypeError, ValueError):
            return None
        speed = max(0.5, min(3.0, speed))
        track = self.current()
        if not track:
            return None
        track["speed"] = speed
        self.state()["speed"] = speed
        return self._play_current(speed=speed)

    def get_speed(self):
        """Текущая скорость или None — ничего не играет."""
        if not self.current():
            return None
        return self.state().get("speed", 1.0)

    def another_version(self, library):
        """Другой файл с тем же названием («включи другую версию»); нет — None."""
        track = self.current()
        if not track:
            return None
        kind = track.get("kind") or "music"
        tokens = set(clean_words(Path(track.get("relative", "")).stem))
        for posix, _size in library.list(kind):
            if posix == track.get("relative"):
                continue
            if tokens and tokens & set(clean_words(Path(posix).stem)):
                return self.start(self._queue_from(library, kind, [(posix, 0)]), 0)
        return None

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
