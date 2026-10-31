"""Режим «Интеллект» — что отвечает на фразу, которую не взял сценарий.

Ось независима от mode (proxy/emulate):
* scenarios — только сценарии; без совпадения — стандартный ответ not_understood, модель не вызывается;
* model     — один вызов LLM (промпт + история), без MCP-инструментов, памяти и заданий;
* agent     — агент: LangGraph (extra «agent»), без него — legacy-цикл с инструментами;
* ext:<id>  — режим расширения (modes/*.py): MODE + async answer(ctx, text, history), TASK-034.
  Расширение выключено/удалено/без режимов — откат на scenarios с записью в recent.

Решение принимается в одной точке — ProviderManager.chat и agent.engine.

Режимы расширений (TASK-034, контракт — docs/contracts/extensions.md): один файл modes/*.py —
один режим; модуль объявляет MODE = {id, title, needs?, tabs?, preset?} и async answer(ctx, text, history),
возвращающую ответ в форме ProviderManager.chat ({text, tool_calls, usage, source…}). Загрузка —
общий ScriptRunner (mtime-кеш: правка файла подхватывается без рестарта), ctx — SDK ScenarioContext
(TASK-018), собранный так же, как для сценария (agent/scenarios.py); настройки расширения режим
читает из ctx.extension (TASK-053, extensions/context.py). Полный id режима —
"<ext-id>.<MODE.id>" (префикс имён элементов расширения), settings.intelligence = "ext:<полный id>".
"""
import datetime
import logging

LEVELS = ("scenarios", "model", "agent")
EXT_PREFIX = "ext:"
NOT_UNDERSTOOD = "Я пока этого не умею."
MODE_TIMEOUT = 30.0  # ответ режима может включать сетевые вызовы расширения — дольше, чем у хуков

log = logging.getLogger(__name__)


def ext_id(value):
    """settings.intelligence = "ext:<id>" → id режима расширения, иначе None."""
    return value[len(EXT_PREFIX):] if isinstance(value, str) and value.startswith(EXT_PREFIX) and len(value) > len(EXT_PREFIX) else None


def level(runtime):
    value = (getattr(runtime, "settings", None) or {}).get("intelligence")
    return value if value in LEVELS else "agent"


def at_least(runtime, minimum):
    return LEVELS.index(level(runtime)) >= LEVELS.index(minimum)


def migrate(settings):
    """Настройки до появления режима: включённый агент (engine graph) — agent, иначе model.
    Значение "ext:<id>" (режим расширения) — не мигрируем, живёт до отката в chat."""
    if ext_id(settings.get("intelligence")) is not None:
        return False
    if settings.get("intelligence") not in LEVELS:
        settings["intelligence"] = "agent" if (settings.get("agent") or {}).get("engine", "graph") == "graph" else "model"
        return True
    return False


def not_understood(runtime):
    """Ответ режима scenarios на фразу без сценария (стандартный ответ или запасная фраза)."""
    from .behavior import standard_for
    reply = standard_for(runtime.settings.get("standard_replies"), "not_understood")
    return {"text": reply["text"] if reply else NOT_UNDERSTOOD, "tool_calls": [], "usage": {}, "source": "scenarios"}


# --- режимы расширений (modes/*.py, TASK-034) --------------------------------------

_runner = None


def mode_runner():
    """Общий ScriptRunner режимов (mtime-кеш — правка modes/*.py без рестарта)."""
    global _runner
    if _runner is None:
        from .scripting import ScriptRunner
        _runner = ScriptRunner(prefix="ext_mode", timeout=MODE_TIMEOUT)
    return _runner


def _tabs(raw):
    """MODE.tabs → [{"id", "title"}]: элементы [id, title] или {id, title}."""
    result = []
    for item in raw or []:
        if isinstance(item, dict) and item.get("id"):
            result.append({"id": str(item["id"]), "title": str(item.get("title") or item["id"])})
        elif isinstance(item, (list, tuple)) and len(item) >= 2 and str(item[0]).strip():
            result.append({"id": str(item[0]), "title": str(item[1])})
    return result


