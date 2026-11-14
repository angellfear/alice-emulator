"""Домен account: аккаунты Яндекса — один вход на пользователя.

GET api/account?user=<id> — статус входа пользователя (login, login_name; секреты
наружу не выходят). POST api/account {user, op}: login_token {token} ·
login_cookies {cookies} · qr_start → {link} · qr_poll → {state: waiting|done, …} ·
logout. user обязателен и должен быть в реестре — иначе 400 с понятным текстом.
Обращения к Яндексу — account.py (по образцу YandexStation, MIT); сбои входа —
400 с понятным текстом без секретов.
GET api/params_required — обязательные незаполненные параметры (конфигуратор).
POST api/params_import {source: get_sync_info | iot} — значения в каталог «Параметры»:
get_sync_info — из журнала upstream (без входа), iot — по всем входам пользователей
сразу (account.linked_sessions); ответ — отчёт по полям без значений секретов.
"""
from aiohttp import web

from ... import account
from . import route


def _session(runtime, user_id):
    """Сессия аккаунта пользователя: user обязателен и должен быть в реестре."""
    if not user_id:
        raise ValueError("Укажите пользователя: user=<id>")
    if not any(user["id"] == user_id for user in runtime.identities.data["users"]):
        raise ValueError(f"Нет пользователя «{user_id}» в реестре")
    return account.AccountSession(runtime, user_id)


@route("account", methods=("GET", "POST"))
async def account_domain(request, runtime, app):
    if request.method == "GET":
        session = _session(runtime, request.query.get("user") or "")
        return web.json_response(session.status())
    body = await request.json() if request.can_read_body else {}
    op = str(body.get("op") or "")
    session = _session(runtime, str(body.get("user") or ""))
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
    if source == "get_sync_info":
        return web.json_response(await account.import_sync_info(runtime))
    if source == "iot":
        return web.json_response(await account.import_iot(runtime))
    raise ValueError("source: get_sync_info | iot")
