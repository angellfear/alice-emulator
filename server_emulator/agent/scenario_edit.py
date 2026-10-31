"""Правка сценариев агентом: черновик → JSON Patch → проверка → прогон фраз → активация.

Черновики — state/scenario_drafts/<draft_id>.json, в роутинг не попадают. История версий —
state/scenario_history/<id>/<n>.json: перед каждой активацией и откатом прежнее определение
сохраняется, revert возвращает любую версию.

Правила:
* встроенный сценарий на месте не правится: черновик — копия user.<id>, при активации
  встроенный выключается (builtin_disabled) — тот же конфликт-флоу, что в UI;
* валидатор (scenario_packages/validate.py) запрещает код; его ошибки возвращаются модели;
* TASK-027: агент пишет только простые JSON (compose / action / ask / finish / error); сложное
  (данные, разбор фразы, ветвление, состояние) — py администратора. Удалённые шаги старого
  формата валидатор отвергает с ошибкой «перенесите в py»;
* scenario_test проверяет фразы черновика и что фразы ДРУГИХ сценариев не перехвачены
  и не стали неоднозначными (неоднозначная реплика уходит в LLM мимо сценария);
* активация — класс риска config (подтверждение). Каталог общий: агент в диалоге с
  пользователем активирует только сценарий, ограниченный guard'ом этим пользователем
  ({"eq": ["{{user.id}}", <user>]}); прочее — предложение (status proposed), его
  утверждает администратор во вкладке «Сценарии». Без пользователя (студия) — каталог целиком.
"""
import copy
import json
import os
import re
import time
import uuid
from pathlib import Path

import jsonpatch

from ..scenario_engine.normalize import lexicon_for, normalize_text
from ..scenario_engine.probe import probe
from ..scenario_engine.router import trigger_phrases
from ..scenario_packages.validate import PackageError, validate_scenario

NAMES = {"scenario_draft", "scenario_patch", "scenario_test", "scenario_activate", "scenario_history", "scenario_revert"}
RISKS = {"scenario_draft": "device", "scenario_patch": "device", "scenario_test": "read",
         "scenario_history": "read", "scenario_activate": "config", "scenario_revert": "config"}
TEMPLATE = {"format_version": 2, "version": "1.0.0", "category": "custom", "title": {"ru": ""},
            "triggers": [{"kind": "phrase", "phrases": []}],
            "flow": [{"id": "reply", "op": "compose", "reply": {"text": ""}}, {"id": "done", "op": "finish"}]}


def _drafts(runtime):
    path = runtime.directory / "scenario_drafts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _history(runtime, scenario_id):
    path = runtime.directory / "scenario_history" / scenario_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _clean(scenario):
    return {k: v for k, v in scenario.items() if not str(k).startswith("_")}


def user_guard(user_id):
    return {"eq": ["{{user.id}}", user_id]}


def personal(scenario, user_id):
    """Сценарий ограничен пользователем: guard — его eq или all с ним."""
    guard, mine = scenario.get("guard"), user_guard(user_id)
    return guard == mine or (isinstance(guard, dict) and mine in (guard.get("all") or []))


def drafts(runtime):
    result = []
    for path in sorted(_drafts(runtime).glob("*.json")):
        try:
            result.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(result, key=lambda d: d.get("updated", 0), reverse=True)


def get_draft(runtime, draft_id):
    if not re.fullmatch(r"d[0-9a-f]{10}", str(draft_id)):
        raise ValueError("Нет черновика: " + str(draft_id))
    path = _drafts(runtime) / (draft_id + ".json")
    if not path.exists():
        raise ValueError("Нет черновика: " + draft_id)
    return json.loads(path.read_text(encoding="utf-8"))


def _write(runtime, draft):
    draft["updated"] = time.time()
    try:
        validate_scenario(copy.deepcopy(draft["scenario"]))
        draft["error"] = None
    except PackageError as exc:
        draft["error"] = str(exc)
    (_drafts(runtime) / (draft["draft_id"] + ".json")).write_text(
        json.dumps(draft, ensure_ascii=False, indent=1), encoding="utf-8")
    return draft