def extension_modes(runtime):
    """Режимы включённых расширений (modes/*.py): [{ext, id, title, tabs, preset, needs, file}].
    Файл без MODE или битый — режим не регистрируется (расширение не роняет перечисление)."""
    extensions = getattr(runtime, "extensions", None)
    if extensions is None or not hasattr(extensions, "contribution_dirs"):
        return []
    result = []
    for ext, path in extensions.contribution_dirs("modes"):
        for file in sorted(path.glob("*.py")):
            try:
                spec = getattr(mode_runner()._module(file), "MODE", None)
            except Exception as exc:  # noqa: BLE001 — битый режим не роняет сервер
                log.warning("Режим расширения %s не загружен: %s", file, exc)
                continue
            if isinstance(spec, dict) and str(spec.get("id") or "").strip():
                result.append({"ext": ext, "id": f"{ext}.{str(spec['id']).strip()}",
                               "title": str(spec.get("title") or spec["id"]),
                               "tabs": _tabs(spec.get("tabs")),
                               "preset": spec.get("preset") if isinstance(spec.get("preset"), dict) else None,
                               "needs": spec.get("needs") if isinstance(spec.get("needs"), list) else [],
                               "file": file})
    return result


def extension_mode(runtime, mode_id):
    """Загруженный режим по полному id ("<ext-id>.<MODE.id>") или None — расширение
    выключено/удалено/не содержит такого режима (откат в chat)."""
    return next((m for m in extension_modes(runtime) if m["id"] == mode_id), None)


def note(runtime, error):
    """Запись о проблеме режима в recent (видна в статусе сервера), как у runtime.recent."""
    recent = getattr(runtime, "recent", None)
    if isinstance(recent, list):
        runtime.recent = (recent + [{"kind": "intelligence", "error": str(error)[:200]}])[-30:]


def ext_fallback(runtime, mode_id, reason):
    """Режим ext:<id> недоступен — отвечаем как «Простой», настройка не меняется
    (расширение могут включить обратно), факт — в recent (TASK-034)."""
    note(runtime, f"Режим ext:{mode_id} недоступен ({reason}) — отвечаю как «Простой».")
    return not_understood(runtime)


def mode_context(runtime, text, context):
    """Контекст запроса для answer: тот же формат, что у сценариев (input/clock +
    профиль владельца и устройства), — ScenarioContext читает его без дублей."""
    sn = str(context.get("sn") or "")
    ctx = {"input": {"text": text, "sn": sn,
                     "request_id": str(context.get("request_id") or ""),
                     "dialog_id": str(context.get("dialog_id") or "")},
           "clock": {"now": datetime.datetime.now()}}
    build = getattr(runtime, "_scenario_context", None)
    if build is not None:
        ctx.update(build(sn=sn, device_id=str(context.get("device_id") or "")))
    return ctx


async def answer(runtime, mode, text, history, context):
    """Ответ режима расширения: ScenarioContext (TASK-018) + answer(ctx, text, history)
    через ScriptRunner. Сбой режима — ответ «Простого» и запись в recent, диалог не роняется.
    Режим видит настройки своего расширения как ctx.extension (TASK-053): новый
    ExtensionContext на каждый вызов — правка настроек действует без рестарта."""
    from .sdk import ScenarioContext
    scenario = ScenarioContext(runtime, mode_context(runtime, text, context))
    build = getattr(getattr(runtime, "extensions", None), "context", None)
    scenario.extension = build(mode["ext"]) if callable(build) else None
    try:
        result = await mode_runner().run(mode["file"], scenario, text, list(history or []), function="answer")
    except Exception as exc:  # noqa: BLE001 — сбой режима не должен ронять диалог
        log.warning("Режим расширения %s упал: %s", mode["id"], exc)
        note(runtime, f"Режим ext:{mode['id']}: {type(exc).__name__}: {exc}")
        return not_understood(runtime)
    if not isinstance(result, dict):
        result = {"text": "" if result is None else str(result)}
    result.pop("source", None)  # источник — режим, подделать «scenarios» нельзя (уровень TTS-кеша)
    result.setdefault("text", "")
    result["text"] = str(result["text"] or "")
    result.setdefault("tool_calls", [])
    if not isinstance(result["tool_calls"], list):
        result["tool_calls"] = []
    result.setdefault("usage", {})
    result["source"] = mode["id"]
    return result
