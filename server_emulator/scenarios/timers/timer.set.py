"""Поставить таймер: «таймер на 5 минут», «поставь таймер на дуо 32 на минуту».

Адресат (колонка владельца) ищется по name/aliases, его алиасы вырезаются из фразы до
разбора длительности. Нет длительности — уточнение; ответ разбирает on_duration.
"""
from server_emulator.sdk import ask, format_duration, say

QUESTION = "На сколько поставить таймер?"


def _target_of(device):
    """Только нужное для ответа — переживает ожидание (ctx.state сохраняется на диск)."""
    device = device or {}
    return {key: device.get(key) for key in ("sn", "name", "current", "aliases") if key in device}


async def handle(ctx):
    target = ctx.find_device(fields=["name", "aliases"], contains_only=True)
    seconds = ctx.parse_duration(strip=(target or {}).get("aliases"))
    if seconds is None:
        ctx.state["target"] = _target_of(target)
        return ask(QUESTION, then="on_duration", expect="duration")
    return await _start(ctx, target, seconds)


async def on_duration(ctx, answer):
    seconds = ctx.parse_duration(answer)
    if seconds is None:
        raise ValueError("Не удалось понять длительность таймера")
    return await _start(ctx, ctx.state.get("target"), seconds)


async def _start(ctx, target, seconds):
    target = target or {}
    arguments = {"seconds": seconds}
    if target.get("sn"):
        arguments["sn"] = target["sn"]
    await ctx.action("timer.start", **arguments)
    text = f"Таймер на {format_duration(seconds)}"
    if target.get("sn") and target.get("current") is not True:
        text += f" на колонке «{target.get('name')}»"
    return say(text + ".")
