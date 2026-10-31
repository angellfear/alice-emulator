"""H02 (v5): probe — решение о поддержке ДО исполнения, без побочных эффектов.

dependencies_ok сверяет реестры одного сценария (действия/композеры/резолверы):
unsupported — нет зависимости, reason code называет первую отсутствующую;
rejected — формат определения не совпадает.

probe — совместимый фронт: первый кандидат роутера + неоднозначность. Guard
(право отказа) здесь НЕ проверяется: условие зависит от контекста запроса
(пользователь/устройство), его разрешает диалоговый слой по всем кандидатам.
Публикационный гейт — атрибут реестра пакетов (v9 §7), не локального каталога.
"""
from __future__ import annotations

from .router import candidates


def dependencies_ok(scenario, executor):
    """(verdict, reason): supported | unsupported | rejected — без исполнения.
    Зависимости сценария — только примитивы движка: action, resolve.
    TASK-019: py-сценарий (executor kind: script) — supported, только если резолвер
    находит файл .py; иначе unsupported script_missing (роутер его не выбирает)."""
    if scenario.get("format_version") != 2:
        return "rejected", f"format_version:{scenario.get('format_version')}"
    if (scenario.get("executor") or {}).get("kind") == "script":
        resolver = getattr(executor, "script_path", None)
        path = resolver(scenario) if resolver is not None else None
        if path is None or not path.is_file():
            return "unsupported", "script_missing"
        return "supported", "script"
    registries = {"action": executor.actions, "resolve": executor.resolvers}
    for step in scenario.get("flow", []):
        op = step.get("op")
        if op in registries and step.get("ref") not in registries[op]:
            return "unsupported", f"missing_dependency:{op}:{step.get('ref')}"
    return "supported", "deps"


def is_builtin(scenario):
    return scenario.get("_origin") == "builtin"


def probe(scenarios, executor, event_name=None, text=None):
    """→ (verdict, reason, scenario|None). Побочных эффектов нет.

    Неоднозначность: другой сценарий совпадает тем же способом (выбор первого
    молча скрывает неоднозначность каталога). Пары builtin-сценариев решает
    курируемый порядок каталога (специфичное объявлено раньше generic)."""
    found = candidates(scenarios, event_name=event_name, text=text)
    if not found:
        return "unsupported", "no_match", None
    scenario, reason = found[0]
    verdict, dep_reason = dependencies_ok(scenario, executor)
    if verdict != "supported":
        return verdict, dep_reason, scenario
    for other, other_reason in found[1:]:
        if other is scenario or other.get("id") == scenario.get("id"):
            continue
        if is_builtin(scenario) and is_builtin(other):
            continue
        if other_reason == reason:
            return "ambiguous", f"tie:{other.get('id')}:{reason}", scenario
    return "supported", reason, scenario
