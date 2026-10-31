"""Домен settings: чтение/сохранение настроек эмулятора (и «Интеллект»). Guard (X-Emulator + same-origin) и маппинг ошибок
обеспечивает диспетчер реестра ui/api/__init__.py.
"""
from aiohttp import web

from . import route


@route("settings", methods=("GET", "POST", "PUT"))
async def settings(request, runtime, app):
    return web.json_response(runtime.public() if request.method == "GET" else await runtime.save_settings(await request.json()))


@route("intelligence", methods=("GET",))
async def intelligence(request, runtime, app):
    """Режимы «Интеллекта» для переключателя UI (TASK-034): встроенные уровни и режимы
    включённых расширений. preset/needs — passthrough из MODE (кнопка «Набор: всё через X»
    и учёт потребностей — TASK-035)."""
    from ...intelligence import LEVELS, extension_modes
    modes = []
    for mode in extension_modes(runtime):
        entry = {"id": mode["id"], "title": mode["title"], "ext": mode["ext"], "tabs": mode["tabs"]}
        if mode["preset"]:
            entry["preset"] = mode["preset"]  # набор «всё через X» — кнопка в карточке (TASK-035)
        if mode["needs"]:
            entry["needs"] = mode["needs"]
        modes.append(entry)
    return web.json_response({"levels": list(LEVELS), "modes": modes})
