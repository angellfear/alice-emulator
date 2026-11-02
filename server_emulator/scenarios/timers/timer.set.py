"""Поставить таймер: «таймер на 5 минут» (TASK-079 — директивы по образцам сервера).

Адресат (колонка владельца) ищется по name/aliases, его алиасы вырезаются из фразы до
разбора длительности. Длительность — сначала слоты template-триггера ({duration_fixed}
или пара-тройка {duration_number} {duration_unit}), иначе разбор фразы. Нет длительности —
уточнение; ответ разбирает on_duration.

Ответ — те же директивы и карточка с речью, что настоящий сервер на ту же фразу
(образцы журнала 2026-10-23 14:41:52, 2026-10-25 15:00:54, 2026-10-25 19:21:30:
экран + timer_set_directive + «Хорошо. 6 минут. Отсчёт пошёл.»).
Своего хранилища таймеров у эмулятора нет — состояние колонки в запросе, директивы
колонке-автору запроса (другая колонка — БЛОКЕР, образца нет).
"""
from server_emulator import clock_directives
from server_emulator.sdk import ask, format_duration, say

QUESTION = "На сколько поставить таймер?"

SET_PROTO = "NAlice.TAlarmCapability.TTimerSetDirective"
INFO_PROTO = "NAlice.TClockAppCapability.TOpenScreenTimerInfoDirective"
LIST_PROTO = "NAlice.TClockAppCapability.TOpenScreenTimerListDirective"


def _slots_seconds(slots):
    """Секунды из сущностей шаблона: fixed («полчаса» → 1800) или {число} {единица}
    (пара и добавка «2 часа 15 минут»). Не таймер-слоты — None."""
    fixed = slots.get("duration_fixed")
    if fixed is not None:
        return int(fixed)
    number, unit = slots.get("duration_number"), slots.get("duration_unit")
    if number is None or unit is None:
        return None
    seconds = int(number) * int(unit)
    extra_number, extra_unit = slots.get("duration_number2"), slots.get("duration_unit2")
    if extra_number is not None and extra_unit is not None:
        seconds += int(extra_number) * int(extra_unit)
    return seconds


def _target_of(device):
    """Только нужное для ответа — переживает ожидание (ctx.state сохраняется на диск)."""
    device = device or {}
    return {key: device.get(key) for key in ("sn", "name", "current", "aliases") if key in device}


async def handle(ctx):
    target = ctx.find_device(fields=["name", "aliases"], contains_only=True)
    seconds = _slots_seconds(ctx.slots)
    if seconds is None:
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


def set_directives(ctx, timer_id, seconds):
    """Директивы установки таймера по образцу: сначала экран — закрытие списка
    (ForceAutoClose), если у колонки открыт экран «Часов», иначе экран нового таймера
    (open_screen_timer_info_directive); затем timer_set_directive (TTimerSetDirective,
    Remaining = Duration, как слал сервер)."""
    screen_open = any(key in ctx.screens for key in ("timer_list", "timer_info", "timer_active"))
    screen = {"name": "open_screen_timer_list_directive", "proto": LIST_PROTO,
              "fields": {"ForceAutoClose": True}} if screen_open else \
             {"name": "open_screen_timer_info_directive", "proto": INFO_PROTO,
              "fields": {"TimerId": timer_id}}
    screen["is_led_silent"] = True
    screen["type"] = "client_action"
    timer_set = {"name": "timer_set_directive", "type": "client_action", "is_led_silent": True,
                 "proto": SET_PROTO,
                 "fields": {"Name": "set_timer_capability_directive",
                            "Timer": {"TimerId": timer_id,
                                      "StartTimestamp": int(ctx.now.timestamp()),
                                      "Duration": int(seconds), "Remaining": int(seconds)}}}
    return [screen, timer_set]


async def _start(ctx, target, seconds):
    target = target or {}
    if target.get("sn") and target.get("current") is not True:
        # директивы ответа уходят колонке-автору запроса; адресации другой колонке
        # в образцах нет — БЛОКЕР (tasks/079)
        return say("Пока не ставлю таймер на другой колонке — только на этой.")
    spec = say(f"Хорошо. {format_duration(seconds)}. Отсчёт пошёл.")
    spec["directives"] = set_directives(
        ctx, clock_directives.new_clock_id(ctx.state.get("clock_id")), seconds)
    return spec
