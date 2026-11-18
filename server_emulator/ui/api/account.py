"""Домен account: импорт значений из аккаунта Яндекса (конфигуратор).

GET api/params_required — обязательные незаполненные параметры (конфигуратор).
POST api/params_import {source: get_sync_info | iot} — значения в каталог «Параметры»:
get_sync_info — из журнала upstream (без входа), iot — по всем аккаунтам реестра
«Авторизации в сервисах» сразу (account.linked_sessions); ответ — отчёт по полям
без значений секретов.

Сам вход в аккаунт Яндекса живёт в реестре сервисных аккаунтов
(ui/api/service_accounts.py, service_accounts.py): сессии для импорта берутся оттуда.
"""
from aiohttp import web

from ... import account
from . import route


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
