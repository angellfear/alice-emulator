"""Отменить таймер (TASK-079): таймер берётся из состояния колонки в запросе.

Своего хранилища нет — «отменить» можно только то, о чём колонка сама сообщила
(Active: Disabled=false и remaining>0). Поряд и поля директив — по образцу журнала
2026-10-25 17:00:33: open_screen_timer_list_directive (ForceAutoClose) +
timer_cancel_directive (TTimerCancelDirective). Нет активного таймера — честный отказ.
Адресат по имени (другая колонка) — БЛОКЕР, образца адресации директив нет.
"""
from server_emulator import station_state
from server_emulator.sdk import say

LIST_PROTO = "NAlice.TClockAppCapability.TOpenScreenTimerListDirective"
CANCEL_PROTO = "NAlice.TAlarmCapability.TTimerCancelDirective"


def _pick(timers, slots):
    """Таймер по порядковому слоту шаблона («второй таймер»), иначе ближайший по остатку."""
    ordinal = slots.get("ordinal")
    if ordinal is not None:
        index = int(ordinal) - 1
        if index < 0:
            index += len(timers)
        if 0 <= index < len(timers):
            return timers[index]
    return timers[0]


async def handle(ctx):
    target = ctx.find_device(fields=["name", "aliases"], contains_only=True)
    if target and target.get("sn") and target.get("current") is not True:
        return say("Пока не отменяю таймер на другой колонке — только на этой.")
    now = int(ctx.now.timestamp())
    timers = station_state.active_timers(ctx.timers, now)
    if not timers:
        return say("Активных таймеров нет.")
    timer = _pick(timers, ctx.slots)
    spec = say("Таймер отменён.")
    spec["directives"] = [
        {"name": "open_screen_timer_list_directive", "type": "client_action", "is_led_silent": True,
         "proto": LIST_PROTO, "fields": {"ForceAutoClose": True}},
        {"name": "timer_cancel_directive", "type": "client_action", "is_led_silent": True,
         "proto": CANCEL_PROTO,
         "fields": {"Name": "cancel_timer_capability_directive", "TimerId": timer["timer_id"]}},
    ]
    return spec
