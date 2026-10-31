"""Общий помощник «слои источников» (TASK-030): каталоги точки подключения
(сценарии, эндпоинты, Quasar, списки) в порядке приоритета — встроенное →
включённые расширения → пользовательское; перекрытие в обратном порядке
(поздний слой сильнее). Один помощник вместо копий логики в каждом каталоге;
формат origin-метки записей — тоже отсюда (docs/contracts/extensions.md,
«Приоритет при совпадении имён»).
"""
from __future__ import annotations

import json
from pathlib import Path

BUILTIN = "builtin"
USER = "user"


def extension_origin(ext_id):
    """origin записи расширения: «extension:<id>»."""
    return f"extension:{ext_id}"


def source_layers(manager, name, builtin=(), user=()):
    """[(origin, Path)] — слои sources точки подключения name в порядке приоритета
    (ранний слой перекрывается поздним).

    manager — ExtensionManager (None или без загруженных расширений — слой
    расширений пуст); builtin/user — встроенные и пользовательские пути (каталоги
    или отдельные файлы). Расширение выключено, битое или не contribution — его
    слоя нет (contribution_dirs отдаёт только включённые и только существующие
    каталоги). Сбой менеджера не роняет каталог: слой расширений просто пуст.
    """
    layers = [(BUILTIN, Path(path)) for path in builtin]
    if manager is not None and name:
        try:
            dirs = manager.contribution_dirs(name)
        except Exception:  # noqa: BLE001 — расширения не должны ломать каталог
            dirs = []
        layers.extend((extension_origin(ext_id), Path(path)) for ext_id, path in dirs)
    layers.extend((USER, Path(path)) for path in user)
    return layers


def layer_records(layers, warn=None):
    """[(origin, Path)] слоёв → [(origin, путь, данные)] — данные разделов из
    расширений (TASK-032): каждый *.json файл слоя (sorted по имени), разобранный
    как JSON. Файл не читается или не разбирается — warn(путь, причина) и идём
    дальше: некорректная запись не роняет раздел. Порядок — как у слоёв (ранний
    слой перекрывается поздним, запись с тем же id у позднего сильнее)."""
    result = []
    for origin, directory in layers:
        try:
            files = sorted(Path(directory).glob("*.json"))
        except OSError:  # noqa: PERF203 — слой не должен ломать раздел
            continue
        for path in files:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                if warn is not None:
                    warn(f"{path}: {str(exc)[:150]}")
                continue
            result.append((origin, path, value))
    return result
