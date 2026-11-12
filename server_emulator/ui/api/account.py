"""Домен account: аккаунт Яндекса для первичного конфигуратора («Подтянуть из аккаунта»).

GET api/account — статус входа (login, login_name; секреты наружу не выходят).
POST api/account {op}: login_token {token} · login_cookies {cookies} · qr_start → {link} ·
qr_poll → {state: waiting|done, …} · logout. Обращения к Яндексу — account.py
(по образцу YandexStation, MIT); сбои входа — 400 с понятным текстом без секретов.
GET api/params_required — обязательные незаполненные параметры (конфигуратор).
POST api/params_import {source: get_sync_info | iot} — значения в каталог «Параметры»:
get_sync_info — из журнала upstream (без входа), iot — из аккаунта (нужен вход);
ответ — отчёт по полям без значений секретов.
"""
from aiohttp import web

from ... import account
from . import route


def _session(runtime):
    return account.AccountSession(runtime)


@route("account", methods=("GET", "POST"))
async def account_domain(request, runtime, app):
    if request.method == "GET":
        return web.json_response(_session(runtime).status())
    body = await request.json() if request.can_read_body else {}
    op = str(body.get("op") or "")
    session = _session(runtime)
    if op == "login_token":
        token = str(body.get("token") or "").strip()
        if not token:
            raise ValueError("Нужен x_token аккаунта")
        return web.json_response(await session.login_token(token))
    if op == "login_cookies":
        return web.json_response(await session.login_cookies(str(body.get("cookies") or "")))
    if op == "qr_start":
        return web.json_response(await session.qr_start())
    if op == "qr_poll":
        return web.json_response(await session.qr_poll())
    if op == "logout":
        session.forget()
        return web.json_response(session.status())
    raise ValueError("op: login_token | login_cookies | qr_start | qr_poll | logout")


@route("params_required", methods=("GET",))
async def params_required(request, runtime, app):
    return web.json_response({"required": account.required_unfilled(runtime)})


@route("params_import", methods=("POST",))
async def params_import(request, runtime, app):
    body = await request.json()
    source = str(body.get("source") or "")
    session = _session(runtime)
    if source == "get_sync_info":
        return web.json_response(await account.import_sync_info(runtime, session))
    if source == "iot":
        return web.json_response(await account.import_iot(runtime, session))
    raise ValueError("source: get_sync_info | iot")
