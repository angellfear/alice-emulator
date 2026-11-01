"""Домен media: библиотека музыки/сказок и радио (TASK-071).

GET api/media → {music: {folders, files, count}, tales: …, radio, base} — каталог
state/media/<kind> (файлы + подпапки-плейлисты) и список радио из настроек.
POST api/media/upload?kind=<music|tales>&folder=<подпапка> — тело файла целиком,
имя в заголовке X-Filename (как /api/stt); до 200 МБ.
DELETE api/media?kind=&name=<путь> — удалить файл. Папки, ставшие пустыми,
уходят.
PUT api/media {"radio": [{name, url}]} — сохранить список радио (validate
настроек). Колонке раздачу ведёт маршрут /media/… (runtime.py, Range поддержан).
"""
import re

from aiohttp import web

from ...media import KINDS, MAX_FILE_MB, MediaLibrary
from . import route

SAFE_NAME = re.compile(r"[^\w .()\[\]-]", re.UNICODE)


def _kind(value):
    if value not in KINDS:
        raise ValueError("kind: " + " | ".join(KINDS))
    return value


def _view(library, kind):
    files = library.list(kind)
    return {"folders": library.folders(kind),
            "files": [{"name": name, "size": size} for name, size in files],
            "count": len(files)}


@route("media", methods=("GET", "POST", "PUT", "DELETE"))
async def media(request, runtime, app):
    library = MediaLibrary(runtime.directory)
    if request.method == "GET":
        return web.json_response({kind: _view(library, kind) for kind in KINDS}
                                 | {"radio": (runtime.settings.get("media") or {}).get("radio") or []})
    if request.method == "POST" and request.headers.get("X-Filename"):
        return await _upload(request, runtime, library)
    if request.method == "PUT":
        value = await request.json()
        radio = (value or {}).get("radio")
        if radio is None:
            raise ValueError("Ожидался {radio: [{name, url}]}")
        await runtime.save_settings({**runtime.settings, "media": {"radio": radio}})
        return web.json_response({"saved": True, "radio": (runtime.settings.get("media") or {}).get("radio") or []})
    if request.method == "DELETE":
        kind = _kind(request.query.get("kind", ""))
        name = request.query.get("name", "")
        path = library.resolve(kind, name)
        if path is None:
            raise ValueError("Файл не найден: " + name)
        path.unlink()
        root = library.root(kind).resolve()  # подпапки, ставшие пустыми, уходят
        for parent in path.parents:
            if parent == root:
                break
            try:
                parent.rmdir()
            except OSError:
                break
        return web.json_response({"deleted": name})
    raise ValueError("POST — загрузка файла (X-Filename) или неопознанная форма")


async def _upload(request, runtime, library):
    """Загрузка файла: raw body, имя в X-Filename, папка — query folder (создаётся)."""
    kind = _kind(request.query.get("kind", ""))
    filename = request.headers.get("X-Filename", "")
    name = SAFE_NAME.sub("_", filename.strip()) or ""
    if not name or ".." in name or name.startswith("."):
        raise ValueError("Нужен заголовок X-Filename с именем файла")
    folder = "/".join(SAFE_NAME.sub("_", part).strip() for part in request.query.get("folder", "").split("/") if part.strip())
    relative = (folder + "/" if folder else "") + name
    root = library.root(kind).resolve()
    target = (root / relative).resolve()
    if root not in target.parents or target.suffix.lower() not in {".mp3", ".ogg", ".opus", ".m4a", ".flac", ".wav", ".aac"}:
        raise ValueError("Файл: аудио " + ", ".join((".mp3", ".ogg", ".opus", ".m4a", ".flac", ".wav", ".aac")))
    body = await request.read()
    if len(body) > MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"Файл больше {MAX_FILE_MB} МБ")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return web.json_response({"saved": relative, "size": len(body)})
