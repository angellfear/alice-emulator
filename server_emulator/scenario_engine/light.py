"""Эффект «свет»: изменение state.light устройства (умный дом эмулятора).

Разбор фразы (комнаты, вкл/выкл, яркость) и выбор лампы — в JSON-сценарии
smart_home/light.switch; здесь только запись состояния.
"""
from __future__ import annotations


def entities_of(device):
    """Сущности света устройства: {room: {on, brightness}} или {}."""
    light = ((device or {}).get("state") or {}).get("light")
    return light if isinstance(light, dict) else {}


def apply_light(identities, sn, room, action, brightness=None):
    """Исполнитель: изменение state.light устройства. Возвращает новое состояние комнаты."""
    registry = identities.public()
    device = next((d for d in registry["devices"] if d["sn"] == sn), None)
    if device is None:
        raise ValueError("device_unknown")
    entities = entities_of(device)
    if room not in entities:
        raise ValueError("room_unknown")
    state = device.get("state") or {}
    room_state = dict(entities.get(room) or {})
    room_state["on"] = action == "on"
    if brightness is not None:
        room_state["brightness"] = brightness
    state["light"] = {**entities, room: room_state}
    identities.mutate({"operation": "state", "sn": sn, "state": state})
    return room_state
