"""Отменить таймер: на колонке, названной во фразе, иначе — на той, что спросила."""
from server_emulator.sdk import say


async def handle(ctx):
    target = ctx.find_device(fields=["name", "aliases"], contains_only=True) or {}
    arguments = {"kind": "timer"}
    if target.get("sn"):
        arguments["sn"] = target["sn"]
    removed = await ctx.action("timer.cancel", **arguments)
    if (removed or 0) > 0:
        return say("Таймер отменён.")
    return say("Активных таймеров нет.")
