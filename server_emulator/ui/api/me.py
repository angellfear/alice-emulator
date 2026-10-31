"""Домен me: кабинет пользователя /me (TASK-062).

/api/me/params (GET/PUT) и /api/me/devices (PUT) обслуживают страницу me.html; монтируются
mount() на корне (/api/me/*, не под /emulator/api), потому что auth_middleware пускает на них
обычного пользователя. Кто смотрит: пользователь — свой дом (привязка identities_user сессии),
админ и открытый режим — дом из ?user=<id>. me_link — обычный admin-маршрут: токен входа в
кабинет без логина пользователя (auth.AuthStore.create_link); в открытом режиме токен не нужен.
"""
import time
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from ...auth import COOKIE
from . import guarded, route

ROOT = Path(__file__).resolve().parents[2]
SEEN_WINDOW = 600  # «виден» — обмен за последние 10 минут

# Поля определения, которые видит пользователь кабинета
DEF_FIELDS = ("key", "title", "hint", "type", "options", "category")


def _error(status, code):
    return web.json_response({"error": code}, status=status)


def _viewer(request, runtime):
    """(user_id, admin) либо ответ-ошибка: 401 без сессии, 409 вход не привязан к пользователю."""
    users = [u["id"] for u in runtime.identities.data["users"]]
    wanted = request.query.get("user")
    if not runtime.auth.enabled:
        admin, own = True, None
    else:
        session = request.get("auth")
        if session is None:
            return _error(401, "unauthorized")
        admin, own = session["role"] == "admin", session.get("identities_user")
    if admin:
        user_id = wanted if wanted in users else own if own in users else (users[0] if users else None)
    else:
        user_id = own if own in users else None
    if user_id is None:
        return _error(409, "principal_not_linked")
    return user_id, admin


def _own_devices(runtime, user_id):
    return [d for d in runtime.identities.data["devices"] if d.get("user_id") == user_id]


def _status(runtime, device):
    """online — живая WS-сессия, seen — обмен за SEEN_WINDOW, иначе offline; плюс last_seen."""
    aliases = set(device.get("device_ids") or [])
    live = any(s.get("send") and (s.get("device_sn") == device["sn"] or s.get("device_id") in aliases)
               for s in runtime.sessions.values())
    seen = (runtime.identities.activity.get(device["sn"]) or {}).get("ts")
    if live:
        return "online", seen
    return ("seen" if seen and time.time() - seen < SEEN_WINDOW else "offline"), seen


def _with_values(runtime, defs, owner):
    values = runtime.quasar_params.values(owner)
    items = []
    for definition in defs:
        item = {k: definition.get(k) for k in DEF_FIELDS}
        current = (values.get(definition["key"]) or {}).get("value")
        item["value"] = definition.get("default_value") if current is None else current
        items.append(item)
    return items


def _snapshot(runtime, user_id, admin):
    user = next(u for u in runtime.identities.data["users"] if u["id"] == user_id)
    store = runtime.quasar_params
    user_defs = store.defs(editable_only=True, scopes=("user",))
    device_defs = store.defs(editable_only=True, scopes=("device",))
    devices = []
    for device in _own_devices(runtime, user_id):
        status, seen = _status(runtime, device)
        room = device.get("room") or (store.values("device:" + device["sn"]).get("device.room") or {}).get("value") or ""
        devices.append({"sn": device["sn"], "name": device.get("name") or device["sn"],
                        "room": room, "model": device.get("model") or "",
                        "platform": (device.get("profile") or {}).get("platform") or "",
                        "status": status, "last_seen": seen,
                        "params": _with_values(runtime, device_defs, "device:" + device["sn"])})
    result = {"user": user_id, "user_name": user.get("name") or user_id,
              "params": _with_values(runtime, user_defs, "user:" + user_id), "devices": devices}
    if admin:
        result["users"] = [{"id": u["id"], "name": u.get("name") or u["id"]}
                           for u in runtime.identities.data["users"]]
    return result


