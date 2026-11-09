"""Продолжить таймер («продолжи таймер», «сними таймер с паузы»).

Кандидаты — таймеры с Paused=true (и не Disabled) из состояния колонки: один —
возобновляем (timer_resume_directive, TTimerResumeDirective по protos), несколько —
уточняющий вопрос с продолжением on_pick (номер таймера), нет — честный отказ.
Отсчёт после возобновления ведёт сама колонка — своего счётчика у эмулятора нет.
"""
from server_emulator import clock_directives
from server_emulator.sdk import ask, say

QUESTION = "Какой таймер продолжить? Скажите номер."


def _paused(timers):
    return [timer for timer in timers if not timer.get("disabled") and timer.get("paused")]


async def handle(ctx):
    timers = _paused(ctx.timers)
    if not timers:
        return say("Таймеров на паузе нет.")
    if len(timers) == 1:
        return await _resume(ctx, timers[0])
    ctx.state["timer_ids"] = [timer["timer_id"] for timer in timers]
    return ask(QUESTION, then="on_pick", expect="ordinal")


async def on_pick(ctx, answer):
    timer_id = _picked(ctx, answer)
    if timer_id is None:
        raise ValueError("Не удалось понять, какой таймер продолжить")
    return await _resume(ctx, {"timer_id": timer_id})


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


async def _resume(ctx, timer):
    spec = say("Таймер запустила.")
    spec["directives"] = [
        {"name": item["name"], "type": "client_action", "is_led_silent": True,
         "proto": item["proto"], "fields": item["fields"]}
        for item in clock_directives.timer_resume_items(timer["timer_id"])
    ]
    return spec
