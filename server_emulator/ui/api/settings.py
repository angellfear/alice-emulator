"""Домен settings: чтение/сохранение настроек эмулятора (и «Интеллект»). Guard (X-Emulator + same-origin) и маппинг ошибок
обеспечивает диспетчер реестра ui/api/__init__.py.
"""
import copy

from aiohttp import web

from . import route


@route("settings", methods=("GET", "POST", "PUT"))
async def settings(request, runtime, app):
    if request.method == "GET":
        return web.json_response(runtime.public())
    value = await request.json()
    # TASK-088: полная копия формы, открытая до чужого сохранения (например, вкладка «Голос»
    # применила источник ASR), — устаревшая: сервер отвечает 409, UI перечитывает настройки
    # и сохраняет заново. `_rev` присылают свои формы; запросы без него (внешние интеграции,
    # старые клиенты) сохраняются как раньше.
    rev = value.get("_rev") if isinstance(value, dict) else None
    if rev is not None and rev != runtime.settings_rev:
        return web.json_response({"error": "Настройки здесь уже изменились — перечитываем и сохраняем заново",
                                  "stale": True}, status=409)
    return web.json_response(await runtime.save_settings(value))


@route("intelligence", methods=("GET", "PUT"))
async def intelligence(request, runtime, app):
    """Режимы «Интеллекта» для переключателя UI (TASK-034): встроенные уровни и режимы
    включённых расширений. preset/needs — passthrough из MODE (кнопка «Набор: всё через X»
    и учёт потребностей — TASK-035).

    PUT (TASK-088): смена режима точечно — сохраняется только ключ `intelligence`. Прежний
    полный PUT копией настроек откатывал stt/tts/model/voice_stack к значениям формы,
    открытой до выбора источника во вкладке «Голос», — распознавание замолкало.
    Прогрев звукового кеша не запускается: смена режима не должна занимать локальный
    речевой сервер, пока колонка говорит."""
    if request.method == "PUT":
        body = await request.json()
        value = body.get("intelligence") if isinstance(body, dict) else None
        if not isinstance(value, str) or not value:
            raise ValueError("intelligence: строка scenarios | model | agent | ext:<id>")
        settings = copy.deepcopy(runtime.settings)  # от валидатора — та же проверка формата, что и при полном сохранении
        settings["intelligence"] = value
        return web.json_response(await runtime.save_settings(settings, warm=False))
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