async def _rename(runtime, device, name=None, room=None):
    """Название/комната колонки: реестр (как правка в «Устройствах») + параметры
    device.name / device.room, затем профили сессий. Под runtime.lock; запись устройства
    перечитывается под ним — привязку могли сменить, пока ждали lock."""
    async with runtime.lock:
        current = next((d for d in runtime.identities.data["devices"]
                        if d["sn"] == device["sn"] and d.get("user_id") == device.get("user_id")), None)
        if current is None:
            raise ValueError("Устройство отвязано от пользователя")
        request = {"operation": "device", "sn": current["sn"], "user_id": current.get("user_id"),
                   "device_ids": current.get("device_ids") or [], "name": current.get("name")}
        if name is not None:
            request["name"] = name
        if room is not None:
            request["room"] = room
        registry = runtime.identities.mutate(request)
        saved = next(d for d in registry["devices"] if d["sn"] == device["sn"])
        owner = "device:" + device["sn"]
        if name is not None and runtime.quasar_params.def_get("device.name"):
            runtime.quasar_params.set_value(owner, "device.name", saved["name"])
        if room is not None and runtime.quasar_params.def_get("device.room"):
            runtime.quasar_params.set_value(owner, "device.room", saved.get("room") or "")
        runtime.refresh_profiles()
    return saved


async def params(request, runtime, app):
    viewer = _viewer(request, runtime)
    if isinstance(viewer, web.Response):
        return viewer
    user_id, admin = viewer
    if request.method == "GET":
        return web.json_response(_snapshot(runtime, user_id, admin))
    value = await request.json()
    if not isinstance(value, dict):
        raise ValueError("expected_object")
    key, owner = value.get("key"), value.get("owner") or "user:" + user_id
    definition = runtime.quasar_params.def_get(key) if isinstance(key, str) else None
    if definition is None:
        raise ValueError("параметр не определён")
    if not definition["user_editable"] or definition["scope"] not in ("user", "device"):
        return _error(403, "not_editable")
    kind, _, target = owner.partition(":") if isinstance(owner, str) else ("", "", "")
    if kind not in ("user", "device") or kind != definition["scope"]:
        return _error(403, "bad_owner")
    if kind == "user" and target != user_id:
        return _error(403, "bad_owner")
    device = next((d for d in _own_devices(runtime, user_id) if d["sn"] == target), None)
    if kind == "device" and device is None:
        return _error(403, "not_your_device")
    if key in ("device.name", "device.room"):  # то же поле, что «Название/Комната» колонки
        text = value.get("value")
        if not isinstance(text, str):
            raise ValueError(key + ": строка")
        saved = await _rename(runtime, device, **{"name" if key == "device.name" else "room": text})
        return web.json_response({"owner": owner, "key": key,
                                  "value": saved["name"] if key == "device.name" else saved.get("room") or ""})
    async with runtime.lock:
        saved = runtime.quasar_params.set_value(owner, key, value.get("value"))
    return web.json_response({"owner": owner, "key": key, "value": saved})


async def devices(request, runtime, app):
    viewer = _viewer(request, runtime)
    if isinstance(viewer, web.Response):
        return viewer
    user_id, _admin = viewer
    value = await request.json()
    if not isinstance(value, dict):
        raise ValueError("expected_object")
    device = next((d for d in _own_devices(runtime, user_id) if d["sn"] == value.get("sn")), None)
    if device is None:
        return _error(403, "not_your_device")
    saved = await _rename(runtime, device, value.get("name"), value.get("room"))
    return web.json_response({"sn": saved["sn"], "name": saved["name"], "room": saved.get("room") or ""})


@route("me_link", methods=("POST",))
async def me_link(request, runtime, app):
    """POST {user} — ссылка в кабинет пользователя для админа панели. Вход включён —
    токен (во фрагменте адреса: не попадает в логи сервера и прокси), иначе ссылка с ?user=."""
    value = await request.json()
    user_id = value.get("user") if isinstance(value, dict) else None
    user = next((u for u in runtime.identities.data["users"] if u["id"] == user_id), None)
    if user is None:
        raise ValueError("Пользователь не найден")
    auth = runtime.auth
    if not auth.enabled:
        return web.json_response({"url": "/me?user=" + quote(user_id, safe="")})
    session = request.get("auth")
    if session is None or session["role"] != "admin":
        return _error(403, "admin_only")
    token = auth.create_link(user_id, user.get("name") or user_id, by=request.cookies.get(COOKIE))
    return web.json_response({"url": "/me#t=" + token})


def mount(app, runtime, prefix="/emulator"):
    """Страница /me и /api/me/* (ответы JSON с маппингом ошибок значений — 400)."""
    async def page(request):
        return web.FileResponse(ROOT / "ui" / "me.html",
                                headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    def handler(fn):
        async def run(request):
            return await fn(request, runtime, app)
        return run

    app.router.add_get("/me", page)
    app.router.add_get(prefix + "/me", page)
    app.router.add_get("/api/me/params", handler(params))
    app.router.add_put("/api/me/params", guarded(handler(params)))
    app.router.add_put("/api/me/devices", guarded(handler(devices)))