def _current(runtime, scenario_id):
    """Действующее определение: пользовательское (settings) или встроенное (библиотека)."""
    from ..scenario_engine.library import SCENARIOS
    own = next((s for s in runtime.settings.get("scenarios", []) if s.get("id") == scenario_id), None)
    if own is not None:
        return copy.deepcopy(own), False
    if scenario_id in SCENARIOS:
        return copy.deepcopy(SCENARIOS[scenario_id]), True
    return None, False


def create(runtime, from_id=None, scenario=None, user_id=None, by="agent", make_personal=False):
    if from_id:
        base, builtin = _current(runtime, from_id)
        if base is None:
            raise ValueError("Нет сценария: " + from_id)
        body = _clean(base)
        if builtin:  # встроенный не правится на месте: копия user.<id>, встроенный выключится при активации
            body["id"] = ("user." + from_id)[:64]
            body["category"] = "custom"
    else:
        body = {**copy.deepcopy(TEMPLATE), **(scenario or {})}
        body.setdefault("id", ("agent." if by == "agent" else "user.") + uuid.uuid4().hex[:8])
    if make_personal and user_id and not personal(body, user_id):
        body["guard"] = {"all": [user_guard(user_id), body["guard"]]} if body.get("guard") else user_guard(user_id)
    draft = {"draft_id": "d" + uuid.uuid4().hex[:10], "base_id": from_id,
             "disable_builtin": from_id if from_id and _current(runtime, from_id)[1] else None,
             "scenario": body, "created_by": by, "user_id": user_id, "status": "draft", "created": time.time()}
    if from_id and _is_script(body):  # py-сценарий: код источника едет в черновике, копия без него — script_missing
        source = runtime.scenario_executor.script_path(base)
        if source is not None and source.is_file():
            draft["script"] = source.read_text(encoding="utf-8")
    return _write(runtime, draft)


def _is_script(scenario):
    return (scenario.get("executor") or {}).get("kind") == "script"


def _write_script(runtime, scenario_id, code):
    """Код py-сценария рядом с пользовательским JSON — как PUT /scenarios: синтаксис до записи, атомарно."""
    try:
        compile(code, scenario_id + ".py", "exec")
    except SyntaxError as exc:
        raise ValueError(f"Скрипт: строка {exc.lineno}: {exc.msg}") from None
    target = runtime.scenario_dir / (scenario_id + ".py")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".py.tmp")
    temporary.write_text(code, encoding="utf-8")
    os.replace(temporary, target)


def patch(runtime, draft_id, operations):
    draft = get_draft(runtime, draft_id)
    if draft["status"] not in ("draft", "proposed"):
        raise ValueError("Черновик уже " + draft["status"])
    try:
        draft["scenario"] = jsonpatch.apply_patch(draft["scenario"], operations)
    except (jsonpatch.JsonPatchException, jsonpatch.JsonPointerException, TypeError) as exc:
        raise ValueError("JSON Patch: " + str(exc))
    draft["status"] = "draft"
    return _write(runtime, draft)


def _catalog_with(runtime, draft):
    """Каталог роутера, как если бы черновик был активирован."""
    scenario = {**copy.deepcopy(draft["scenario"]), "enabled": True}
    replaced = {scenario["id"], draft.get("disable_builtin")}
    from ..scenario_engine.library import catalog_order
    return sorted([s for s in runtime.dialog.scenarios() if s.get("id") not in replaced] + [scenario], key=catalog_order)


def _route(runtime, catalog, text):
    verdict, reason, scenario = probe(catalog, runtime.scenario_executor,
                                      text=normalize_text(text, lexicon_for(catalog)))
    return verdict, (scenario or {}).get("id")


