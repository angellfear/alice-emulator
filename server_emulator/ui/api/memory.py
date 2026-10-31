"""Домен memory: вкладка «Память» — долговременная память агента (agent/memory.py).

GET ?user=<id> → {available, settings (ключ замаскирован), users, user, items {facts,
episodes, instructions}, log (фоновые рефлексии)}.
POST {op}: settings {enabled, recall_limit, reflect_after_s, embed {url, model, api_key}}
(api_key '__KEEP__' — не менять) / put {user, kind, text, key?} / delete {user, kind, key} /
forget {user} — вся память пользователя / reflect {user} — рефлексия тредов его колонок
сейчас / test_embed — размерность вектора текущей модели эмбеддингов.
Контракт — docs/contracts/memory.md.
"""
from aiohttp import web

from . import route


def _memory():
    try:
        from ...agent import memory
    except ImportError:  # extra «agent» не установлен
        return None
    return memory


def _user(runtime, user_id):
    if not any(u["id"] == user_id for u in runtime.identities.data["users"]):
        raise ValueError("Нет такого пользователя реестра: " + str(user_id))
    return user_id


def _threads(runtime, user_id):
    return ["voice:" + d["sn"] for d in runtime.identities.data["devices"] if d.get("user_id") == user_id]


@route("memory", methods=("GET", "POST"))
async def memory_api(request, runtime, app):
    memory = _memory()
    if memory is None or runtime.agent is None:
        return web.json_response({"available": False})
    if request.method == "GET":
        users = [{"id": u["id"], "name": u.get("name") or u["id"]} for u in runtime.identities.data["users"]]
        user_id = request.query.get("user") or (users[0]["id"] if users else None)
        items = {}
        if user_id:
            _user(runtime, user_id)
            async with memory.open_store(runtime, runtime.agent.path) as store:
                for kind in memory.KINDS:
                    items[kind] = await memory.items(store, user_id, kind, limit=200)
        settings = memory.options(runtime)
        settings["embed"]["api_key"] = runtime.masked_secret(settings["embed"].get("api_key"))
        return web.json_response({"available": True, "settings": settings, "users": users, "user": user_id,
                                  "items": items, "log": runtime.agent.reflector.log[::-1]})
    value = await request.json()
    op = value.get("op")
    if op == "settings":
        current = memory.options(runtime)
        embed = {**current["embed"], **(value.get("embed") or {})}
        if embed.get("api_key") == "__KEEP__":
            embed["api_key"] = current["embed"].get("api_key", "")
        if embed.get("url"):
            runtime.validate_url(embed["url"])
        stored = {"enabled": bool(value.get("enabled", current["enabled"])),
                  "recall_limit": max(1, min(20, int(value.get("recall_limit", current["recall_limit"])))),
                  "reflect_after_s": max(10, min(3600, int(value.get("reflect_after_s", current["reflect_after_s"])))),
                  "embed": {k: str(embed.get(k) or "") for k in ("url", "model", "api_key")}}
        settings = runtime.public()  # форма UI: секреты провайдеров — маской, save_settings их сохранит
        settings["memory"] = stored
        await runtime.save_settings(settings, preserve_mode=True)
        return web.json_response({"ok": True})
    if op == "test_embed":
        cfg = memory.options(runtime)
        if not cfg["embed"].get("url"):
            raise ValueError("Не задан URL эмбеддингов")
        vector = await memory.RuntimeEmbeddings(runtime, cfg["embed"]).aembed_query("проверка")
        return web.json_response({"dims": len(vector)})
    user_id = _user(runtime, value.get("user"))
    if op == "reflect":
        records = [await runtime.agent.reflector.reflect(runtime, thread, user_id) for thread in _threads(runtime, user_id)]
        return web.json_response({"records": records})
    async with memory.open_store(runtime, runtime.agent.path) as store:
        if op == "put":
            text = (value.get("text") or "").strip()
            if not text and value.get("kind") != "instructions":
                raise ValueError("Пустая запись")
            key = await memory.put(store, user_id, value.get("kind"), text, value.get("key"))
            return web.json_response({"key": key})
        if op == "delete":
            if value.get("kind") not in memory.KINDS:
                raise ValueError("kind ∈ " + ", ".join(memory.KINDS))
            await store.adelete(memory.namespace(user_id, value["kind"]), value["key"])
            return web.json_response({"ok": True})
        if op == "forget":
            removed = 0
            for kind in memory.KINDS:
                while True:
                    found = await store.asearch(memory.namespace(user_id, kind), limit=100)
                    for item in found:
                        await store.adelete(item.namespace, item.key)
                    removed += len(found)
                    if len(found) < 100:
                        break
            return web.json_response({"removed": removed})
    raise ValueError("Неизвестная операция: " + str(op))

