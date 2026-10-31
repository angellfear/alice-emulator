"""Раздел «Анимации»: каталог LED-анимаций станций (JSON + GIF) с разделением
по устройствам.

Модель — реестр способностей (display: led_matrix|seven_segment|none): ДуоМакс
без анимаций, Мини — 7-сегментный дисплей, LED-матрица — emotions v2/v3.
Запись анимации: {id, title, models[], url, file, sha256, bytes, source};
GIF — формат wire (frontal_led_image). Локальные копии — data/animations/media,
оригинальный URL станция качает как настоящий wire. Каталог data/animations/
catalog.json — источник правды (правки при остановленном сервере возможны).

Проигрывание — Push-директива draw_led_screen в живую сессию SN (payload по
капче 2026-10-24). Подтверждение экраном станции не верифицировано — delivery
'sent/unconfirmed', как у остальных Push-действий панели.

Расширения (TASK-032): папка animations/ включённого расширения добавляет
записи поверх каталога (только чтение, класс code им не нужен — data):
*.json — запись анимации (url или frames, локальный GIF рядом — file), id без
префикса получает <ext-id>., у записи origin «extension:<id>»; пользовательская
запись каталога с тем же id побеждает; выключение расширения убирает записи;
некорректная запись — warning без падения раздела. Слои — extensions/layers
(source_layers/layer_records), callable ставит runtime (конструктор ниже).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

DISPLAYS = ("led_matrix", "seven_segment", "none")
ANIMATION_MODELS = {
    "led_matrix": {"title": "LED-матрица (глазки)", "display": "led_matrix",
                   "note": "emotions v2/v3, как у станции из капчи (.227)"},
    "mini": {"title": "Станция Мини", "display": "seven_segment",
             "note": "анимации на 7-сегментном дисплее"},
    "duo_max": {"title": "Станция ДуоМакс", "display": "none",
                "note": "анимаций нет — раздел честно пуст"},
}
DEFAULT_DISPLAY = "led_matrix"  # модель не указана: предполагаем способность

CAPTURED = [
    ("builtin.led.joy", "Глазки (радость)", "emotions/v3/joy.gif"),
    ("builtin.led.wink", "Подмигнуть", "emotions/v3/wink.gif"),
    ("builtin.led.animate_in", "Появление", "emotions/v2/animate_in.gif"),
    ("builtin.led.joy_v2", "Радость v2", "emotions/v2/joy.gif"),
    ("builtin.led.neutral", "Нейтрально", "emotions/v2/neutral.gif"),
    ("builtin.weather.clear_night", "Погода: ясно/ночь", "animations/weather/clear/night"),
    ("builtin.player.play", "Плеер: играет", "animations/player_play"),
    ("builtin.player.pause", "Плеер: пауза", "animations/player_pause"),
]
YANDEX_LED = "https://static-alice.s3.yandex.net/led-production/"


def display_of(model):
    entry = ANIMATION_MODELS.get(model or "")
    return (entry or {}).get("display", DEFAULT_DISPLAY)


class AnimationStore:
    """Каталог + медиа; файл-источник правды, кеш в памяти."""

    def __init__(self, root, extension_dirs=None):  # root = data/animations
        self.root = Path(root)
        self.media = self.root / "media"
        self.path = self.root / "catalog.json"
        self.extension_dirs = extension_dirs  # callable → [(origin, Path)] — слои animations/, TASK-032
        self.root.mkdir(parents=True, exist_ok=True)
        self.media.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.data = data if isinstance(data, dict) else {}
        entries = self.data.setdefault("animations", [])
        known = {a.get("id") for a in entries}
        for identifier, title, tail in CAPTURED:  # семена из капч, не перетирают правки
            if identifier not in known:
                entries.append({"id": identifier, "title": title, "models": [],
                                "url": YANDEX_LED + tail + (".gif" if not tail.endswith((".gif",)) else ""),
                                "file": None, "sha256": None, "bytes": None,
                                "source": "capture", "added_ts": time.time()})
        self.save()

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(self.path)

    # --- слой расширений (TASK-032) ---------------------------------------------------

    def _extension_layers(self):
        """Слои animations/ включённых расширений: [(origin, Path)] (callable из runtime —
        extensions/layers.source_layers). Слой не задан или сбой — пусто, раздел работает."""
        if self.extension_dirs is None:
            return []
        try:
            return [(origin, Path(path)) for origin, path in self.extension_dirs()]
        except Exception:  # noqa: BLE001 — расширения не должны ломать раздел
            return []

    def _extension_entries(self):
        """Записи анимаций расширений: {id: запись} — *.json файла (validate, как у
        пользовательских), id без префикса получает <ext-id>.; некорректная запись —
        warning, раздел работает. Поздний слой перекрывает ранний."""
        from .extensions.layers import layer_records

        def warn(message):
            log.warning("Расширения (анимации): %s", message)

        entries = {}
        for origin, path, value in layer_records(self._extension_layers(), warn=warn):
            ext_id = origin.split(":", 1)[1] if str(origin).startswith("extension:") else ""
            if not isinstance(value, dict):
                warn(f"{path}: нужен JSON-объект с записью анимации")
                continue
            value = dict(value)
            identifier = value.get("id") or path.stem
            if ext_id and not str(identifier).startswith(ext_id + "."):
                identifier = f"{ext_id}.{identifier}"
            value["id"] = identifier
            try:
                entry = self.validate(value)
            except ValueError as exc:
                warn(f"{path}: {exc}")
                continue
            entry["origin"] = origin
            entries[entry["id"]] = entry
        return entries

    def list(self):
        """Каталог: пользовательские записи (catalog.json) + записи включённых
        расширений (только чтение; пользовательская запись с тем же id побеждает,
        TASK-032). Каждая запись помечена origin: user | extension:<id>."""
        extension = self._extension_entries()
        result = []
        for animation in self.data.get("animations", []):
            extension.pop(animation.get("id"), None)
            result.append({**animation, "origin": "user"})
        result.extend(extension.values())
        return json.loads(json.dumps(result, ensure_ascii=False))

    def get(self, identifier):
        found = next((a for a in self.data["animations"] if a.get("id") == identifier), None)
        if found is not None:
            return found  # пользовательская запись побеждает запись расширения
        return self._extension_entries().get(identifier)

    def url_of(self, identifier):
        animation = self.get(identifier)
        return (animation or {}).get("url") or ""

    def validate(self, value):
        if not isinstance(value, dict):
            raise ValueError("Анимация: объект")
        identifier = value.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9_.-]{1,64}", identifier):
            raise ValueError("id: строчные буквы/цифры/._- до 64 символов")
        if not isinstance(value.get("title"), str) or not value["title"].strip():
            raise ValueError("title: непустая строка")
        models = value.get("models", [])
        if not isinstance(models, list) or any(not isinstance(m, str) for m in models):
            raise ValueError("models: список идентификаторов моделей")
        unknown = [m for m in models if m not in ANIMATION_MODELS]
        if unknown:
            raise ValueError(f"неизвестные модели: {unknown}")
        frames = value.get("frames") or []
        if not isinstance(frames, list) or any(not isinstance(f, str) for f in frames):
            raise ValueError("frames: список id кадров-анимаций (последовательность wire)")
        entry = {"id": identifier, "title": value["title"].strip(), "models": models,
                 "frames": frames, "file": value.get("file"), "sha256": value.get("sha256"),
                 "bytes": value.get("bytes"), "source": value.get("source") or "manual",
                 "added_ts": value.get("added_ts") or time.time()}
        if frames:
            entry["url"] = value.get("url") or ""  # последовательность: свой url не обязателен
            return entry
        url = value.get("url")
        if not isinstance(url, str) or not re.fullmatch(r"https?://[^\s]+", url):
            raise ValueError("url: http(s)-ссылка на GIF (или соберите последовательность frames)")
        entry["url"] = url
        return entry

    def sequence_urls(self, identifier):
        """Кадры анимации в порядке wire: для простой — [url], для frames — urls кадров."""
        entry = self.get(identifier)
        if entry is None:
            return []
        if entry.get("frames"):
            urls = []
            for frame in entry["frames"]:
                child = self.get(frame)
                urls.append((child or {}).get("url") or "")
            return [u for u in urls if u]
        return [entry["url"]] if entry.get("url") else []

    def put(self, value, media_b64=None):
        """Создание/замена; media_b64 — GIF (≤2 МиБ) сохраняется в media/."""
        entry = self.validate(value)
        previous = self.get(entry["id"])
        if media_b64:
            raw = base64.b64decode(media_b64, validate=True)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("GIF до 2 МиБ")
            if not raw.startswith(b"GIF8"):
                raise ValueError("Медиа должна быть GIF")
            name = re.sub(r"[^a-z0-9_.-]", "_", entry["id"]) + ".gif"
            (self.media / name).write_bytes(raw)
            entry.update({"file": name, "sha256": hashlib.sha256(raw).hexdigest(),
                          "bytes": len(raw), "source": "manual"})
        elif previous and previous.get("file") and entry.get("url") == previous.get("url"):
            entry["file"], entry["sha256"], entry["bytes"] = previous["file"], previous["sha256"], previous["bytes"]
        replaced = [a for a in self.data["animations"] if a["id"] != entry["id"]]
        self.data["animations"] = replaced + [entry]
        self.save()
        return entry

    def delete(self, identifier):
        before = len(self.data["animations"])
        self.data["animations"] = [a for a in self.data["animations"] if a["id"] != identifier]
        if len(self.data["animations"]) == before:
            raise ValueError("Анимация не найдена")
        self.save()

    def media_path(self, name):
        path = (self.media / name).resolve()
        if path.is_relative_to(self.media.resolve()) and path.is_file():
            return path
        for _origin, directory in self._extension_layers():  # GIF из папки расширения (TASK-032)
            root = Path(directory).resolve()
            candidate = (root / name).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                return candidate
        raise ValueError("Медиа не найдена")

    def for_model(self, model):
        """Записи, доступные модели (пустой models = все устройства с анимациями)."""
        capable = display_of(model) != "none"
        return [a for a in self.list() if capable and (not a.get("models") or model in a["models"])]


async def play_on_device(runtime, identifier, sn):
    """Push draw_led_screen в живую сессию SN; способности модели проверяются."""
    animation = runtime.animations.get(identifier)
    if animation is None:
        raise ValueError("Анимация не найдена")
    device = next((d for d in runtime.identities.data["devices"] if d.get("sn") == sn), None)
    if display_of((device or {}).get("model")) == "none":
        raise ValueError("У этой модели анимаций нет (см. реестр моделей)")
    urls = runtime.animations.sequence_urls(identifier)
    if not urls:
        raise ValueError("Анимация без кадров: задайте url или frames")
    return await runtime.push_action(sn, "draw_led_screen", {"frames": urls})