def test(runtime, draft_id, phrases=()):
    draft = get_draft(runtime, draft_id)
    if draft.get("error"):
        return {"valid": False, "error": draft["error"]}
    scenario_id = draft["scenario"]["id"]
    before, after = runtime.dialog.scenarios(), _catalog_with(runtime, draft)
    own = list(phrases) or [p for t in draft["scenario"].get("triggers", []) if t.get("kind") == "phrase"
                            for p in trigger_phrases(t)]
    results = [{"phrase": p, **dict(zip(("verdict", "scenario"), _route(runtime, after, p)))} for p in own]
    stolen = []
    for other in before:  # фразы остальных сценариев: черновик не должен их перехватывать
        if other.get("id") in (scenario_id, draft.get("disable_builtin")):
            continue
        for trigger in other.get("triggers", []):
            if trigger.get("kind") != "phrase":
                continue
            for base in trigger_phrases(trigger)[:3]:
                # дословная фраза — точное совпадение своего сценария; живая реплика длиннее
                for phrase in (base, base + " сейчас"):
                    if _route(runtime, before, phrase) != ("supported", other["id"]):
                        continue
                    verdict, winner = _route(runtime, after, phrase)
                    if (verdict, winner) != ("supported", other["id"]):  # перехват или неоднозначность → в LLM
                        stolen.append({"phrase": phrase, "was": other["id"], "now": winner if verdict == "supported" else verdict})
                        break
    return {"valid": True, "results": results, "stolen": stolen,
            "ok": all(r["scenario"] == scenario_id for r in results) and not stolen}


async def _save(runtime, scenario, disable_builtin=None, script=None):
    """Активное определение: история прежней версии, затем сохранение тем же путём, что PUT /scenarios."""
    if _is_script(scenario):
        if script is not None:
            _write_script(runtime, scenario["id"], script)
        elif not (runtime.scenario_dir / (scenario["id"] + ".py")).is_file():
            raise ValueError("executor script: нет кода и файла обработчика")
    previous, _builtin = _current(runtime, scenario["id"])
    if previous is not None and not _builtin:
        history = _history(runtime, scenario["id"])
        number = len(list(history.glob("*.json"))) + 1
        (history / f"{number:04d}.json").write_text(json.dumps({"saved": time.time(), "scenario": _clean(previous)},
                                                               ensure_ascii=False, indent=1), encoding="utf-8")
    scenarios = [x for x in runtime.settings.get("scenarios", []) if x.get("id") != scenario["id"]] \
        + [{**scenario, "enabled": True}]
    settings = {**runtime.settings, "scenarios": scenarios}
    if disable_builtin:
        settings["builtin_disabled"] = sorted(set(runtime.settings.get("builtin_disabled") or []) | {disable_builtin})
    await runtime.save_settings(settings)


async def activate(runtime, draft_id, user_id=None, approved_by_admin=False):
    """→ {status: active|proposed}. Пользователь — только свой (personal) сценарий; иначе предложение."""
    draft = get_draft(runtime, draft_id)
    if draft["status"] == "active":
        raise ValueError("Черновик уже активирован")
    scenario = validate_scenario(copy.deepcopy(draft["scenario"]))
    if user_id and not approved_by_admin and not personal(scenario, user_id):
        draft["status"] = "proposed"
        _write(runtime, draft)
        return {"status": "proposed", "draft_id": draft_id,
                "note": "Сценарий общий для всех колонок: отправлен на утверждение в панель (вкладка «Сценарии»)."}
    await _save(runtime, scenario, draft.get("disable_builtin"), draft.get("script"))
    draft["status"], draft["activated"] = "active", time.time()
    _write(runtime, draft)
    return {"status": "active", "id": scenario["id"]}


def discard(runtime, draft_id):
    get_draft(runtime, draft_id)
    (_drafts(runtime) / (draft_id + ".json")).unlink()


def history(runtime, scenario_id):
    path = runtime.directory / "scenario_history" / scenario_id
    if not path.exists():
        return []
    return [{"version": int(p.stem), "saved": json.loads(p.read_text(encoding="utf-8"))["saved"]}
            for p in sorted(path.glob("*.json"))]


