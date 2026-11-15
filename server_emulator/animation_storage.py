"""Хранилище проектов анимаций: <корень>/<платформа>/<id>.json.

Проект: {platform, id, title, kind, size, loop_from, frames}. kind и size берутся из
реестра дисплеев платформы (animations.PLATFORM_DISPLAYS), от клиента принимается
только kind. Кадры — в родном представлении дисплея:
  ring / panel / oknix — {colors: [[r,g,b,w] × N], delayMs}  (.led)
  clock                — {brightnesses: [0..255 × 32], delayMs}  (.scled)
  frontal              — {colors: [[v,v,v] × W·H], delayMs}  (GIF 25×16 строится на лету;
                          дисплей чёрно-белый: r=g=b, ненулевое значение — пиксель
                          включён (255), нулевое — выключен (0); градаций нет)
loop_from — индекс кадра, с которого анимация зацикливается (-1 — без цикла).

Импорт .led/.scled/.gif, экспорт в родной формат, сборка GIF для draw_led_screen
(файл кладётся в <корень>/media и отдаётся доменом animations-media).
"""
from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

from . import animation_formats as formats

log = logging.getLogger(__name__)

PROJECT_ID_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")
MAX_FRAMES = 500
MAX_DELAY_MS = 600_000
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MIN_GIF_DELAY_MS = 20
# LED-экран играет GIF с постоянным шагом кадра 20 мс (как настоящие GIF эмоций: 50 кадров/с,
# каждый кадр 20 мс): задержка кадра проекта при сборке GIF раскладывается на повторы по 20 мс
GIF_STEP_MS = 20
MAX_GIF_MS = 60_000  # предельная длина frontal-анимации: 3000 кадров GIF по 20 мс


def _displays(platform: str) -> list[dict]:
    from .animations import platform_displays
    return platform_displays(platform)


def _display(platform: str, kind: str) -> dict:
    found = next((d for d in _displays(platform) if d["kind"] == kind), None)
    if found is None:
        raise ValueError(f"У платформы {platform} нет дисплея «{kind}»")
    return found


def _check_platform(platform) -> str:
    if not isinstance(platform, str) or not _displays(platform):
        raise ValueError("platform: платформа с дисплеем из реестра")
    return platform


def _check_id(project_id) -> str:
    if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError("id: строчные буквы/цифры/._- до 64 символов")
    return project_id


