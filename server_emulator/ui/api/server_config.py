"""Домен server: «Дополнительно → Сервер» — адрес, порты, SSL, сертификат.

GET api/server → {config (файл), effective (с учётом командной строки), serve_domains
(обслуживаемые домены с включённостью и покрытием текущим сертификатом), overridden, listeners,
errors, launcher}. PUT api/server — проверка и сохранение в state/server.json (ошибка — 400, файл не меняется;
занятый порт — предупреждение). POST api/server {"action": "restart"} — применить перезапуском.
POST api/server-cert (multipart: kind=cert|key и file) — загрузка PEM в state/certs/ → {path, has_key};
настройки не меняются, путь подставляет форма, применяется сохранением. Содержимое файла не логируется и не возвращается.
GET/PUT api/server-front — поведение фронта для необслуживаемых доменов (front_uncovered) и
списки обслуживаемых (serve_off, serve_extra): сохраняется в тот же server.json и применяется
без перезапуска — фронт читает файл на каждое соединение; GET отдаёт ещё observed (домены,
которые фронт видел с запуска), extra (покрытие пользовательских строк) и sans (имена
сертификата). PUT api/server не обнуляет эти ключи: форма их не отправляет — значения берутся из файла.
POST api/server-dns-check — проверка DNS: обслуживаемые домены разрешаются системным резолвером
машины с сервером (getaddrinfo, hosts-файл учитывается), каждая A-запись сверяется с адресами
этого компьютера; ok — все адреса домена локальные. Отражает DNS машины с сервером: колонка
видит то же только при совпадении её DNS (роутер может раздавать разные адреса разным клиентам).
"""
import asyncio
import os
import socket

from aiohttp import web

from ... import control, server_config
from ...front import cert_covers, dns_names_in_cert
from ...upstream.resolver import local_addresses
from ...version import current
from ...voice_stack import speech_dir_from_config

from . import route


def _info(app):
    return app.get(control.INFO_KEY) or {}


def _cert_names(state, config):
    """DNS-имена листового сертификата из настроек; файла нет — пусто."""
    path = server_config.resolve(state, config.get("cert"))
    return dns_names_in_cert(path) if path is not None and path.is_file() else ()


def serve_domains(state, config):
    """[{host, mandatory, enabled, covered}] — обслуживаемые домены (локальный домен первым).
    mandatory — всегда включён; enabled — для дополнительных (serve_off выключает);
    covered — по SAN текущего файла сертификата (нет файла — False)."""
    names = _cert_names(state, config)
    off = config.get("serve_off") or []
    hosts = ([config["local_domain"]] if config.get("local_domain") else []) + list(server_config.MANDATORY_DOMAINS)
    rows = [{"host": host, "mandatory": True, "enabled": True, "covered": cert_covers(host, names)}
            for host in hosts]
    rows += [{"host": host, "mandatory": False, "enabled": host not in off,
              "covered": cert_covers(host, names)} for host in server_config.OPTIONAL_DOMAINS]
    return rows


def _bound(info):
    return {entry["port"] for entry in info.get("listeners", []) if entry.get("ok")}


