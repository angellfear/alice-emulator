"""Отменить будильник: на колонке, названной во фразе, иначе — на той, что спросила."""
from server_emulator.sdk import say


async def handle(ctx):
    target = ctx.find_device(fields=["name", "aliases"], contains_only=True) or {}
    arguments = {"kind": "alarm"}
    if target.get("sn"):
        arguments["sn"] = target["sn"]
    removed = await ctx.action("timer.cancel", **arguments)
    if (removed or 0) > 0:
        return say("Будильник отменён.")
    return say("Активных будильников нет.")
