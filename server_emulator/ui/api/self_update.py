"""Домен self_update: статус самообновления и действия по клику (TASK-040).

GET — статус (текущая/новая версия, можно ли обновить, итог последнего обновления). POST {action}:
check — проверка сейчас, update / stash_update (с откладыванием правок) / rollback — план и
отделённый процесс updater'а с штатным выходом сервера (UPDATE_CODE), skip — пропустить версию,
snooze — «Позже» до следующей плановой проверки. Guard (X-Emulator + same-origin) — общий.
"""
import copy

from aiohttp import web

from ...control import EXIT_KEY, request_exit
from ...self_update import repo
from ...server_config import UPDATE_CODE
from . import route


@route("self_update", methods=("GET", "POST"))
async def self_update(request, runtime, app):
    service = runtime.self_update
    if request.method == "GET":
        return web.json_response(service.status(app))
    value = await request.json()
    action = value.get("action")
    if action == "check":
        result = await service.check(manual=True)  # ручная проверка работает и без автообновления
        return web.json_response({"check_error": result.get("error"), **service.status(app)})
    if action == "snooze":
        service.snoozed = True
        return web.json_response(service.status(app))
    if action == "skip":
        version = (service.last.get("release") or {}).get("version")
        if not version:
            raise ValueError("нет новой версии, чтобы её пропустить")
        settings = copy.deepcopy(runtime.settings)
        settings["self_update"]["skipped"] = version
        await runtime.save_settings(settings)
        return web.json_response(service.status(app))
    if action == "stash_update":  # «Отложить правки и обновить»: stash по клику, как у ComfyUI
        stash_sha = repo.stash(repo.discover(service.root))
        if not stash_sha:
            raise ValueError("правок в отслеживаемых файлах нет — обычное обновление доступно")
    return web.json_response(_start_update(service, app, action))


def _start_update(service, app, action):
    """План + updater отдельным процессом + штатный выход сервера (UPDATE_CODE)."""
    status = service.status(app)
    if action == "rollback":
        target = status["previous"]
        if not target:
            raise ValueError("предыдущей версии нет — откатывать не на что")
    else:
        target = (status["latest"] or {}).get("version")
        if not target:
            raise ValueError("новой версии нет")
        if not status["can_update"]:
            raise ValueError("; ".join(status["reasons"]) or "обновление недоступно")
    plan = service.plan(target, action)
    service.spawn_updater(plan)
    if not request_exit(app, UPDATE_CODE):
        raise ValueError("Сервер запущен без управления — завершите его и обновите вручную")
    return {"updating": True, "target": target, "launch": plan["launch"]}
