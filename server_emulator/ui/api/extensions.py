"""Домен extensions (TASK-029): список расширений и источников, установка с
подтверждением класса code/install, включение/выключение, обновление, откат,
удаление, настройки расширения, python-зависимости. Логика — в
extensions/manager.ExtensionManager; домен только отображает её на API.
"""
from aiohttp import web

from . import route


@route("extensions", methods=("GET", "POST"))
async def extensions(request, runtime, app):
    manager = runtime.extensions
    if request.method == "GET":
        # Список: источник, версия, доступное обновление (из освежённого каталога),
        # класс, разрешения, что добавляет, состояние. Сети при GET нет.
        return web.json_response(manager.listing())
    value = await request.json()
    op = value.get("op")
    if op == "add_source":
        return web.json_response(await manager.add_source(str(value.get("url", ""))))
    if op == "remove_source":
        return web.json_response(await manager.remove_source(str(value.get("url", ""))))
    if op == "catalog":  # свежий список каталога-индекса (сеть)
        return web.json_response({"entries": await manager.catalog(str(value.get("url", "")))})
    if op == "install":  # без confirm для code/install — карточка подтверждения
        return web.json_response(await manager.install(value, confirm=bool(value.get("confirm"))))
    if op == "enable":
        return web.json_response(await manager.enable(str(value.get("id", ""))))
    if op == "disable":
        return web.json_response(await manager.disable(str(value.get("id", ""))))
    if op == "update":  # версия источника должна быть новее; code/install — как install
        return web.json_response(await manager.update(str(value.get("id", "")), confirm=bool(value.get("confirm"))))
    if op == "rollback":
        return web.json_response(await manager.rollback(str(value.get("id", ""))))
    if op == "uninstall":
        return web.json_response(await manager.uninstall(str(value.get("id", ""))))
    if op == "set":  # значения настроек расширения (схема — settings.fields манифеста)
        return web.json_response(await manager.set_values(str(value.get("id", "")), value.get("values") or {}))
    if op == "install_packages":  # requires.python_packages: без confirm — список
        return web.json_response(await manager.install_python_packages(str(value.get("id", "")),
                                                                       confirm=bool(value.get("confirm"))))
    raise ValueError("Неизвестная операция: " + str(op))
