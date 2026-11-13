"""Хранилище проектов анимаций: state/animations/<platform>/<id>.json.

Проект = {platform, id, title, kind (ring|panel|frontal|clock), size, fps, frames}
где frames — кадры в родном представлении:
  - ring/panel/oknix: .led-строки (список строк hex-цветов + delay)
  - clock: .scled кадры (список {brightnesses: [32 ints], delayMs})
  - frontal: ссылка на GIF в state/media (file + media_url)

CRUD + импорт файлов .led/.scled/.gif через API.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_ID_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")
KINDS = ("ring", "panel", "frontal", "clock", "oknix")


class AnimationProjectStore:
    """Хранилище проектов анимаций по платформам.

    Каждый проект — JSON-файл state/animations/<platform>/<id>.json.
    Медиа (GIF для frontend) — state/animations/media/<имя>.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.media = self.root / "media"
        self.root.mkdir(parents=True, exist_ok=True)
        self.media.mkdir(parents=True, exist_ok=True)

    # --- пути ----------------------------------------------------------------

    def _platform_dir(self, platform: str) -> Path:
        return self.root / platform

    def _project_path(self, platform: str, project_id: str) -> Path:
        return self._platform_dir(platform) / f"{project_id}.json"

    # --- валидация -----------------------------------------------------------

    @staticmethod
    def _validate_id(project_id: str) -> None:
        if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
            raise ValueError("id: строчные буквы/цифры/._- до 64 символов")

    @staticmethod
    def _validate_project(value: dict) -> dict:
        if not isinstance(value, dict):
            raise ValueError("проект: объект")
        project_id = value.get("id")
        if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
            raise ValueError("id: строчные буквы/цифры/._- до 64 символов")
        if not isinstance(value.get("title"), str) or not value["title"].strip():
            raise ValueError("title: непустая строка")
        kind = value.get("kind")
        if kind not in KINDS:
            raise ValueError(f"kind: один из {', '.join(KINDS)}")
        size = value.get("size")
        if not isinstance(size, list) or not all(isinstance(s, int) and s > 0 for s in size):
            raise ValueError("size: список положительных целых")
        fps = value.get("fps")
        if not isinstance(fps, (int, float)) or fps <= 0:
            raise ValueError("fps: положительное число")
        frames = value.get("frames")
        if not isinstance(frames, list):
            raise ValueError("frames: список кадров")
        return {
            "id": project_id,
            "title": value["title"].strip(),
            "kind": kind,
            "size": size,
            "fps": float(fps),
            "frames": frames,
            "platform": value.get("platform", ""),
        }

    # --- CRUD ----------------------------------------------------------------

    def list(self, platform: str) -> list[dict]:
        """Список проектов платформы (отсортировано по id)."""
        pdir = self._platform_dir(platform)
        if not pdir.is_dir():
            return []
        projects = []
        for path in sorted(pdir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                projects.append(data)
            except (OSError, ValueError) as exc:
                log.warning("Анимации: некорректный проект %s: %s", path, exc)
        return projects

    def get(self, platform: str, project_id: str) -> dict | None:
        """Проект по платформе и id. None — не найден."""
        path = self._project_path(platform, project_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put(self, platform: str, value: dict, media_b64: str | None = None) -> dict:
        """Создать/заменить проект. media_b64 — GIF (≤2 MiB) для kind=frontal."""
        self._validate_id(value.get("id", ""))
        if value.get("platform") and value["platform"] != platform:
            raise ValueError("platform в теле не совпадает с путем")
        entry = self._validate_project(value)
        entry["platform"] = platform

        if media_b64:
            raw = base64.b64decode(media_b64, validate=True)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("GIF до 2 MiБ")
            if not raw.startswith(b"GIF8"):
                raise ValueError("Медиа должна быть GIF")
            name = re.sub(r"[^a-z0-9_.-]", "_", entry["id"]) + ".gif"
            (self.media / name).write_bytes(raw)
            entry["media_file"] = name
            entry["media_sha256"] = hashlib.sha256(raw).hexdigest()
            entry["media_bytes"] = len(raw)

        pdir = self._platform_dir(platform)
        pdir.mkdir(parents=True, exist_ok=True)
        path = self._project_path(platform, entry["id"])
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(path)
        return entry

    def delete(self, platform: str, project_id: str) -> None:
        """Удалить проект. ValueError — не найден."""
        path = self._project_path(platform, project_id)
        if not path.is_file():
            raise ValueError("Проект не найден")
        path.unlink()

    def media_path(self, name: str) -> Path:
        """Путь к медиа-файлу (проверка на выход за директорию)."""
        path = (self.media / name).resolve()
        if path.is_relative_to(self.media.resolve()) and path.is_file():
            return path
        raise ValueError("Медиа не найдена")

    def import_file(self, platform: str, project_id: str, title: str,
                    kind: str, size: list[int], fps: float,
                    file_bytes: bytes, filename: str) -> dict:
        """Импортировать файл .led/.scled/.gif в проект.

        - .led: текст распарсивается в кадры (hex-цвета + delay)
        - .scled: текст или бинарник распарсивается в кадры яркостей
        - .gif: сохраняется как медиа, кадры — ссылка на медиа
        """
        if kind not in KINDS:
            raise ValueError(f"kind: один из {', '.join(KINDS)}")

        lower = filename.lower()
        if lower.endswith(".led"):
            text = file_bytes.decode("utf-8", errors="replace")
            from .animation_formats import parse_led_text
            result = parse_led_text(text, size[0] if size else 24)
            frames = [
                {"colors": [[c.r, c.g, c.b, c.w] for c in f.colors], "delayMs": f.delayMs}
                for f in result.frames
            ]
        elif lower.endswith(".scled"):
            from .animation_formats import parse_scled_bytes
            result = parse_scled_bytes(file_bytes)
            frames = [
                {"brightnesses": [c.r for c in f.colors], "delayMs": f.delayMs}
                for f in result.frames
            ]
        elif lower.endswith(".gif"):
            if not file_bytes.startswith(b"GIF8"):
                raise ValueError("Медиа должна быть GIF")
            if len(file_bytes) > 2 * 1024 * 1024:
                raise ValueError("GIF до 2 MiБ")
            name = re.sub(r"[^a-z0-9_.-]", "_", project_id) + ".gif"
            (self.media / name).write_bytes(file_bytes)
            frames = [{"media_file": name, "media_url": f"/emulator/api/animations-media?file={name}"}]
        else:
            raise ValueError("Поддерживаются только .led, .scled, .gif")

        return self.put(platform, {
            "id": project_id,
            "title": title or Path(filename).stem,
            "kind": kind,
            "size": size,
            "fps": fps,
            "platform": platform,
            "frames": frames,
        })