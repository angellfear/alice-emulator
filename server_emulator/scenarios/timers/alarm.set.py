"""Поставить будильник: «будильник на 7:30», «поставь будильник на дуо 32 на 7 утра».

Адресат (колонка владельца) ищется по name/aliases, его алиасы вырезаются из фразы до
разбора времени. Нет времени — уточнение; ответ разбирает on_time.
"""
from server_emulator.sdk import ask, format_hhmm, say

QUESTION = "На какое время поставить будильник?"


def _target_of(device):
    """Только нужное для ответа — переживает ожидание (ctx.state сохраняется на диск)."""
    device = device or {}
    return {key: device.get(key) for key in ("sn", "name", "current", "aliases") if key in device}


async def handle(ctx):
    target = ctx.find_device(fields=["name", "aliases"], contains_only=True)
    time_value = ctx.parse_time(strip=(target or {}).get("aliases"))
    if time_value is None:
        ctx.state["target"] = _target_of(target)
        return ask(QUESTION, then="on_time", expect="time")
    return await _start(ctx, target, time_value)


async def on_time(ctx, answer):
    time_value = ctx.parse_time(answer)
    if time_value is None:
        raise ValueError("Не удалось понять время будильника")
    return await _start(ctx, ctx.state.get("target"), time_value)


async def _start(ctx, target, time_value):
    target = target or {}
    arguments = {"time": time_value}
    if target.get("sn"):
        arguments["sn"] = target["sn"]
    timezone = (ctx.params.get("effective") or {}).get("timezone")
    if timezone:
        arguments["timezone"] = timezone
    await ctx.action("alarm.start", **arguments)
    text = f"Будильник на {format_hhmm(time_value)}"
    if target.get("sn") and target.get("current") is not True:
        text += f" на колонке «{target.get('name')}»"
    return say(text + ".")
