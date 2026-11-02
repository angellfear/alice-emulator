"""Штатные директивы плеера колонки — сборка payload по образцам (TASK-087).

Элементы директив ({"name", "proto", "fields", "payload", "analytics_type",
"is_led_silent"}) использует сценарий device.player.pause — те же директивы, что
настоящий сервер слал на «стоп»/«подожди» (журнал 2026-10-21…2026-10-26).
tts_play_placeholder — первой директивой, как в образцах; response_compose.finalize
второго плейсхолдера не добавляет. Типы сообщений — из схемы станции
("centaur"), поля — по расшифрованным payload_raw образцов.

Своей очереди/паузы/медиатеки у эмулятора нет (главное правило
tasks/PLAN-station-skills.md): состояние плеера — из запроса колонки
(station_state, capability 46), ответ — директивы образца без речи.
"""
from __future__ import annotations

ANIMATION_PROTO = "NAlice.TAnimationCapability.TDrawAnimationDirective"
HIDE_VIEW_PROTO = "NAlice.TLayeredDivUICapability.TDivUIHideViewDirective"

# каталог S3 со штатными анимациями колонки (образец payload draw_animation_directive)
S3_BUCKET = "https://quasar.s3.yandex.net"
PLAYER_PAUSE_ANIMATION = "animations/player_pause"


def tts_placeholder():
    """tts_play_placeholder — каркас диалогового ответа (образцы: первой директивой,
    payload {"channel": "Dialog"})."""
    return {"name": "tts_play_placeholder", "type": "client_action", "is_led_silent": True,
            "analytics_type": "tts_play_placeholder", "payload": {"channel": "Dialog"}}


def draw_player_pause():
    """draw_animation_directive — анимация паузы с колонки (образцы 2026-10-21 20:35:28,
    2026-10-25 01:00:30: TDrawAnimationDirective{Animations[{S3Directory}]})."""
    return {"name": "draw_animation_directive", "type": "client_action", "is_led_silent": True,
            "analytics_type": "draw_animation_directive", "proto": ANIMATION_PROTO,
            "fields": {"Name": "draw_animation_directive",
                       "Animations": [{"S3Directory": {"Bucket": S3_BUCKET,
                                                       "Path": PLAYER_PAUSE_ANIMATION}}]}}


def pause_items():
    """Останов на играющей колонке (доминирующая форма x20, образцы 2026-10-21 20:35:28,
    2026-10-22 12:00:59, 2026-10-26 16:52:41 и ещё 17; фразы «Стоп», «Подожди»,
    «Ладно, стоп»): скрыть экран плеера, анимация паузы, player_pause{smooth:false},
    очистка очереди. Речи в ответе нет."""
    return [tts_placeholder(),
            {"name": "divui_hide_view", "type": "client_action", "is_led_silent": True,
             "analytics_type": "divui_hide_view", "proto": HIDE_VIEW_PROTO,
             "fields": {"Name": "divui_hide_view"}},
            draw_player_pause(),
            {"name": "player_pause", "type": "client_action", "is_led_silent": True,
             "analytics_type": "general_conversation_player_pause",
             "payload": {"smooth": False}},
            {"name": "clear_queue", "type": "client_action", "is_led_silent": True,
             "analytics_type": "general_conversation_player_pause"}]


def stop_items():
    """Останов на колонке, где играть уже нечего (образцы 2026-10-21 23:25, 2026-10-24
    00:58): без player_pause — останавливать нечего, анимация паузы + очистка очереди
    (analytics «player_pause»). Речи нет."""
    return [tts_placeholder(), draw_player_pause(),
            {"name": "clear_queue", "type": "client_action", "is_led_silent": True,
             "analytics_type": "player_pause"}]
