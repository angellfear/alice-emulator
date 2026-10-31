"""Домен setup: первый запуск — состояние протокола/картинок, загрузка протокола, загрузка картинок.

GET api/setup → {schemas, protocol_files, images, images_total, images_busy, images_error}.
POST api/setup/protocol (multipart, один файл: libquasar_daemons.so или zip с .proto / .pb) — поток пишется
во временный файл в state, а не в память; после разбора файл удаляется. Разбор тяжёлый — в потоке executor.
POST api/setup/images {"accept": true} — скачать картинки устройств с веб-версии умного дома
(device_images.py); без согласия пользователя (галочка в окне настройки) — 400.
"""
import asyncio
import shutil
import uuid

from aiohttp import web

from ... import protocol_import
from ...device_images import ImageFetchError
from . import route

UPLOAD_LIMIT = 1024 ** 3  # 1 ГБ на запрос


def state(runtime):
    return {**protocol_import.setup_status(), **runtime.device_images.status()}


@route("setup", methods=("GET",))
async def setup_state(request, runtime, app):
    return web.json_response(state(runtime))


async def refresh_images(request, runtime):
    """POST api/setup/images: скачать картинки по согласию. Нет сети — 502 с понятным текстом, UI — с силуэтами."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("accept") is not True:
        return web.json_response({"error": "Нужно согласие: картинки принадлежат Яндексу", **state(runtime)},
                                 status=400)
    try:
        await runtime.device_images.refresh()
    except ImageFetchError as exc:
        status = 409 if runtime.device_images.busy else 502
        return web.json_response({"error": str(exc), **state(runtime)}, status=status)
    return web.json_response({"ok": True, **state(runtime)})


async def upload(request, runtime):
    """POST api/setup/protocol (только админ — как остальные admin-API; guard X-Emulator + same-origin)."""
    busy = runtime.__dict__.setdefault("_setup_lock", asyncio.Lock())
    if busy.locked():
        return web.json_response({"error": "Разбор протокола уже идёт, дождитесь окончания"}, status=409)
    if not request.content_type.startswith("multipart/"):
        raise ValueError("Нужен один файл: libquasar_daemons.so или zip с .proto / .pb")
    async with busy:
        temp_dir = runtime.directory / "setup-tmp" / uuid.uuid4().hex
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            paths, size = [], 0
            async for part in await request.multipart():
                if not part.filename:
                    continue
                target = temp_dir / f"{len(paths)}.bin"  # имя из запроса на диск не попадает
                with target.open("wb") as handle:
                    while chunk := await part.read_chunk(1 << 20):
                        size += len(chunk)
                        if size > UPLOAD_LIMIT:
                            raise ValueError("Файл больше 1 ГБ")
                        handle.write(chunk)
                paths.append(target)
            if len(paths) != 1:
                raise ValueError("Нужен один файл: libquasar_daemons.so или zip с .proto / .pb")
            try:
                result = await asyncio.get_running_loop().run_in_executor(None, protocol_import.import_source, paths[0])
            except protocol_import.ProtocolImportError:
                raise
            except Exception as exc:  # noqa: BLE001 — битый файл и т.п. → понятная ошибка в окне настройки
                raise ValueError(f"Не удалось разобрать файл: {exc}") from exc
            return web.json_response({"ok": True, "result": result, **state(runtime)})
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
