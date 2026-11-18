"""Домен service-accounts — «Настройки → Авторизация в сервисах» панели.

GET  service-accounts — реестр аккаунтов и копилка токенов колонок:
{accounts: [{id, service, title, default, created, login}],
 columns: [{sn, platform, ip, ts}]} — колонки копилки без токенов, платформа из
реестра устройств, ip и ts — из деталей захвата (адрес колонки на момент запроса).

POST service-accounts {op}: qr_start → {link, qr} (qr — data-URI PNG, QR-код ссылки
прямо в панели: сканирует приложение Яндекса на телефоне, браузер не открывается)
· qr_poll → {state: waiting|done, …} · default {id} · remove {id} · forget {sn}
(удалить захваченный токен колонки — колонка принесёт его снова при следующем
обращении к Яндексу). Вход в Яндекс — только QR; вход Spotify — через точки login
поставщика (providers_login), токены оттуда попадают в этот же реестр.
"""
from aiohttp import web

from ... import music
from . import route


def _column_rows(runtime):
    """Строки копилки токенов колонок для панели: sn, платформа (реестр устройств),
    ip и время захвата (детали из копилки). Токены наружу не идут."""
    devices = {}
    for device in runtime.identities.data["devices"]:
        if device.get("sn") and device["sn"] not in devices:
            devices[device["sn"]] = str((device.get("profile") or {}).get("platform") or "")
    return [{**item, "platform": devices.get(item["sn"], "")}
            for item in music.captured_column_details()]


@route("service-accounts", methods=("GET", "POST"))
async def service_accounts(request, runtime, app):
    registry = runtime.service_accounts
    if request.method == "GET":
        return web.json_response({"accounts": registry.list(),
                                  "columns": _column_rows(runtime)})
    body = await request.json()
    op = str(body.get("op") or "")
    if op == "qr_start":
        return web.json_response(await registry.yandex_qr_start())
    if op == "qr_poll":
        return web.json_response(await registry.yandex_qr_poll())
    if op == "default":
        registry.set_default(str(body.get("id") or ""))
        return web.json_response({"ok": True})
    if op == "remove":
        registry.remove(str(body.get("id") or ""))
        return web.json_response({"ok": True})
    if op == "forget":
        music.forget_token(runtime, str(body.get("sn") or ""))
        return web.json_response({"ok": True})
    raise ValueError("op: qr_start | qr_poll | default | remove | forget")
