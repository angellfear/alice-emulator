"""Свет: «включи свет на кухне», «выключи свет», «свет на 50» → state.light колонки (TASK-025).

Логика бывшего JSON-flow. Лампы колонки — `ctx.device["lights"]` (имена комнат из
state.light); команда — `power` сценария («включи» → on), комната — `rooms`
сценария, яркость — число 1..100 (необязательно). Без названной комнаты берётся комната
колонки, если для неё есть лампа, а иначе единственная лампа. Тексты ответов те же.
"""
import json

from server_emulator.sdk import say


def lamps_text(lights):
    return json.dumps(lights, ensure_ascii=False)  # как фильтр json в шаблонах: ["кухня", "спальня"]


async def handle(ctx):
    slots = ctx.slots or {}
    power = slots.get("power") or ctx.parse_lexicon("power")
    if power is None:
        raise ValueError("Не удалось понять команду света")
    lights = list(ctx.device.get("lights") or [])
    room = slots.get("room") or ctx.parse_lexicon("rooms")
    if room is not None:
        if room not in lights:
            return say(f"Лампы «{room}» нет. Доступны: {lamps_text(lights)}.")
    elif not lights:
        return say("Лампы не настроены: добавьте state.light устройству.")
    elif ctx.device.get("room") in lights:
        room = ctx.device["room"]
    elif len(lights) == 1:
        room = lights[0]
    else:
        return say(f"Не понял, какую лампу: доступны {lamps_text(lights)}.")
    arguments = {"room": room, "on": power}
    brightness = slots.get("number")
    if brightness is not None:
        brightness = int(brightness)
    else:
        brightness = ctx.parse_number(minimum=1, maximum=100)
    if brightness is not None:
        arguments["brightness"] = brightness
    result = await ctx.action("light.set", **arguments)  # сбой действия — исключение, сценарий errored
    if power == "on":
        level = result.get("brightness") if isinstance(result, dict) else None
        if level is not None:
            return say(f"Свет ({room}) включён на {level}%.")
        return say(f"Свет ({room}) включён.")
    return say(f"Свет ({room}) выключен.")
