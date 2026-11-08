"""Домен providers («Поставщики данных»): виды поставщиков и выбор активного.

Вид — папка в плагине с обязательными функциями (KINDS в server_emulator/providers.py):
сейчас музыка (music/*.py включённых расширений — встроенных и внешних), позже погода
и новости. GET отдаёт только виды, у которых есть поставщики: по ним строится группа
вкладок. Выбор активного — точечное сохранение ключа settings["providers"][<вид>]
(пусто — первый включённый по алфавиту), как у intelligence: полная копия настроек
откатывала бы остальные ключи. Неизвестный вид или выключенный/неизвестный id — 400.
"""
import copy

from aiohttp import web

from ... import music
from ... import providers
from . import route


def provider_items(kind):
    """Записи вида (по алфавиту id) для API: origin встроенного — builtin."""
    items = []
    for pid, item in sorted((providers.PROVIDERS.get(kind) or {}).items()):
        status = {}
        if music.is_yandex_music(item):
            status["oauth_captured"] = bool(music.captured_oauth())  # сам токен наружу не идёт
        items.append({"id": pid, "title": str(item.get("title") or pid),
                      "extension": str(item.get("ext_id") or ""),
                      "origin": str(item.get("origin") or "extension"),
                      "enabled": True, "status": status})
    return items


@route("providers", methods=("GET", "PUT"))
async def providers_view(request, runtime, app):
    if request.method == "GET":
        kinds = {}
        for kind in providers.kinds():
            kinds[kind] = {"title": providers.kind_title(kind),
                           "selected": str(((runtime.settings or {}).get("providers") or {})
                                           .get(kind) or ""),
                           "providers": provider_items(kind)}
        return web.json_response({"kinds": kinds,
                                  "phrase_order": copy.deepcopy(
                                      runtime.settings.get("phrase_order")
                                      or {"kinds": [], "plugins": {}})})
    body = await request.json()
    if "order" in body:  # ручной порядок справочника фраз: drag-and-drop вкладок видов
        settings = copy.deepcopy(runtime.settings)
        settings["phrase_order"] = body.get("order")  # форму проверит settings_service
        return web.json_response(await runtime.save_settings(settings, warm=False))
    kind = str(body.get("kind") or "")
    pid = str(body.get("id") or "").strip()
    if kind not in providers.kinds():
        raise ValueError("Неизвестный вид поставщиков: " + kind)
    if pid and pid not in (providers.PROVIDERS.get(kind) or {}):
        raise ValueError("Неизвестный или выключенный поставщик: " + pid)
    settings = copy.deepcopy(runtime.settings)
    settings.setdefault("providers", {})[kind] = pid  # "" — авто: первый включённый
    return web.json_response(await runtime.save_settings(settings, warm=False))
