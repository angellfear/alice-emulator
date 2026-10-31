"""Встроенная библиотека сценариев: server_emulator/scenarios/<category>/*.json.

Один JSON-файл — один сценарий формата v2 (docs/contracts/scenario-json-v2.md).
Разделы — categories.json, общие словари — lexicons.json. Загрузка при импорте;
битый/невалидный файл пропускается с записью в ERRORS (виден в API), остальные
работают. Порядок каталога: fallback последними, затем priority, затем id.

Слой расширений (TASK-030): extension_scenarios() читает scenarios/ включённых
расширений при каждом запросе каталога (runtime._scenarios) — включение/выключение
расширения меняет каталог без рестарта; записи помечаются _origin="extension:<id>"
и _file (путь от корня расширения) — резолвер script_path (runtime.py) находит .py
рядом с JSON. Записи расширения только для чтения: правка — пользовательская копия
в settings (как у встроенных).
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "scenarios"


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def catalog_order(scenario):
    return (bool(scenario.get("fallback")), int(scenario.get("priority", 100)), str(scenario.get("id")))


def _load():
    from ..scenario_packages.validate import PackageError, validate_scenario
    categories = _read(ROOT / "categories.json", [])
    known = {item["id"] for item in categories}
    scenarios, errors = {}, []
    for path in sorted(ROOT.glob("*/*.json")):
        item = _read(path, None)
        where = f"{path.parent.name}/{path.name}"
        if not isinstance(item, dict):
            errors.append({"file": where, "error": "не JSON-объект"})
            continue
        try:
            validate_scenario(item)
        except PackageError as exc:
            errors.append({"file": where, "error": str(exc)})
            continue
        if item.get("category") != path.parent.name or path.parent.name not in known:
            errors.append({"file": where, "error": f"category {item.get('category')!r} ≠ папке {path.parent.name!r}"})
            continue
        if item["id"] in scenarios:
            errors.append({"file": where, "error": f"повтор id {item['id']}"})
            continue
        scenarios[item["id"]] = {**item, "_origin": "builtin", "_file": where}
    ordered = dict(sorted(scenarios.items(), key=lambda pair: catalog_order(pair[1])))
    return ordered, categories, _read(ROOT / "lexicons.json", {}), errors


def extension_scenarios(manager):
    """Слой scenarios/ включённых расширений (TASK-030): JSON v2 из
    <ext>/scenarios/<раздел>/*.json (раздел — папка, category ей равна; как в
    библиотеке, но без categories.json — у расширения её нет). Читается при каждом
    вызове: правки dev-папки и включение/выключение расширений видны сразу.

    Возвращает (словарь id → сценарий, errors): битый/невалидный файл пропускается
    с записью в errors, остальные работают; повтор id (внутри расширения или между
    расширениями) получает поздний файл. Сценарий помечается _origin="extension:<id>"
    и _file — путь JSON от корня расширения (script_path резолвит .py рядом)."""
    import os

    from ..extensions.layers import source_layers
    from ..scenario_packages.validate import PackageError, validate_scenario
    scenarios, errors = {}, []
    if manager is None:
        return scenarios, errors
    try:
        layers = [layer for layer in source_layers(manager, "scenarios")
                  if layer[0].startswith("extension:")]
    except Exception:  # noqa: BLE001 — расширения не должны ломать каталог
        return scenarios, errors
    for origin, layer_root in layers:
        ext_id = origin.split(":", 1)[1]
        ext_root = Path((manager.loaded.get(ext_id) or {}).get("dir") or layer_root)
        for path in sorted(layer_root.glob("*/*.json")):
            item = _read(path, None)
            where = f"{ext_id}:{path.relative_to(layer_root).as_posix()}"
            if not isinstance(item, dict):
                errors.append({"file": where, "error": "не JSON-объект"})
                continue
            try:
                validate_scenario(item)
            except PackageError as exc:
                errors.append({"file": where, "error": str(exc)})
                continue
            if item.get("category") != path.parent.name:
                errors.append({"file": where,
                               "error": f"category {item.get('category')!r} ≠ папке {path.parent.name!r}"})
                continue
            if item["id"] in scenarios:
                errors.append({"file": where, "error": f"повтор id {item['id']}"})
                continue
            try:
                relative = path.relative_to(ext_root).as_posix()
            except ValueError:  # contributes указывает вне корня расширения — честный относительный путь
                relative = Path(os.path.relpath(path, ext_root)).as_posix()
            scenarios[item["id"]] = {**item, "_origin": origin, "_file": relative}
    return scenarios, errors


SCENARIOS, CATEGORIES, LEXICONS, ERRORS = _load()
