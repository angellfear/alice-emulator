"""Домены agent-ops и agent-event: журнал агента и внешние события (agent/ops.py).

agent-ops — вкладка «AI агент»: GET → {available, settings, users, runs, usage, misses};
POST {op}: settings {settings} — включение агента (engine graph | legacy), лимиты, PII,
вебхук, предложения сценариев / suggest — разобрать промахи сейчас.
agent-event — POST {text, sn | user_id, delivery?, title?}: событие извне (вебхук, Home
Assistant) запускает агента в фоне; итог — на колонку, во входящие или молча. Без сессии
администратора нужен заголовок X-Agent-Token = agent.webhook_token. Контракт — docs/contracts/agent.md.
"""
import hmac

from aiohttp import web

from . import route


def _service(runtime):
    return getattr(runtime, "agent", None)


PII = ("credit_card", "secret", "email", "ip", "mac_address", "url")


def _settings(runtime):
    from ...agent import engine, ops
    from ...async_reply import ASYNC_AFTER_S, WAIT_TEXT
    cfg = {"max_model_calls": 6, **ops.options(runtime)}  # agent/service.py DEFAULTS без импорта LangChain
    return {"enabled": engine(runtime) == "graph", "installed": _service(runtime) is not None,
            "max_model_calls": cfg["max_model_calls"], "daily_model_calls": cfg["daily_model_calls"],
            "budgets": cfg["budgets"], "pii": cfg["pii"], "suggest_cron": cfg["suggest_cron"],
            "suggest_min_count": cfg["suggest_min_count"],
            "async_reply_s": float(cfg["async_reply_s"]) if cfg.get("async_reply_s") is not None else ASYNC_AFTER_S,
            "async_wait_text": cfg.get("async_wait_text") or WAIT_TEXT,
            "webhook_token": runtime.masked_secret(cfg["webhook_token"]) if cfg["webhook_token"] else ""}


async def _save(runtime, value):
    """Настройки вкладки «AI агент» → settings.agent; включение агента — engine graph | legacy."""
    current = dict(runtime.settings.get("agent") or {})
    intelligence = None
    for gone in ("stream_reply", "filler_after_s", "think_phrases", "think_every_s", "think_words"):
        current.pop(gone, None)  # озвучка по фразам убрана — её заменил отложенный ответ (Push)
    if "enabled" in value:  # прежний тумблер вкладки — это режим «Интеллект» (agent ↔ model)
        if value["enabled"] and _service(runtime) is None:
            raise ValueError('AI агент не установлен: pip install -e ".[agent]"')
        current["engine"] = "graph"
        intelligence = "agent" if value["enabled"] else "model"
    for key, low, high in (("max_model_calls", 1, 30), ("daily_model_calls", 0, 1_000_000), ("suggest_min_count", 1, 100)):
        if key in value:
            current[key] = max(low, min(high, int(value[key])))
    if "budgets" in value:
        users = {u["id"] for u in runtime.identities.data["users"]}
        current["budgets"] = {str(k): max(0, int(v)) for k, v in (value["budgets"] or {}).items()
                              if k in users and v not in (None, "")}
    if "async_reply_s" in value:
        current["async_reply_s"] = max(0.0, min(15.0, float(value["async_reply_s"] or 0)))
    if "async_wait_text" in value:
        current["async_wait_text"] = " ".join(str(value["async_wait_text"] or "").split())[:200]
    if "pii" in value:
        unknown = [k for k in value["pii"] or [] if k not in PII]
        if unknown:
            raise ValueError("pii: неизвестные типы " + ", ".join(unknown))
        current["pii"] = list(value["pii"] or [])
    if "suggest_cron" in value:
        cron = (value["suggest_cron"] or "").strip()
        if cron:
            from apscheduler.triggers.cron import CronTrigger
            CronTrigger.from_crontab(cron)  # ValueError — неверное выражение
        current["suggest_cron"] = cron
    if "webhook_token" in value and value["webhook_token"] != "__KEEP__":
        current["webhook_token"] = str(value["webhook_token"] or "").strip()
    settings = runtime.public()  # форма UI: секреты — маской, save_settings их сохранит
    settings["agent"] = current  # токен — настоящий, не маска public()
    if intelligence:
        settings["intelligence"] = intelligence
    await runtime.save_settings(settings)
    routines = getattr(runtime, "routines", None)
    if routines is not None and routines.scheduler is not None:
        routines._schedule_suggest()


@route("agent-ops", methods=("GET", "POST"))
async def agent_ops(request, runtime, app):
    service = _service(runtime)
    if request.method == "GET":
        if request.query.get("brief"):  # состояние для бокового меню вкладки
            return web.json_response({"settings": {"enabled": _settings(runtime)["enabled"]}})
        users = [{"id": u["id"], "name": u.get("name") or u["id"]} for u in runtime.identities.data["users"]]
        if service is None:
            return web.json_response({"available": False, "settings": _settings(runtime), "users": users})
        from ...agent import ops
        cfg = ops.options(runtime)
        return web.json_response({"available": True, "settings": _settings(runtime), "users": users,
                                  "runs": service.ops.runs(limit=200), "usage": service.ops.usage(),
                                  "misses": service.ops.misses(int(cfg["suggest_days"]))[:50]})
    value = await request.json()
    if value.get("op") == "settings":
        await _save(runtime, value.get("settings") or {})
        return web.json_response({"settings": _settings(runtime)})
    if service is None:
        raise ValueError('AI агент не установлен: pip install -e ".[agent]"')
    if value.get("op") == "suggest":
        return web.json_response(await service.ops.suggest(runtime))
    raise ValueError("Неизвестная операция: " + str(value.get("op")))


@route("agent-event", methods=("POST",))
async def agent_event(request, runtime, app):
    from ...agent import ops
    admin = (request.get("auth") or {}).get("role") == "admin"
    token = ops.options(runtime)["webhook_token"]
    if not admin and not (token and hmac.compare_digest(request.headers.get("X-Agent-Token", ""), token)):
        raise web.HTTPForbidden(text='{"error":"bad_token"}', content_type="application/json")
    from ...agent import engine
    routines = getattr(runtime, "routines", None)
    if routines is None or engine(runtime) != "graph":
        return web.json_response({"error": "AI агент выключен или не установлен"}, status=503)
    value = await request.json()
    text = (value.get("text") or "").strip()
    if not text:
        raise ValueError("Нужен text — что произошло и что сделать")
    return web.json_response(await routines.event(text, sn=value.get("sn"), user_id=value.get("user_id"),
                                                  delivery=value.get("delivery") or "speak",
                                                  title=value.get("title") or "Событие"))
