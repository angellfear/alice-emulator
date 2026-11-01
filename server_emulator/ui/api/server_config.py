"""Домен server (TASK-042): «Дополнительно → Сервер» — адрес, порты, SSL, сертификат.

GET api/server → {config (файл), effective (с учётом командной строки), overridden, listeners, errors,
launcher}. PUT api/server — проверка и сохранение в state/server.json (ошибка — 400, файл не меняется;
занятый порт — предупреждение). POST api/server {"action": "restart"} — применить перезапуском.
POST api/server-cert (multipart: kind=cert|key и file) — загрузка PEM в state/certs/ → {path, has_key};
настройки не меняются, путь подставляет форма, применяется сохранением. Содержимое файла не логируется и не возвращается.
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


@route("server-cert", methods=("POST",))
async def server_cert(request, runtime, app):
    if not request.content_type.startswith("multipart/"):
        raise ValueError("Нужен файл сертификата или ключа (multipart)")
    kind, data = None, None
    async for part in await request.multipart():
        if part.name == "kind":
            kind = (await part.text()).strip()
        elif part.filename is not None and data is None:
            data = bytearray()
            while chunk := await part.read_chunk(64 * 1024):
                data += chunk
                if len(data) > server_config.CERT_LIMIT:  # дальше не читаем: не PEM-файл
                    break
            filename = part.filename
    if data is None:
        raise ValueError("Файл не передан")
    return web.json_response(server_config.store_cert(runtime.directory, kind, filename, bytes(data)))
