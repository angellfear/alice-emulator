"""Выключить сигнал таймера/будильника («хватит», TASK-079).

Звенит ли колонка — видно из её состояния в запросе: будильник CurrentlyPlaying=true
(образец 2026-10-27 19:05:07) или таймер CurrentlyPlaying/истёкший активный. Не звенит —
отказ (None): реплику забирает прежний исполнитель (device.player.pause — пауза музыки).

Образцы ответов сервера:
· будильник (2026-10-27 19:05:13): alarm_stop_directive + close_screen_alarm_active_directive
  (между ними сервер слал alarm_add_directive — перепостановку snooze; время и политика
  snooze по образцам не выводятся однозначно → БЛОКЕР, не отправляем);
· таймер (2026-10-27 23:15:03): timer_stop_directive{TimerId} +
  close_screen_timer_active_directive, ответ без речи (сервер остановил молча).
"""
from server_emulator import station_state
from server_emulator.sdk import say

ALARM_STOP_PROTO = "NAlice.TAlarmCapability.TAlarmStopDirective"
ALARM_CLOSE_PROTO = "NAlice.TClockAppCapability.TCloseScreenAlarmActiveDirective"
TIMER_STOP_PROTO = "NAlice.TAlarmCapability.TTimerStopDirective"
TIMER_CLOSE_PROTO = "NAlice.TClockAppCapability.TCloseScreenTimerActiveDirective"


async def handle(ctx):
    now = int(ctx.now.timestamp())
    state = {"timers": ctx.timers, "alarms": ctx.alarms}
    ring = station_state.ringing(state, now)
    if ring is None:
        return None  # у колонки не звенит — «хватит» остаётся медиа-паузе
    kind, item_id = ring
    if kind == "alarm":
        spec = say("Будильник выключил.")
        spec["card"] = False
        spec["directives"] = [
            {"name": "alarm_stop_directive", "type": "client_action", "is_led_silent": True,
             "proto": ALARM_STOP_PROTO,
             "fields": {"Name": "stop_alarm_capability_directive", "StopMedia": True}},
            {"name": "close_screen_alarm_active_directive", "type": "client_action",
             "is_led_silent": True, "proto": ALARM_CLOSE_PROTO, "fields": {}},
        ]
        return spec
    spec = {"text": "", "card": False, "directives": [
        # в образце 23:15:03 tts_play_placeholder стоит первым кадром при пустой речи —
        # добавляем его вручную (finalize дописывает tts только когда есть озвучка)
        {"name": "tts_play_placeholder", "type": "client_action", "is_led_silent": True,
         "payload": {"channel": "Dialog"}},
        {"name": "timer_stop_directive", "type": "client_action", "is_led_silent": True,
         "proto": TIMER_STOP_PROTO, "fields": {"TimerId": item_id}},
        {"name": "close_screen_timer_active_directive", "type": "client_action",
         "is_led_silent": True, "proto": TIMER_CLOSE_PROTO, "fields": {}},
    ]}
    return spec
