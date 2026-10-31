"""Домен catalog: каталог приложений живёт в эмуляторе (API + UI); роутов
/api/catalog в админке прокси больше нет. observed доступен, когда прокси
подключил store с журналом трафика."""
from . import route


@route("catalog", methods=("GET", "PUT"))
async def catalog(request, runtime, app):
    if request.method == "GET":
        return await runtime.catalog.get(request)
    return await runtime.catalog.settings(request)
