"""Сценарии как инструменты агента: каталог (чтение), probe (dry-run) и запуск.

Сценарии — быстрый путь без LLM: реплику сначала разбирает роутер сценариев, агенту
она приходит, только если сценарий не подошёл. Агент видит каталог, чтобы не дублировать
сценарии и отвечать «что ты умеешь», и запускает их сам (scenario_run), когда команда
пришла в свободной форме или их несколько — эффекты те же, что у сценария (свет, push,
директивы ответа). Класс риска сценария — поле risk (по умолчанию device: без
подтверждения); scenario_run наследует его.
"""
import copy
import datetime
import json
import uuid

from ..scenario_engine.normalize import lexicon_for, normalize_text
from ..scenario_engine.probe import probe
from ..scenario_engine.router import trigger_phrases

DEFAULT_RISK = "device"

SPECS = [
    {"name": "scenario_list",
     "description": "Каталог сценариев — быстрых команд без модели (погода, свет, таймеры, громкость…): "
                    "id, название, примеры фраз, класс риска. query — фильтр по подстроке в id/названии/фразах.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
    {"name": "scenario_get",
     "description": "Полное JSON-определение сценария по id (триггеры, guard, flow).",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "scenario_probe",
     "description": "Dry-run без эффектов: какой сценарий сработает на фразу и почему (или почему ни один).",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "scenario_run",
     "description": "Выполнить сценарий: text — команда пользователя естественной фразой («включи свет на кухне»), "
                    "из неё сценарий берёт параметры. id — конкретный сценарий (иначе выбирает роутер по text). "
                    "Возвращает статус и реплику сценария; её смысл передай пользователю своими словами.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "id": {"type": "string"}},
                     "required": ["text"]}},
]
NAMES = {spec["name"] for spec in SPECS}
RISKS = {"scenario_list": "read", "scenario_get": "read", "scenario_probe": "read"}


def _title(scenario):
    title = scenario.get("title")
    if isinstance(title, dict):
        title = title.get("ru") or title.get("en")
    return title or scenario.get("id")


def _phrases(scenario, limit=3):
    phrases = []
    for trigger in scenario.get("triggers", []):
        if trigger.get("kind") == "phrase":
            phrases += trigger_phrases(trigger)
    return phrases[:limit]


def catalog(runtime):
    return runtime.dialog.scenarios() if getattr(runtime, "dialog", None) else []


def find(runtime, scenario_id):
    return next((s for s in catalog(runtime) if s.get("id") == scenario_id), None)


def route(runtime, text):
    """Сценарий, который выберет роутер для фразы (как DialogManager.handle_text), или None."""
    scenarios = catalog(runtime)
    verdict, reason, scenario = probe(scenarios, runtime.scenario_executor,
                                      text=normalize_text(text, lexicon_for(scenarios)))
    return verdict, reason, scenario


def risk_of(runtime, arguments):
    """Класс риска вызова scenario_run: риск выбранного сценария."""
    arguments = arguments or {}
    scenario = find(runtime, arguments["id"]) if arguments.get("id") else route(runtime, arguments.get("text") or "")[2]
    return (scenario or {}).get("risk") or DEFAULT_RISK


def index(runtime, limit=60):
    """Краткий индекс для системного промпта: одна строка на сценарий с фразами."""
    lines = []
    for scenario in catalog(runtime):
        phrases = _phrases(scenario)
        if phrases:
            lines.append(f"- {scenario['id']}: {_title(scenario)} («" + "», «".join(phrases) + "»)")
    if len(lines) > limit:
        lines = lines[:limit] + [f"- … ещё {len(lines) - limit}, см. scenario_list"]
    return "\n".join(lines)


async def call(runtime, name, arguments, context):
    if name == "scenario_list":
        query = (arguments.get("query") or "").lower()
        items = [{"id": s["id"], "title": _title(s), "phrases": _phrases(s, 5),
                  "risk": s.get("risk") or DEFAULT_RISK, "builtin": s.get("_origin") == "builtin"}
                 for s in catalog(runtime)]
        if query:
            items = [i for i in items if query in json.dumps(i, ensure_ascii=False).lower()]
        return items[:50]
    if name == "scenario_get":
        scenario = find(runtime, arguments.get("id"))
        if scenario is None:
            raise ValueError("Сценарий не найден: " + str(arguments.get("id")))
        return {k: v for k, v in scenario.items() if not str(k).startswith("_")}
    if name == "scenario_probe":
        verdict, reason, scenario = route(runtime, arguments.get("text") or "")
        return {"verdict": verdict, "reason": reason, "scenario": (scenario or {}).get("id")}
    if name == "scenario_run":
        return await run(runtime, arguments, context)
    raise ValueError("Неизвестный инструмент сценариев: " + name)


async def run(runtime, arguments, context):
    text = (arguments.get("text") or "").strip()
    if arguments.get("id"):
        scenario = find(runtime, arguments["id"])
        if scenario is None:
            raise ValueError("Сценарий не найден или выключен: " + arguments["id"])
    else:
        verdict, reason, scenario = route(runtime, text)
        if verdict != "supported":
            return {"status": "not_run", "verdict": verdict, "reason": reason}
    sn = context.get("sn") or ""
    # Диалог голосового запроса: уточнение сценария (ask) продолжит следующая реплика —
    # сценарий становится владельцем диалога. Без него (студия) уточнение снимается.
    dialog_id = context.get("dialog_id") or "agent:" + uuid.uuid4().hex
    ctx = {"input": {"message_id": uuid.uuid4().hex, "request_id": context.get("request_id") or "",
                     "dialog_id": dialog_id, "sn": sn, "text": text},
           "clock": {"now": datetime.datetime.now()}, "sequence": next(runtime.sequence)}
    ctx.update(runtime._scenario_context(sn=sn, device_id=context.get("device_id") or ""))
    if getattr(runtime, "animations", None) is not None:
        ctx["animations"] = {a["id"]: a["url"] for a in runtime.animations.list() if a.get("url")}
    report = await runtime.dialog.run_scenario(copy.deepcopy(scenario), ctx, dialog_id)
    if report.get("status") == "awaiting" and not context.get("dialog_id"):
        runtime.scenario_executor.cancel(dialog_id)
    context.setdefault("scenario_messages", []).extend(report.get("messages") or [])
    result = {"scenario": scenario["id"], "status": report.get("status"), "speech": report.get("speech") or ""}
    if report.get("status") == "awaiting":
        result["note"] = "Сценарий ждёт уточнения: задай пользователю этот вопрос; ответ получит сценарий."
    return result