def _byte(value, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        raise ValueError(f"{what}: целое 0..255")
    return value


def _delay(value, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"delayMs: целое {minimum}..{maximum}")
    return value


def _frame(kind: str, display: dict, frame) -> dict:
    if not isinstance(frame, dict):
        raise ValueError("кадр: объект")
    if kind == "clock":
        values = frame.get("brightnesses")
        if not isinstance(values, list) or len(values) != formats.SCLED_LEDS:
            raise ValueError(f"brightnesses: список из {formats.SCLED_LEDS} яркостей")
        return {"brightnesses": [_byte(v, "яркость") for v in values],
                "delayMs": _delay(frame.get("delayMs"), 0, formats.MAX_DELAY_MS)}
    count = display["size"][0] * (display["size"][1] if len(display["size"]) > 1 else 1)
    colors = frame.get("colors")
    if not isinstance(colors, list) or len(colors) != count:
        raise ValueError(f"colors: список из {count} цветов")
    clean = []
    for color in colors:
        if not isinstance(color, list) or not 3 <= len(color) <= 4:
            raise ValueError("цвет: [r, g, b] или [r, g, b, w]")
        channels = [_byte(v, "канал цвета") for v in color]
        if kind == "frontal":
            if channels[0] != channels[1] or channels[1] != channels[2]:
                raise ValueError("frontal: дисплей монохромный, r=g=b")
            clean.append([255, 255, 255] if channels[0] else [0, 0, 0])   # пиксель только вкл/выкл
        else:
            clean.append(channels + [0] * (4 - len(channels)))
    low = MIN_GIF_DELAY_MS if kind == "frontal" else 0
    return {"colors": clean, "delayMs": _delay(frame.get("delayMs"), low, MAX_DELAY_MS)}


def validate_project(platform: str, value: dict) -> dict:
    """Проверенный проект с kind/size из реестра платформы."""
    _check_platform(platform)
    if not isinstance(value, dict):
        raise ValueError("проект: объект")
    title = value.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title: непустая строка")
    kind = value.get("kind")
    display = _display(platform, kind)
    frames = value.get("frames")
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
        raise ValueError(f"frames: от 1 до {MAX_FRAMES} кадров")
    loop_from = value.get("loop_from", -1)
    clean = [_frame(kind, display, f) for f in frames]
    if kind == "frontal" and sum(f["delayMs"] for f in clean) > MAX_GIF_MS:
        raise ValueError(f"frontal: анимация длиннее {MAX_GIF_MS // 1000} с")
    if isinstance(loop_from, bool) or not isinstance(loop_from, int) or not -1 <= loop_from < len(frames):
        raise ValueError("loop_from: -1 (без цикла) или индекс кадра")
    return {"platform": platform, "id": _check_id(value.get("id")), "title": title.strip(),
            "kind": kind, "size": list(display["size"]), "loop_from": loop_from,
            "frames": clean}


class AnimationProjectStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.media = self.root / "media"
        self.root.mkdir(parents=True, exist_ok=True)
        self.media.mkdir(parents=True, exist_ok=True)

    def _path(self, platform: str, project_id: str) -> Path:
        return self.root / _check_platform(platform) / f"{_check_id(project_id)}.json"

    def list(self, platform: str) -> list[dict]:
        directory = self.root / _check_platform(platform)
        projects = []
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
            try:
                projects.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                log.warning("Анимации: пропущен повреждённый проект %s: %s", path, exc)
        return projects

    def get(self, platform: str, project_id: str) -> dict | None:
        path = self._path(platform, project_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put(self, platform: str, value: dict) -> dict:
        entry = validate_project(platform, value)
        path = self._path(platform, entry["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        return entry

    def delete(self, platform: str, project_id: str) -> None:
        path = self._path(platform, project_id)
        if not path.is_file():
            raise ValueError("Проект не найден")
        path.unlink()
        (self.media / self._gif_name(platform, project_id)).unlink(missing_ok=True)

    # --- импорт / экспорт -------------------------------------------------------

    def import_file(self, platform: str, project_id: str, title: str, kind: str,
                    data: bytes, filename: str) -> dict:
        """Файл .led / .scled / .gif → проект платформы (кадры по размеру дисплея)."""
        display = _display(_check_platform(platform), kind)
        if len(data) > MAX_IMPORT_BYTES:
            raise ValueError("Файл до 2 МиБ")
        extension = Path(filename or "").suffix.lower()
        loop_from = -1
        if extension == ".led" and kind in ("ring", "panel", "oknix"):
            parsed = formats.parse_led_text(data.decode("utf-8", errors="replace"), display["size"][0])
            frames, loop_from = parsed.frames, parsed.loop_from
        elif extension == ".scled" and kind == "clock":
            parsed = formats.parse_scled_bytes(data)
            frames, loop_from = parsed.frames, parsed.loop_from
        elif extension == ".gif" and kind == "frontal":
            frames, loop_from = _gif_frames(data, tuple(display["size"]))
        else:
            raise ValueError(f"Файл {extension or 'без расширения'} не подходит дисплею «{kind}»: "
                             "ring/panel/oknix — .led, clock — .scled, frontal — .gif")
        if not frames:
            raise ValueError("В файле нет кадров")
        return self.put(platform, {"id": project_id, "title": title or Path(filename).stem,
                                   "kind": kind, "loop_from": loop_from, "frames": frames})

    def export(self, platform: str, project_id: str) -> tuple[str, str, bytes]:
        """(имя файла, content-type, содержимое) в родном формате дисплея."""
        project = self.get(platform, project_id)
        if project is None:
            raise ValueError("Проект не найден")
        kind, frames, loop_from = project["kind"], project["frames"], project["loop_from"]
        if kind == "clock":
            return (f"{project_id}.scled", "text/plain; charset=utf-8",
                    formats.build_scled_text(frames, loop_from).encode("utf-8"))
        if kind == "frontal":
            return f"{project_id}.gif", "image/gif", render_gif(project)
        return (f"{project_id}.led", "text/plain; charset=utf-8",
                formats.build_led_text(frames, loop_from, project_id).encode("utf-8"))

    def ring_file(self, platform: str, project_id: str) -> bytes:
        """Файл кольца для каталога draw_animation_directive (форма файла S3)."""
        project = self.get(platform, project_id)
        if project is None:
            raise ValueError("Проект не найден")
        if project["kind"] != "ring":
            raise ValueError("Проект не для кольца")
        return formats.build_ring_s3_text(project["frames"], project["loop_from"]).encode("utf-8")

    @staticmethod
    def _gif_name(platform: str, project_id: str) -> str:
        return f"project.{platform}.{project_id}.gif"

    def publish_gif(self, project: dict) -> str:
        """GIF проекта frontal → media/; возвращает имя файла для animations-media."""
        name = self._gif_name(project["platform"], project["id"])
        (self.media / name).write_bytes(render_gif(project))
        return name


# --- GIF (Pillow) ------------------------------------------------------------

def _pillow():
    from PIL import Image
    return Image


def render_gif(project: dict) -> bytes:
    """Кадры frontal → GIF 2 цвета: кадр проекта — round(delayMs / 20) одинаковых кадров по
    20 мс (шаг LED-экрана), метка цикла Netscape, если задан loop_from. Pillow при save_all
    склеивает одинаковые соседние кадры в один с суммой задержек, поэтому кадры пишутся его
    покадровыми getheader/getdata."""
    Image = _pillow()
    from PIL import GifImagePlugin
    width, height = project["size"]
    palette = [0, 0, 0, 255, 255, 255]
    images = []
    for frame in project["frames"]:
        image = Image.new("P", (width, height))
        image.putpalette(palette)
        image.putdata([1 if c[0] else 0 for c in frame["colors"]])
        images.append((image, max(1, round(frame["delayMs"] / GIF_STEP_MS))))
    info = {"loop": 0} if project["loop_from"] >= 0 else {}
    header, _ = GifImagePlugin.getheader(images[0][0], info=info)
    out = [*header]
    for image, repeats in images:
        data = b"".join(GifImagePlugin.getdata(image, duration=GIF_STEP_MS, disposal=1))
        out.extend([data] * repeats)
    out.append(b";")
    return b"".join(out)


def _gif_frames(data: bytes, size: tuple) -> tuple[list[dict], int]:
    """GIF → (кадры frontal, loop_from). Другой размер — масштаб к размеру матрицы:
    уменьшение усредняет по площади (Resampling.BOX — при сильном сжатии сосед
    теряет линии), увеличение — ближайшим соседом. Частичные кадры GIF (дельта
    с disposal) Pillow композитит с предыдущими при seek — кадр полный.
    Дисплей чёрно-белый, градаций нет: кадр переводится в серый и порогом (127) —
    в пиксели вкл/выкл; цветной источник не отклоняется — приводится к тому, что
    реально показала бы колонка. Одинаковые соседние кадры (после порога) — один кадр с
    суммой задержек; длиннее MAX_GIF_MS и MAX_FRAMES кадров — обрезается. Цикл источника (метка Netscape loop) — loop_from=0:
    частичный цикл GIF не выражает, повторяется целиком; без метки — играет раз."""
    Image = _pillow()
    if not data.startswith(b"GIF8"):
        raise ValueError("Файл не GIF")
    try:
        gif = Image.open(io.BytesIO(data))
        loop_from = 0 if gif.info.get("loop", -1) >= 0 else -1
        frames, total = [], 0
        for index in range(getattr(gif, "n_frames", 1)):
            gif.seek(index)
            image = gif.convert("RGBA")
            background = Image.new("RGBA", image.size, (0, 0, 0, 255))
            image = Image.alpha_composite(background, image).convert("L")
            if image.size != size:
                if image.size[0] > size[0] or image.size[1] > size[1]:
                    image = image.resize(size, Image.Resampling.BOX)
                else:
                    image = image.resize(size, Image.Resampling.NEAREST)
            delay = max(MIN_GIF_DELAY_MS, min(int(gif.info.get("duration", 100) or 100), MAX_DELAY_MS))
            delay = min(delay, MAX_GIF_MS - total)
            if delay <= 0:
                break
            total += delay
            colors = [[255, 255, 255] if v > 127 else [0, 0, 0] for v in image.tobytes()]
            if frames and frames[-1]["colors"] == colors and frames[-1]["delayMs"] + delay <= MAX_DELAY_MS:
                frames[-1]["delayMs"] += delay      # повтор кадра (шаг 20 мс экспорта) — одна задержка
                continue
            if len(frames) == MAX_FRAMES:
                break
            frames.append({"colors": colors, "delayMs": delay})
    except (OSError, EOFError, ValueError) as exc:
        raise ValueError(f"GIF не разобран: {exc}") from None
    return frames, loop_from
