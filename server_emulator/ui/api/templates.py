"""Домен templates: реестр шаблонов экранных директив (screen_templates).

GET — список {templates: [...]} (id, директива, источник, переменные); GET ?id= —
шаблон целиком (тело с подстановками). PUT {id, body, title?, name?, analytics_type?} —
сохранение: встроенный id — переопределение, новый — пользовательский шаблон
(state/screen_templates). POST {op}: reset {id} — сброс к встроенному;
send {id, sn, values} — сборка директивы и Push на живую сессию SN (проверка
шаблона на реальной станции). Контракт — docs/contracts/templates.md.
"""
from aiohttp import web

from ... import actions, screen_templates
from ...schema import load_type
from ...scenario_engine.proto_build import encode
from . import route

ROOT = screen_templates.CARDS_DIR.parent  # каталог пакета server_emulator


@route("templates", methods=("GET", "PUT", "POST"))
async def templates(request, runtime, app):
    reg = screen_templates.registry()
    if request.method == "GET":
        id_ = request.query.get("id")
        if id_:
            entry = reg.read(id_)
            if entry is None:
                raise ValueError(f"Шаблон «{id_}» не найден")
            return web.json_response(entry)
        return web.json_response({"templates": reg.list()})
    value = await request.json()
    if request.method == "PUT":
        return web.json_response(reg.save(value.get("id") or "", value.get("body") or {},
                                          title=value.get("title") or "",
                                          name=value.get("name"),
                                          analytics_type=value.get("analytics_type")))
    op = value.get("op")
    if op == "reset":
        id_ = value.get("id") or ""
        reg.reset(id_)
        return web.json_response({"reset": id_})
    if op == "send":
        id_, sn = value.get("id") or "", str(value.get("sn") or "")
        if not sn:
            raise ValueError("send: нужен sn устройства")
        directive = reg.build(id_, value.get("values") or {})
        server_type = load_type(ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
        payload_raw = encode(ROOT / "schemas", directive["proto"], directive["fields"], "centaur")
        message = actions.push_directive(server_type, '', items=[
            {"name": directive["name"], "payload_raw": payload_raw,
             "is_led_silent": bool(directive["is_led_silent"])}])
        sent = await runtime.send_message(sn, message)
        return web.json_response({"delivery": "sent" if sent else "no_session"})
    raise ValueError("Неизвестная операция: " + str(op))
