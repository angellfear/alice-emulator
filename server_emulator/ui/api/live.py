"""Домен live (TASK-088): одна строка состояния в шапку панели — готовность ASR/TTS/Модели
и микросостояние текущего хода агента. Лёгкий и частый (опрос 1–2 с, пока вкладка видима):
без разбора пресетных полей и списков адаптеров — только активный источник, здоровье
локального пакета и журналы runtime (agent_activity / voice_errors).

status: ok — зелёный (локальный пакет отвечает на /health; облачный настроен и последняя
операция без ошибки) · warn — жёлтый (установка/запуск) · err — красный (ошибка, hint —
текст и время) · off — серый (источник не выбран).
"""
import time

from aiohttp import web

from . import route

# статус локального пакета (voice_stack.LocalSpeech.state) → значок и пояснение шапки
PACKAGE_STATE = {
    "running": ("ok", "сервер отвечает"),
    "starting": ("warn", "сервер запускается"),
    "installing": ("warn", "установка пакета"),
    "stopped": ("err", "сервер остановлен"),
    "absent": ("err", "пакет не установлен"),
    "error": ("err", "ошибка установки"),
    "unsupported": ("err", "пакет не поддерживается на этой ОС"),
}


def _when(ts):
    return time.strftime("%H:%M", time.localtime(ts))


async def _package_service(stack, package):
    state = await stack.local.state(package)
    status, hint = PACKAGE_STATE.get(state.get("status"), ("err", str(state.get("status") or "нет состояния")))
    if state.get("error"):
        hint = state["error"]
    elif state.get("stage") and status == "warn":
        hint = f"{hint} — {state['stage']}"
    if status == "err":
        hint += " · " + _when(time.time())
    return {"status": status, "hint": hint, "source": package.get("title", ""), "kind": "package"}


def _cloud_service(runtime, settings_key, service, title):
    cfg = runtime.settings.get(settings_key) or {}
    configured = any(cfg.get(k) for k in ("url", "model", "adapter", "stub_text"))
    error = runtime.voice_errors.get(service)
    if error:
        return {"status": "err", "hint": f"{error.get('error') or 'сбой'} · {_when(error.get('ts', 0))}",
                "source": title, "kind": "preset"}
    if configured:
        return {"status": "ok", "hint": "настроен, последняя операция без ошибок", "source": title, "kind": "preset"}
    return {"status": "off", "hint": "источник выбран, но не настроен (пустой адрес)",
            "source": title, "kind": "preset"}


@route("live", methods=("GET",))
async def live(request, runtime, app):
    from ...voice_stack import SETTINGS_KEY, SERVICES
    stack = runtime.voice_stack
    services = {}
    for service in SERVICES:
        ref = stack.active(service)
        kind, _, identifier = ref.partition(":")
        if kind == "k":
            try:
                package = stack.package(identifier)
            except ValueError:  # ссылка на удалённый пакет — «источника нет» (unavailable_source)
                package = None
            services[service] = (await _package_service(stack, package) if package else
                                 {"status": "err", "hint": f"применённый источник «{ref}» недоступен", "source": "", "kind": "package"})
        elif kind == "p":
            try:
                title = stack.preset(service, identifier).get("title", ref)
            except ValueError:
                title = ref
            services[service] = _cloud_service(runtime, SETTINGS_KEY[service], service, title)
        else:
            cfg = runtime.settings.get(SETTINGS_KEY[service]) or {}
            # адреса в настройках есть, а источника стек не выбирает — работает и так (до вкладки «Голос»)
            configured = any(cfg.get(k) for k in ("url", "model", "adapter", "stub_text"))
            error = runtime.voice_errors.get(service)
            services[service] = (
                {"status": "err", "hint": f"{error.get('error') or 'сбой'} · {_when(error.get('ts', 0))}", "source": "", "kind": ""}
                if error else
                {"status": "ok" if configured else "off",
                 "hint": "адрес из настроек (источник в голосовом стеке не выбран)" if configured else "источник не выбран",
                 "source": "", "kind": ""})

    activity = runtime.agent_activity
    state = activity.get("state") or "idle"
    if state != "idle" and time.time() - float(activity.get("ts") or 0) > 120:
        state = "idle"  # ход завис без терминального события (обрыв колонки, сбой процесса)
    return web.json_response({
        "intelligence": runtime.settings.get("intelligence") or "agent",
        "services": services,
        "agent": {"state": state,
                  "tool": activity.get("tool") if state == "tool" else None,
                  "error": activity.get("error") if state == "error" else None},
    })
