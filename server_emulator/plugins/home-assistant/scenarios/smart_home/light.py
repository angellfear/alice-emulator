"""Свет умного дома через Home Assistant: «включи свет на кухне», «выключи свет»,
«свет на 50» → сервисы light.turn_on/turn_off HA (действие ha.call_service).

Тексты и логика — штатный сценарий ядра, исполнительная сторона — устройства HA.
Лампы — датасет ha.lights (ключ "devices": [{"name", "entity_id", "entity_ids"}], кеш БД,
свежесть max_age); команда и комната — слоты записи справочника фраз (`power`,
`ha_room` из закрытого списка устройств). Комната — названная во фразе, иначе комната
колонки из кабинета (регистр устройств); все лампы площадки включаются вместе. Комнаты
у колонки нет — единственная лампа, а при нескольких один вопрос «В какой комнате
колонка?» (колонка слушает ответ без «Алиса»): названная комната запоминается как комната
колонки (действие device.set_room) и больше не спрашивается; запрос без колонки-адресата
включает все лампы. Падежные варианты комнаты дополняет общий словарь (lists_from via room). Яркость — число 1..100 →
brightness_pct сервиса. Плагин не настроен или список ламп пуст — фразы файла
вне справочника (сюда не попасть); сбой HA при вызове — понятная фраза.
"""
import json

from server_emulator.sdk import ask, say

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


def _norm(text):
    return str(text or "").strip().lower().replace("ё", "е")


def _device_by_name(devices, name):
    wanted = _norm(name)
    return next((device for device in devices if _norm(device.get("name")) == wanted), None) if wanted else None


def _named_in(ctx, devices, text):
    """Лампа-площадка, названная в тексте ответа («на кухне», «кухня»): точное имя,
    падежная форма общего словаря rooms, имя целыми словами."""
    found = _device_by_name(devices, text) or _device_by_name(devices, ctx.parse_lexicon("rooms", text))
    return found or ctx.find_in(devices, text, field="name", contains_only=True)


def _entities(device):
    return list(device.get("entity_ids") or [e for e in [device.get("entity_id")] if e])


async def _switch(ctx, power, brightness, targets, label):
    """Вызов light.turn_on/turn_off HA на лампы площадок targets (все вместе)."""
    entities = [entity for device in targets for entity in _entities(device)]
    if not entities:
        return say(f"Лампы «{label}» нет.")
    arguments = {"domain": "light", "service": "turn_on" if power == "on" else "turn_off",
                 "entity_id": entities if len(entities) > 1 else entities[0]}
    if brightness is not None:
        arguments["data"] = {"brightness_pct": brightness}
    try:
        result = await ctx.action("ha.call_service", **arguments)  # сбой HA — понятная фраза
    except Exception:  # noqa: BLE001 — сеть/HTTP/настройки: ответ пользователю, не ошибка сценария
        return say(UNAVAILABLE)
    if power == "on":
        level = _brightness(result, entities[0]) if len(targets) == 1 else None
        if level is not None:
            return say(f"Свет ({label}) включён на {level}%.")
        return say(f"Свет ({label}) включён.")
    return say(f"Свет ({label}) выключен.")


async def _devices(ctx):
    record = await ctx.dataset_record("ha.lights", "devices", max_age=DEVICES_MAX_AGE)
    devices = (record or {}).get("value")
    return devices if isinstance(devices, list) and devices else None


async def handle(ctx):
    slots = ctx.slots or {}
    power = slots.get("power") or ctx.parse_lexicon("power")  # прогон без слотов справочника (агент, рутина)
    power = str(power or "").strip()
    if power not in ("on", "off"):
        raise ValueError("Не удалось понять команду света")
    devices = await _devices(ctx)
    if devices is None:
        return say(UNAVAILABLE)  # список не собран (HA недоступен) — честная фраза сбоя
    brightness = ctx.parse_number(minimum=1, maximum=100)
    room = str(slots.get("ha_room") or "").strip()
    if not room:
        room = str(ctx.parse_lexicon("rooms") or "").strip()  # незнакомая комната в тексте
        if room and not _device_by_name(devices, room):
            ctx.state["light"] = {"power": power, "brightness": brightness, "remember": False}
            return ask(f"Лампы «{room}» нет. В какой комнате включить свет? "
                       f"Доступны: {lamps_text(devices)}.", then="on_room")
        room = ""
    if not room:
        speaker = str(ctx.device.get("room") or "").strip()
        if speaker:  # комната колонки из кабинета
            found = _device_by_name(devices, speaker)
            if found is None:
                return say(f"В комнате «{speaker}» ламп нет. Доступны: {lamps_text(devices)}.")
            return await _switch(ctx, power, brightness, [found], found["name"])
        if len(devices) == 1:
            return await _switch(ctx, power, brightness, devices, devices[0]["name"])
        if not ctx.sn:  # колонки-адресата нет (агент, рутина): спрашивать некого
            return await _switch(ctx, power, brightness, devices, "все лампы")
        ctx.state["light"] = {"power": power, "brightness": brightness, "remember": True}
        return ask("В какой комнате колонка?", then="on_room")
    device = _device_by_name(devices, room)
    if device is None:
        return say(f"Лампы «{room}» нет. Доступны: {lamps_text(devices)}.")
    return await _switch(ctx, power, brightness, [device], device["name"])


async def on_room(ctx, answer):
    """Ответ на вопрос о комнате: названная комната — лампы площадки; для вопроса о
    комнате колонки она ещё запоминается (больше не спрашиваем)."""
    pending = ctx.state.get("light") or {}
    if pending.get("power") not in ("on", "off"):
        raise ValueError("Не удалось понять команду света")
    devices = await _devices(ctx)
    if devices is None:
        return say(UNAVAILABLE)
    device = _named_in(ctx, devices, answer)
    if device is None:
        return say(f"Не нашла такую комнату. Доступны: {lamps_text(devices)}.")
    if pending.get("remember") and ctx.sn:
        try:
            await ctx.action("device.set_room", room=device["name"])
        except Exception:  # noqa: BLE001 — свет включаем, даже если запись комнаты не удалась
            pass
    return await _switch(ctx, pending["power"], pending.get("brightness"), [device], device["name"])
