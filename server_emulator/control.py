"""Локальное управление сервером (TASK-045): состояние, штатная остановка и перезапуск.

`/__control/status|stop|restart` принимают запросы только с этого компьютера (127.0.0.1 / ::1) и с
заголовком `X-Control-Token`, равным ключу из `state/control.token` (новый при каждом старте). Этим
пользуются трей-лаунчер, смена порта (TASK-042) и самообновление (TASK-040). Остановка — штатная:
сайты закрываются, `Runtime.close()` через on_cleanup, процесс выходит с кодом 0 или `RESTART_CODE`.
"""
from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from aiohttp import web

from .server_config import RESTART_CODE

TOKEN_FILE = "control.token"
HEADER = "X-Control-Token"
EXIT_KEY = web.AppKey("alica_exit", object)          # функция exit(code) — задаёт server.serve
INFO_KEY = web.AppKey("emulator_info", dict)     # pid, папка данных, слушатели
LOOPBACK = ("127.0.0.1", "::1")


def write_token(state):
    token = secrets.token_urlsafe(32)
    path = Path(state) / TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(TOKEN_FILE + ".tmp")
    temp.write_text(token, encoding="utf-8")
    os.replace(temp, path)
    return token


def read_token(state):
    try:
        return (Path(state) / TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def request_exit(app, code):
    """Штатный выход сервера: 0 — стоп, RESTART_CODE — перезапуск. False — сервер запущен не через serve."""
    exit_ = app.get(EXIT_KEY)
    if exit_ is None:
        return False
    exit_(code)
    return True


def mount(app, token):
    def check(request):
        if request.remote not in LOOPBACK:
            raise web.HTTPForbidden(text="Только с этого компьютера")
        if not hmac.compare_digest(request.headers.get(HEADER, ""), token):
            raise web.HTTPForbidden(text="Нужен ключ управления")

    async def status(request):
        check(request)
        return web.json_response({**app.get(INFO_KEY, {}), "pid": os.getpid()})

    async def stop(request):
        check(request)
        if not request_exit(app, 0):
            raise web.HTTPConflict(text="Сервер запущен без управления")
        return web.json_response({"stopping": True})

    async def restart(request):
        check(request)
        if not request_exit(app, RESTART_CODE):
            raise web.HTTPConflict(text="Сервер запущен без управления")
        return web.json_response({"restarting": True})

    app.router.add_get("/__control/status", status)
    app.router.add_post("/__control/stop", stop)
    app.router.add_post("/__control/restart", restart)
