"""Штатные директивы таймеров/будильников — сборка payload по образцам (TASK-079).

Одни и те же элементы директив ({"name", "proto", "fields", "is_led_silent"}) используют
сценарии (compose_reply в ответе колонке) и действия runtime (Push). Имена полей и значения
— по образцам журнала настоящего сервера (2026-10-22…2026-10-27); сами типы сообщений —
из схемы станции ("centaur": NAlice.TAlarmCapability.*, NAlice.TClockAppCapability.*).
"""
from __future__ import annotations

import datetime
import uuid

from .scenario_engine.proto_build import encode
from .schema import load_type

INFO_PROTO = "NAlice.TClockAppCapability.TOpenScreenTimerInfoDirective"
LIST_PROTO = "NAlice.TClockAppCapability.TOpenScreenTimerListDirective"
ALARM_LIST_PROTO = "NAlice.TClockAppCapability.TOpenScreenAlarmListDirective"
SET_PROTO = "NAlice.TAlarmCapability.TTimerSetDirective"
CANCEL_PROTO = "NAlice.TAlarmCapability.TTimerCancelDirective"
ADD_PROTO = "NAlice.TAlarmCapability.TAlarmAddDirective"
SNOOZE_MINUTES = 10  # SnoozeTime в обоих образцах alarm_add_directive


def new_clock_id(fixed=None):
    """Идентификатор таймера/будильника как у колонки: 32 hex в четырёх группах (образцы).
    fixed — готовый id (состояние колонки в прогоне эталона), иначе новый случайный."""
    if fixed:
        return str(fixed)
    hex_digits = uuid.uuid4().hex
    return "-".join(hex_digits[i:i + 8] for i in (0, 8, 16, 24))


def timer_set_items(timer_id, seconds, start_ts=None, screen_open=False):
    """Элементы установки таймера по образцу (2026-10-23 14:41:52): экран нового таймера
    (или закрытие открытого списка) + timer_set_directive с Remaining = Duration."""
    if screen_open:
        screen = {"name": "open_screen_timer_list_directive", "proto": LIST_PROTO,
                  "fields": {"ForceAutoClose": True}}
    else:
        screen = {"name": "open_screen_timer_info_directive", "proto": INFO_PROTO,
                  "fields": {"TimerId": timer_id}}
    start_ts = int(datetime.datetime.now().timestamp() if start_ts is None else start_ts)
    return [screen,
            {"name": "timer_set_directive", "proto": SET_PROTO,
             "fields": {"Name": "set_timer_capability_directive",
                        "Timer": {"TimerId": timer_id, "StartTimestamp": start_ts,
                                  "Duration": int(seconds), "Remaining": int(seconds)}}}]


def timer_cancel_items(timer_id):
    """Элементы отмены таймера по образцу (2026-10-25 17:00:33)."""
    return [{"name": "open_screen_timer_list_directive", "proto": LIST_PROTO,
             "fields": {"ForceAutoClose": True}},
            {"name": "timer_cancel_directive", "proto": CANCEL_PROTO,
             "fields": {"Name": "cancel_timer_capability_directive", "TimerId": timer_id}}]


def alarm_add_items(alarm_id, when, hours, minutes, timezone_name="", offset_seconds=0):
    """Элементы добавления будильника по образцу (2026-10-22 05:47:24): экран списка +
    alarm_add_directive (одна дата активации, таймзона колонки, SnoozeTime 10)."""
    local_time = {"Hours": int(hours)}
    if int(minutes):
        local_time["Minutes"] = int(minutes)
    fields = {"AlarmId": alarm_id, "LocalTime": local_time,
              "ActivationDays": [{"Date": {"Year": when.year, "Month": when.month, "Day": when.day}}],
              "SnoozeTime": SNOOZE_MINUTES}
    if timezone_name:
        fields["Timezone"] = {"TimezoneName": timezone_name, "OffsetSeconds": int(offset_seconds)}
    return [{"name": "open_screen_alarm_list_directive", "proto": ALARM_LIST_PROTO,
             "fields": {"ForceAutoClose": True}},
            {"name": "alarm_add_directive", "proto": ADD_PROTO,
             "fields": {"Name": "alarm_add_directive", "Alarm": fields}}]


def push_message(schemas_root, items):
    """PushMessage с перечнем этих элементов: proto+fields → payload_raw по схеме станции."""
    from . import actions
    server_type = load_type(schemas_root, "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
    encoded = [{"name": item["name"], "is_led_silent": True,
                "payload_raw": encode(schemas_root, item["proto"], item.get("fields") or {},
                                      item.get("schema") or "centaur")}
               for item in items]
    return actions.push_directive(server_type, "", items=encoded)
