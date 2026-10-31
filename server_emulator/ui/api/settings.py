"""Домены settings и mode: чтение/сохранение настроек эмулятора и переключение режима (RF-03).

Код перенесён из runtime.py (функция legacy_api) дословно; guard
(X-Emulator + same-origin) и маппинг ошибок обеспечивает диспетчер
реестра ui/api/__init__.py.
"""
from aiohttp import web

from . import route


@route("settings", methods=("GET", "POST", "PUT"))
async def settings(request, runtime, app):
    return web.json_response(runtime.public() if request.method == "GET" else await runtime.save_settings(await request.json(), preserve_mode=True))


@route("mode", methods=("GET", "POST", "PUT"))
async def mode(request, runtime, app):
    from ...runtime import MODES, STANDALONE_KEY  # внутри обработчика: избегаем цикла импортов
    if request.method=='GET':
        return web.json_response({'mode':runtime.settings['mode'],'modes':list(MODES),
                                  'excluded':runtime.settings.get('emulation_excluded') or []})
    value = await request.json()
    if app.get(STANDALONE_KEY) and value.get('mode') != 'emulate':
        raise ValueError('Standalone работает только в эмуляции; для проксирования нужен компонент прокси')
    return web.json_response(await runtime.set_mode(value['mode'],value.get('connections','wait')))


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
