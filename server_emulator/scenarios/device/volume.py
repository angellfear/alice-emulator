"""Громкость: «громче», «тише», «громкость 5», «выключи звук», «максимальная громкость»
→ Push в колонку (TASK-025). Шкала колонки — 0..10; значения больше 10 читаются как проценты.

Логика бывшего JSON-flow: режим (`modes` сценария) → действие device.push; без режима —
число 0..100. Квирк прежнего flow сохранён как есть: уровень из процентов уходит строкой
(`"5"` — фильтр div возвращал str), а из шкалы 0..10 — числом.
"""
from server_emulator.sdk import say


async def push(ctx, action, **arguments):
    payload = {"action": action}
    if arguments:
        payload["arguments"] = arguments
    await ctx.action("device.push", **payload)


async def handle(ctx):
    slots = ctx.slots or {}
    mode = slots.get("volume_fixed") or slots.get("volume_mode") or ctx.parse_lexicon("modes")
    if mode == "mute":
        await push(ctx, "sound_set_level", level=0)
        return say("Звук выключен.")
    if mode == "max":
        await push(ctx, "sound_set_level", level=10)
        return say("Максимальная громкость.")
    if mode == "up":
        await push(ctx, "sound_louder")
        return say("Громче.")
    if mode is not None:  # любой иной режим словаря — «тише», как ветка else прежнего flow
        await push(ctx, "sound_quiter")
        return say("Тише.")
    level = slots.get("number")
    if level is not None:
        level = int(level)
    else:
        level = ctx.parse_number(minimum=0, maximum=100)
    if level is None:
        raise ValueError("Не удалось понять громкость")
    if level > 10:
        level = str(int(float(level) // 10.0))  # проценты → шкала 0..10, строкой (как фильтр div)
    await push(ctx, "sound_set_level", level=level)
    return say(f"Громкость {level} из 10.")
