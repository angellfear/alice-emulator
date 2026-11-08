"""Свет умного дома через Home Assistant: «включи свет на кухне», «выключи свет»,
«свет на 50» → сервисы light.turn_on/turn_off HA (действие ha.call_service).

Тексты и логика — штатный сценарий ядра, исполнительная сторона — устройства HA.
Лампы — датасет ha.lights (ключ "devices": [{"name", "entity_id", "entity_ids"}], кеш БД,
свежесть max_age); команда и комната — слоты записи справочника фраз (`power`,
`ha_room` из закрытого списка устройств), без названной комнаты — комната колонки,
если для неё есть лампа, а иначе единственная лампа; падежные варианты комнаты
дополняет общий словарь (lists_from via room). Яркость — число 1..100 →
brightness_pct сервиса. Плагин не настроен или список ламп пуст — фразы файла
вне справочника (сюда не попасть); сбой HA при вызове — понятная фраза.
"""
import json

from server_emulator.sdk import say

DEVICES_MAX_AGE = 3600  # запись датасета читаем любой свежести запроса (пишет плагин)
UNAVAILABLE = ("Home Assistant недоступен — проверьте адрес и токен в настройках "
               "расширения «Home Assistant».")


def lamps_text(devices):
    return json.dumps([device["name"] for device in devices], ensure_ascii=False)  # как фильтр json


def _brightness(result, entity_id):
    """Процент яркости из ответа HA (список изменённых состояний): brightness_pct,
    нет — brightness 0..255; нет — None."""
    for state in result if isinstance(result, list) else []:
        if not isinstance(state, dict) or str(state.get("entity_id") or "") != entity_id:
            continue
        attributes = state.get("attributes") or {}
        if attributes.get("brightness_pct") is not None:
            return int(attributes["brightness_pct"])
        if attributes.get("brightness") is not None:
            return round(int(attributes["brightness"]) * 100 / 255)
    return None


async def handle(ctx):
    slots = ctx.slots or {}
    power = slots.get("power") or ctx.parse_lexicon("power")  # прогон без слотов справочника (агент, рутина)
    power = str(power or "").strip()
    if power not in ("on", "off"):
        raise ValueError("Не удалось понять команду света")
    record = await ctx.dataset_record("ha.lights", "devices", max_age=DEVICES_MAX_AGE)
    devices = (record or {}).get("value")
    if not isinstance(devices, list) or not devices:
        return say(UNAVAILABLE)  # список не собран (HA недоступен) — честная фраза сбоя
    names = [device.get("name") for device in devices]
    room = str(slots.get("ha_room") or "").strip()
    if not room:
        room = str(ctx.parse_lexicon("rooms") or "").strip()  # незнакомая комната в тексте
        if room and room not in names:
            return say(f"Лампы «{room}» нет. Доступны: {lamps_text(devices)}.")
        speaker = str(ctx.device.get("room") or "")
        room = next((name for name in names if name.lower() == speaker.lower()), None)
    if not room:
        if len(devices) == 1:
            room = devices[0]["name"]
        else:
            return say(f"Не понял, какую лампу: доступны {lamps_text(devices)}.")
    device = next((device for device in devices if device.get("name") == room), None)
    entities = (device or {}).get("entity_ids") or [e for e in [(device or {}).get("entity_id")] if e]
    if not entities:
        return say(f"Лампы «{room}» нет. Доступны: {lamps_text(devices)}.")
    entity = entities[0]
    arguments = {"domain": "light", "service": "turn_on" if power == "on" else "turn_off",
                 "entity_id": entities if len(entities) > 1 else entity}  # все лампы площадки
    brightness = ctx.parse_number(minimum=1, maximum=100)
    if brightness is not None:
        arguments["data"] = {"brightness_pct": brightness}
    try:
        result = await ctx.action("ha.call_service", **arguments)  # сбой HA — понятная фраза
    except Exception:  # noqa: BLE001 — сеть/HTTP/настройки: ответ пользователю, не ошибка сценария
        return say(UNAVAILABLE)
    if power == "on":
        level = _brightness(result, entity)
        if level is not None:
            return say(f"Свет ({room}) включён на {level}%.")
        return say(f"Свет ({room}) включён.")
    return say(f"Свет ({room}) выключен.")
