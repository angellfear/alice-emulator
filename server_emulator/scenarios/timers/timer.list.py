"""Состояние таймеров колонки (TASK-079): «сколько осталось до таймера».

Таймеры — только из состояния запроса (EnvironmentStateRaw); остаток считается как
настоящий сервер: Duration − (now − StartTimestamp) (сверено с «Осталось 47 минут 48
секунд», образец 2026-10-25 16:13:09). Директива — open_screen_timer_list_directive с
TargetTimerIds интересующих таймеров. Нет активных — честный ответ без директив
(образца для этого случая нет).
"""
from server_emulator import station_state
from server_emulator.sdk import format_duration, say

LIST_PROTO = "NAlice.TClockAppCapability.TOpenScreenTimerListDirective"


def _pick(timers, slots):
    """Таймер по порядковому слоту шаблона («до второго таймера»), иначе ближайший."""
    ordinal = slots.get("ordinal")
    if ordinal is not None:
        index = int(ordinal) - 1
        if index < 0:
            index += len(timers)
        if 0 <= index < len(timers):
            return timers[index]
    return None


async def handle(ctx):
    now = int(ctx.now.timestamp())
    timers = station_state.active_timers(ctx.timers, now)
    if not timers:
        return say("Активных таймеров нет.")
    chosen = _pick(timers, ctx.slots)
    if chosen is not None or len(timers) == 1:
        timer = chosen or timers[0]
        spec = say(f"Осталось {format_duration(timer['remaining'])}")
        ids = [timer["timer_id"]]
    else:
        nearest = timers[0]
        spec = say(f"Активных таймеров {len(timers)}, до ближайшего — {format_duration(nearest['remaining'])}.")
        ids = [timer["timer_id"] for timer in timers]
    spec["directives"] = [
        {"name": "open_screen_timer_list_directive", "type": "client_action", "is_led_silent": True,
         "proto": LIST_PROTO, "fields": {"TargetTimerIds": ids}},
    ]
    return spec