async def revert(runtime, scenario_id, version, user_id=None):
    path = runtime.directory / "scenario_history" / scenario_id / f"{int(version):04d}.json"
    if not path.exists():
        raise ValueError(f"Нет версии {version} сценария {scenario_id}")
    scenario = validate_scenario(json.loads(path.read_text(encoding="utf-8"))["scenario"])
    if user_id and not personal(scenario, user_id):
        raise ValueError("Откат общего сценария — только администратором в панели")
    await _save(runtime, scenario)
    return {"status": "active", "id": scenario_id, "version": int(version)}


def tool_specs(runtime, user_id):
    """Инструменты правки сценариев: [(name, description, schema, executor)]."""
    def dump(value):
        return json.dumps(value, ensure_ascii=False)

    def brief(draft):
        return {"draft_id": draft["draft_id"], "status": draft["status"], "error": draft.get("error"),
                "scenario": draft["scenario"]}

    async def scenario_draft(from_id=None, scenario=None, personal=True):
        return dump(brief(create(runtime, from_id, scenario, user_id, "agent", bool(personal and user_id))))

    async def scenario_patch(draft_id, patch_ops=None, **values):
        return dump(brief(patch(runtime, draft_id, patch_ops or values.get("patch") or [])))

    async def scenario_test(draft_id, phrases=None):
        return dump(test(runtime, draft_id, phrases or []))

    async def scenario_activate(draft_id):
        return dump(await activate(runtime, draft_id, user_id))

    async def scenario_history(id):
        return dump(history(runtime, id))

    async def scenario_revert(id, version):
        return dump(await revert(runtime, id, version, user_id))

    ops = {"type": "array", "items": {"type": "object", "properties": {
        "op": {"type": "string", "enum": ["add", "remove", "replace", "move", "copy", "test"]},
        "path": {"type": "string"}, "value": {}, "from": {"type": "string"}}, "required": ["op", "path"]}}
    return [
        ("scenario_draft", "Черновик сценария (в роутинг не попадает): from_id — копия существующего (встроенный "
         "станет user.<id>), иначе scenario — новый JSON v2 (docs: triggers phrase, flow compose/finish). "
         "Только простые сценарии: шаги compose, action, ask, finish, error; запрос данных, разбор фразы, "
         "ветвление, состояние в JSON недоступны — их пишет администратор в py, "
         "предложи пользователю обратиться к нему. "
         "personal (по умолчанию да) — сценарий только для этого пользователя.",
         {"type": "object", "properties": {"from_id": {"type": "string"}, "scenario": {"type": "object"},
                                           "personal": {"type": "boolean"}}}, scenario_draft),
        ("scenario_patch", "Изменить черновик JSON Patch (RFC 6902), например "
         "[{\"op\":\"add\",\"path\":\"/triggers/0/phrases/-\",\"value\":\"включи гирлянду\"}]. Возвращает "
         "черновик и ошибку валидатора, если есть — исправь следующим патчем.",
         {"type": "object", "properties": {"draft_id": {"type": "string"}, "patch_ops": ops},
          "required": ["draft_id", "patch_ops"]}, scenario_patch),
        ("scenario_test", "Проверить черновик: какие фразы он ловит и не перехватывает ли фразы других сценариев.",
         {"type": "object", "properties": {"draft_id": {"type": "string"},
                                           "phrases": {"type": "array", "items": {"type": "string"}}},
          "required": ["draft_id"]}, scenario_test),
        ("scenario_activate", "Включить черновик в работу (после scenario_test). Общий для всех сценарий уходит "
         "на утверждение в панель.", {"type": "object", "properties": {"draft_id": {"type": "string"}},
                                      "required": ["draft_id"]}, scenario_activate),
        ("scenario_history", "Сохранённые версии сценария.",
         {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}, scenario_history),
        ("scenario_revert", "Вернуть сценарий к версии из scenario_history.",
         {"type": "object", "properties": {"id": {"type": "string"}, "version": {"type": "integer"}},
          "required": ["id", "version"]}, scenario_revert),
    ]