@route("server", methods=("GET", "PUT", "POST"))
async def server(request, runtime, app):
    info = _info(app)
    state = runtime.directory
    if request.method == "GET":
        config = server_config.load(state)
        return web.json_response({"config": config, "effective": info.get("config"),
                                  "serve_domains": serve_domains(state, config),
                                  "overridden": info.get("overridden", []), "listeners": info.get("listeners", []),
                                  "errors": info.get("errors", []), "launcher": info.get("launcher", False), "version": current()})
    body = await request.json()
    if request.method == "PUT":
        file_cfg = server_config.load(state)
        if isinstance(body, dict):  # форма эти ключи не отправляет — сохранить значения из файла
            for key in ("front_uncovered", "serve_off", "serve_extra"):
                body.setdefault(key, file_cfg[key])
        config = server_config.validate(state, body)
        bound, warnings = _bound(info), []
        for port in (config["port"], config["http_port"]):
            if port and port not in bound and server_config.port_busy(config["host"], port):
                warnings.append(f"Порт {port} сейчас занят другим процессом")
        if file_cfg.get("speech_dir") != config.get("speech_dir") and not os.environ.get("ALICA_SPEECH_DIR"):
            old_dir, new_dir = speech_dir_from_config(file_cfg), speech_dir_from_config(config)
            if old_dir != new_dir and not (new_dir.is_dir() and any(new_dir.iterdir())):
                warnings.append(f"По новому пути ({new_dir}) пакетов голосового стека нет — их нужно "
                                 f"установить заново. Файлы по прежнему пути ({old_dir}) не удалены.")
        server_config.save(state, config)
        effective, overridden = server_config.effective(config, {k: info.get("config", {}).get(k)
                                                                 for k in info.get("overridden", [])})
        return web.json_response({"saved": True, "config": config, "effective": effective, "warnings": warnings,
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


@route("server-front", methods=("GET", "PUT"))
async def server_front(request, runtime, app):
    """Поведение фронта: режим для необслуживаемых доменов (front_uncovered) и списки
    обслуживаемых (serve_off, serve_extra). Перезапуска не нужно — фронт читает server.json
    на каждое соединение; уже открытые соединения не затрагиваются."""
    state = runtime.directory
    if request.method == "GET":
        config = server_config.load(state)
        seen = runtime.front_observed or {}  # без запущенного фронта — пусто (наблюдения с его запуска)
        observed = sorted(({"host": host, "count": int(entry.get("count", 0))}
                           for host, entry in seen.items()),
                          key=lambda entry: (-entry["count"], entry["host"]))
        names = _cert_names(state, config)
        return web.json_response({"front_uncovered": config["front_uncovered"],
                                  "serve_off": config["serve_off"], "serve_extra": config["serve_extra"],
                                  "extra": [{"host": host, "covered": cert_covers(host, names)}
                                            for host in config["serve_extra"]],
                                  "observed": observed, "sans": list(runtime.front_sans or ())})
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("Ожидается объект")
    # частичная правка: что отправлено — то и проверяется, остальное — из файла; полный validate
    # тут не нужен — он проверял бы сертификат (например при ещё не настроенном SSL), который
    # эта форма не трогает
    config = server_config.load(state)
    if "front_uncovered" in body:
        config["front_uncovered"] = server_config.front_mode(body)
    if "serve_off" in body or "serve_extra" in body:
        merged = {**config, **{key: body[key] for key in ("serve_off", "serve_extra") if key in body}}
        config["serve_off"], config["serve_extra"] = server_config.serve_policy(merged)
    server_config.save(state, config)
    return web.json_response({"saved": True, "front_uncovered": config["front_uncovered"],
                              "serve_off": config["serve_off"], "serve_extra": config["serve_extra"]})


def _served_hosts(config):
    """Обслуживаемые домены для проверки DNS: локальный домен, обязательные, включённые
    дополнительные и пользовательский список; повторы убираются, порядок сохраняется."""
    off = config.get("serve_off") or []
    hosts = ([config["local_domain"]] if config.get("local_domain") else []) \
        + list(server_config.MANDATORY_DOMAINS) \
        + [host for host in server_config.OPTIONAL_DOMAINS if host not in off] \
        + list(config.get("serve_extra") or [])
    return list(dict.fromkeys(hosts))


DNS_CHECK_TIMEOUT_S = 15  # на домен: системный резолвер при недоступном DNS может думать долго


@route("server-dns-check", methods=("POST",))
async def server_dns_check(request, runtime, app):
    """Проверка DNS: обслуживаемые домены разрешаются системным резолвером машины с сервером,
    каждая A-запись сверяется с адресами этого компьютера (local_addresses — те же, что
    отсекают петлю туннелей). ok — домен разрешается и все его адреса локальные; частично
    локальный список (round-robin с чужим адресом) — не ok: часть запросов колонки ушла бы мимо."""
    config = server_config.load(runtime.directory)
    hosts = _served_hosts(config)
    local = local_addresses()
    loop = asyncio.get_running_loop()

    def resolve(host):
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            return []
        return sorted({info[4][0] for info in infos})

    async def check(host):
        try:
            return host, await asyncio.wait_for(loop.run_in_executor(None, resolve, host),
                                                DNS_CHECK_TIMEOUT_S)
        except (asyncio.TimeoutError, OSError):
            return host, []

    resolved = await asyncio.gather(*(check(host) for host in hosts))
    items = [{"host": host, "ips": ips, "ok": bool(ips) and all(ip in local for ip in ips)}
             for host, ips in resolved]
    return web.json_response({"items": items, "local": sorted(local),
                              "ok": all(item["ok"] for item in items)})
