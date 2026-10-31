"""Домен server (TASK-042): «Дополнительно → Сервер» — адрес, порты, SSL, сертификат.

GET api/server → {config (файл), effective (с учётом командной строки), overridden, listeners, errors,
launcher}. PUT api/server — проверка и сохранение в state/server.json (ошибка — 400, файл не меняется;
занятый порт — предупреждение). POST api/server {"action": "restart"} — применить перезапуском.
"""
from aiohttp import web

from ... import control, server_config
from ...version import current

from . import route


def _info(app):
    return app.get(control.INFO_KEY) or {}


def _bound(info):
    return {entry["port"] for entry in info.get("listeners", []) if entry.get("ok")}


@route("server", methods=("GET", "PUT", "POST"))
async def server(request, runtime, app):
    info = _info(app)
    state = runtime.directory
    if request.method == "GET":
        return web.json_response({"config": server_config.load(state), "effective": info.get("config"),
                                  "overridden": info.get("overridden", []), "listeners": info.get("listeners", []),
                                  "errors": info.get("errors", []), "launcher": info.get("launcher", False), "version": current()})
    body = await request.json()
    if request.method == "PUT":
        config = server_config.validate(state, body)
        bound, warnings = _bound(info), []
        for port in (config["port"], config["http_port"]):
            if port and port not in bound and server_config.port_busy(config["host"], port):
                warnings.append(f"Порт {port} сейчас занят другим процессом")
        server_config.save(state, config)
        effective, overridden = server_config.effective(config, {k: info.get("config", {}).get(k)
                                                                 for k in info.get("overridden", [])})
        return web.json_response({"saved": True, "config": config, "warnings": warnings,
                                  "url": server_config.local_url(effective), "overridden": overridden})
    if (body or {}).get("action") != "restart":
        raise ValueError("Неизвестное действие")
    if not control.request_exit(app, server_config.RESTART_CODE):
        raise ValueError("Сервер запущен без управления перезапуском (тестовый экземпляр)")
    return web.json_response({"restarting": True})
