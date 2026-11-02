"""Поставить будильник (TASK-079): директивы по образцам настоящего сервера.

Время — слоты template-триггера ({time_word} или {hour}/{minute}/{day_period}), иначе
разбор фразы; нет времени — уточнение (on_time). Дата: {date_word} («сегодня», «завтра»,
«послезавтра») или ближайшее будущее (время сегодня уже прошло — ставим на завтра).

Образцы журнала 2026-10-22 05:47:24 и 2026-10-27 19:00:37: open_screen_alarm_list_directive
(ForceAutoClose) + alarm_add_directive (TAlarmAddDirective: AlarmId, LocalTime, одна дата
в ActivationDays, Timezone колонки, SnoozeTime 10). Будильник по дню недели ({weekday}) и
адресация другой колонке — БЛОКЕР (образцов нет, см. tasks/079). Своего хранилища нет:
будильник живёт у колонки, мы лишь отправляем директиву добавления.
"""
import datetime

from server_emulator import clock_directives
from server_emulator.sdk import ask, format_hhmm, say

QUESTION = "На какое время поставить будильник?"

LIST_PROTO = "NAlice.TClockAppCapability.TOpenScreenAlarmListDirective"
ADD_PROTO = "NAlice.TAlarmCapability.TAlarmAddDirective"
SNOOZE_MINUTES = 10  # SnoozeTime в обоих образцах alarm_add_directive


def _slots_time(slots):
    """«ЧЧ:ММ» из сущностей шаблона: {time_word} («полночь») или {hour} [{minute}]
    [{day_period}]. Дневной период: «дня»/«вечера» после полудня +12, «утра»/«ночи»
    «12 …» — полночь. Не время — None."""
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


def _target_of(device):
    """Только нужное для ответа — переживает ожидание (ctx.state сохраняется на диск)."""
    device = device or {}
    return {key: device.get(key) for key in ("sn", "name", "current", "aliases") if key in device}


async def handle(ctx):
    target = ctx.find_device(fields=["name", "aliases"], contains_only=True)
    time_value = _slots_time(ctx.slots)
    if time_value is None:
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
    if target.get("sn") and target.get("current") is not True:
        return say("Пока не ставлю будильник на другой колонке — только на этой.")
    if ctx.slots.get("weekday") or (ctx.slots.get("day") and ctx.slots.get("month")):
        # ActivationDays с Weekday/выборочной датой в образцах не встречался
        return say("Будильник на конкретный день недели пока не ставлю — только на ближайшие дни.")
    hours, minutes = (int(part) for part in time_value.split(":"))
    alarm_id = clock_directives.new_clock_id(ctx.state.get("clock_id"))
    now = ctx.now  # aware datetime в таймзоне колонки
    date_word = ctx.slots.get("date_word")
    days = {"завтра": 1, "послезавтра": 2}.get(str(date_word), 0)
    when = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    if not days and when <= now:
        days = 1  # время сегодня уже прошло — будильник на завтра
    when = when + datetime.timedelta(days=days)
    timezone_name = (ctx.input_timezone
                     or (ctx.params.get("effective") or {}).get("timezone")
                     or str(now.tzname() or ""))
    offset_seconds = int(now.utcoffset().total_seconds()) if now.utcoffset() is not None else 0
    when_suffix = {1: " на завтра", 2: " на послезавтра"}.get(days, "")
    spec = say(f"Будильник на {format_hhmm(time_value)} поставил{when_suffix}.")
    spec["directives"] = [
        {"name": "open_screen_alarm_list_directive", "type": "client_action", "is_led_silent": True,
         "proto": LIST_PROTO, "fields": {"ForceAutoClose": True}},
        {"name": "alarm_add_directive", "type": "client_action", "is_led_silent": True,
         "proto": ADD_PROTO,
         "fields": {"Name": "alarm_add_directive",
                    "Alarm": _alarm_fields(alarm_id, when, hours, minutes,
                                          timezone_name, offset_seconds)}},
    ]
    return spec


def _alarm_fields(alarm_id, when, hours, minutes, timezone_name, offset_seconds):
    """TAlarm в alarm_add_directive по образцу: LocalTime без нулевых полей, одна дата
    активации (одноразовый), таймзона колонки, SnoozeTime 10 минут."""
    local_time = {"Hours": hours}
    if minutes:
        local_time["Minutes"] = minutes
    return {"AlarmId": alarm_id,
            "LocalTime": local_time,
            "ActivationDays": [{"Date": {"Year": when.year, "Month": when.month, "Day": when.day}}],
            "Timezone": {"TimezoneName": timezone_name, "OffsetSeconds": offset_seconds},
            "SnoozeTime": SNOOZE_MINUTES}
