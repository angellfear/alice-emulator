"""Отменить будильник («отмени будильник на 7 утра»): будильник из состояния колонки.

Время — слоты template-триггера (как в alarm.set), иначе все будильники состояния.
Звонящий (CurrentlyPlaying) — не кандидат: его останавливает timers.ringing.stop.
Один кандидат — отменяем (alarm_cancel_directive, TAlarmCancelDirective по protos);
несколько — уточняющий вопрос с продолжением on_pick (номер или время), нет — честный
отказ. Своего хранилища нет: будильник живёт у колонки.
"""
from server_emulator import clock_directives
from server_emulator.sdk import ask, format_hhmm, say

QUESTION = "Какой будильник отменить? Скажите номер или время."

LIST_PROTO = "NAlice.TClockAppCapability.TOpenScreenAlarmListDirective"
CANCEL_PROTO = "NAlice.TAlarmCapability.TAlarmCancelDirective"


def _slots_time(slots):
    """«ЧЧ:ММ» из сущностей шаблона — как в timers.alarm.set (сценарии самодостаточны):
    {time_word} («полночь») или {hour} [{minute}] [{day_period}]; дневной период после
    полудня +12, «утра/ночи» с «12» — полночь. Не время — None."""
    word = slots.get("time_word")
    if word:
        return str(word)
    hour = slots.get("hour")
    if hour is None:
        return None
    hour = int(hour)
    period = slots.get("day_period")
    if period in ("дня", "вечера") and hour < 12:
        hour += 12
    elif period in ("утра", "ночи") and hour == 12:
        hour = 0
    minute = int(slots.get("minute") or 0)
    return f"{hour:02d}:{minute:02d}"


def _active(alarms):
    """Будильники-кандидаты: включённые и не звонящие сейчас."""
    return [alarm for alarm in alarms
            if not alarm.get("disabled") and not alarm.get("currently_playing")]


def _matches(alarms, time_value, ordinal=None):
    """Совпадения: по времени («07:30»), иначе по порядковому слоту ({ordinal},
    «второй будильник»), иначе все кандидаты."""
    if time_value:
        hours, minutes = (int(part) for part in time_value.split(":"))
        return [alarm for alarm in alarms
                if alarm.get("time", {}).get("hours") == hours
                and alarm.get("time", {}).get("minutes") == minutes]
    if ordinal is not None:
        index = int(ordinal) - 1
        if index < 0:  # «последний» — с конца списка (лексикон ordinal: -1)
            index += len(alarms)
        if 0 <= index < len(alarms):
            return [alarms[index]]
        return []
    return alarms


async def handle(ctx):
    alarms = _active(ctx.alarms)
    time_value = _slots_time(ctx.slots)
    ordinal = ctx.slots.get("ordinal")
    if not alarms:
        return say("Будильников нет.")
    candidates = _matches(alarms, time_value, ordinal)
    if not candidates:
        if time_value:
            return say(f"Будильника на {format_hhmm(time_value)} нет.")
        return say("Такого будильника нет.")
    if len(candidates) == 1:
        return await _cancel(ctx, candidates[0])
    ctx.state["alarm_ids"] = [alarm["alarm_id"] for alarm in candidates]
    ctx.state["alarm_times"] = [alarm.get("time") or {} for alarm in candidates]
    return ask(QUESTION, then="on_pick", expect="ordinal")


async def on_pick(ctx, answer):
    alarm_id = _picked(ctx, answer)
    if alarm_id is None:
        raise ValueError("Не удалось понять, какой будильник отменить")
    return await _cancel(ctx, {"alarm_id": alarm_id})


def _picked(ctx, answer):
    """Будильник из ответа: номер («первый», «два») или время («семь утра»)."""
    text = str(answer or "").lower().replace("ё", "е")
    ids = ctx.state.get("alarm_ids") or []
    index = ctx.parse_number(text)
    if index is None:
        index = ctx.parse_lexicon("ordinal", text)  # «второй» — лексикон порядковых
    if index is not None and 1 <= int(index) <= len(ids):
        return ids[int(index) - 1]
    time_value = ctx.parse_time(text)
    if time_value:
        hours, minutes = (int(part) for part in time_value.split(":"))
        for position, alarm_time in enumerate(ctx.state.get("alarm_times") or []):
            if alarm_time.get("hours") == hours and alarm_time.get("minutes") == minutes:
                return ids[position]
    return None


async def _cancel(ctx, alarm):
    spec = say("Будильник отменила.")
    spec["directives"] = [
        {"name": item["name"], "type": "client_action", "is_led_silent": True,
         "proto": item["proto"], "fields": item["fields"]}
        for item in clock_directives.alarm_cancel_items(alarm["alarm_id"])
    ]
    return spec
