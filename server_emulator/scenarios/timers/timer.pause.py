"""Пауза таймера («поставь таймер на паузу»): таймер берётся из состояния колонки.

Активные незапауженные таймеры (station_state.active_timers) — кандидат; один —
ставим на паузу (timer_pause_directive, TTimerPauseDirective по protos), несколько —
уточняющий вопрос с продолжением on_pick (номер таймера). Нет активных — честный отказ.
Своего хранилища нет: paused остаётся в состоянии колонки (она же возобновляет отсчёт).
"""
from server_emulator import clock_directives, station_state
from server_emulator.sdk import ask, say

QUESTION = "Какой таймер поставить на паузу? Скажите номер."


async def handle(ctx):
    timers = [timer for timer in station_state.active_timers(ctx.timers, int(ctx.now.timestamp()))
              if not timer.get("paused")]
    if not timers:
        return say("Активных таймеров нет.")
    if len(timers) == 1:
        return await _pause(ctx, timers[0])
    ctx.state["timer_ids"] = [timer["timer_id"] for timer in timers]
    return ask(QUESTION, then="on_pick", expect="ordinal")


async def on_pick(ctx, answer):
    timer_id = _picked(ctx, answer)
    if timer_id is None:
        raise ValueError("Не удалось понять, какой таймер поставить на паузу")
    return await _pause(ctx, {"timer_id": timer_id})


def _picked(ctx, answer):
    """Номер таймера из ответа («первый», «два»): в порядку списка из уточнения."""
    text = str(answer or "").lower().replace("ё", "е")
    ids = ctx.state.get("timer_ids") or []
    index = ctx.parse_number(text)
    if index is None:
        index = ctx.parse_lexicon("ordinal", text)  # «второй» — лексикон порядковых
    if index is not None and 1 <= int(index) <= len(ids):
        return ids[int(index) - 1]
    return None


async def _pause(ctx, timer):
    spec = say("Таймер поставила на паузу.")
    spec["directives"] = [
        {"name": item["name"], "type": "client_action", "is_led_silent": True,
         "proto": item["proto"], "fields": item["fields"]}
        for item in clock_directives.timer_pause_items(timer["timer_id"])
    ]
    return spec
