"""Выбор модели: карточка диалога / действие / оба (запрос пользователя).

Модель получает инструмент `alice_action` (только при settings.model_actions).
Она может ответить текстом (карточка+голос), вызвать действие, или и то и то.

Исполнители по видам (v9 §5: без фиктивных успехов):
- light {room, on, brightness?} → adapter state.light (реальное изменение состояния);
- timer {seconds} → серверный планировщик;
- volume {level 0..10} → директива sound_set_level (wire из перехвата volume-set);
- app {package}/music {query}/activity {name} → исполнитель НЕ подключён:
  вызов попадает в pending_calls (ручная панель) и получает честную карточку-заметку.
"""
from __future__ import annotations

TOOL_NAME = "alice_action"

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Выполнить действие на устройстве Алисы. Можно совмещать с текстовым "
                       "ответом: текст станет карточкой диалога и голосом, действие выполнится "
                       "дополнительно. kinds: light (room, on, brightness), timer (seconds), "
                       "volume (level 0..10), app (package), music (query), activity (name).",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["light", "timer", "volume", "app", "music", "activity"]},
                "room": {"type": "string"}, "on": {"type": "boolean"}, "brightness": {"type": "integer"},
                "seconds": {"type": "integer"}, "level": {"type": "number"},
                "package": {"type": "string"}, "query": {"type": "string"}, "name": {"type": "string"},
            },
            "required": ["kind"],
        },
    },
}

UNSUPPORTED = {"app", "music", "activity"}


def parse_calls(tool_calls):
    """Извлекает вызовы alice_action из ответа модели. Остальные инструменты остаются ручными."""
    selected = []
    for call in tool_calls or []:
        fn = (call.get("function") or {})
        if fn.get("name") != TOOL_NAME:
            continue
        try:
            arguments = __import__("json").loads(fn.get("arguments") or "{}")
        except ValueError:
            continue
        if isinstance(arguments, dict) and arguments.get("kind"):
            selected.append({"id": call.get("id"), **arguments})
    return selected


def execute(runtime, calls, sn, response, directives=None):
    """Исполняет выбранные моделью действия. response — Event.AliceResponse (мутируется:
    директивы громкости); response=None (цикл агента, ответа ещё нет) — директивы
    складываются в directives и добавляются в AliceResponse голосовым трактом.
    Возвращает (заметки-карточки, выполненные, отложенные)."""
    import uuid

    from .response_compose import add_volume_directive

    notes, executed, deferred = [], [], []
    for call in calls:
        kind = call.get("kind")
        try:
            if kind == "light":
                from .scenario_engine.light import apply_light, entities_of
                device = next((d for d in runtime.identities.data["devices"] if d["sn"] == sn), None)
                entities = entities_of(device)
                room = call.get("room")
                if not entities or room not in entities:
                    rooms = ", ".join(sorted(entities)) or "лампы не настроены"
                    notes.append(f"Свет: не найдена комната ({rooms}).")
                    continue
                result = apply_light(runtime.identities, sn, room,
                                     "on" if call.get("on") else "off", call.get("brightness"))
                state = "включён" if result.get("on") else "выключен"
                level = f" на {result['brightness']}%" if result.get("on") and "brightness" in result else ""
                notes.append(f"Свет ({room}) {state}{level}.")
                executed.append(call)
            elif kind == "timer":
                seconds = int(call.get("seconds") or 0)
                runtime.timer_scheduler.schedule(sn=sn, seconds=seconds)
                notes.append(f"Таймер на {seconds} с поставлен.")
                executed.append(call)
            elif kind == "volume":
                level = max(0.0, min(10.0, float(call.get("level") or 0)))
                if response is not None:
                    add_volume_directive(response, level)
                else:
                    directives.append({"kind": "volume", "level": level})
                    notes.append(f"Громкость {level:g}.")
                executed.append(call)  # директива уходит в этом же AliceResponse
            elif kind in UNSUPPORTED:
                runtime.pending_calls = (runtime.pending_calls + [{
                    "id": call.get("id") or uuid.uuid4().hex,
                    "function": {"name": f"alice_action.{kind}", "arguments": __import__("json").dumps(call, ensure_ascii=False)},
                    "origin": "model_action",
                }])[-30:]
                what = call.get("package") or call.get("query") or call.get("name") or kind
                notes.append(f"Действие {kind} ({what}): исполнитель не подключён — вызов сохранён в панели.")
                deferred.append(call)
            else:
                notes.append(f"Неизвестный вид действия: {kind}.")
        except Exception as exc:  # действие не должно ронять ответ
            notes.append(f"Действие {kind} не выполнено: {type(exc).__name__}.")
    return notes, executed, deferred
