"""device.screen.navigation — «ещё»/«назад»: листание экранов колонки.

Образец (запись .85 2026-11-12 14:26:49, 14:27:19): тихий ответ без речи —
tts_play_placeholder + go_forward / go_backward с пустым Payload, IsLedSilent,
без AnalyticsType (тот же вид, что go_home в device.home). Листает экран сама
колонка. Триггеры — только точные реплики: «перемотай назад» — плееру, «дальше»
без контекста экрана у настоящего сервера — ответ модели, не навигация.
"""
from server_emulator.media_directives import tts_placeholder

GO = {"еще": "go_forward", "назад": "go_backward"}


async def handle(ctx):
    name = GO.get(ctx.text.strip().lower().replace("ё", "е"))
    if name is None:
        return None
    return {"text": "", "card": False,
            "directives": [tts_placeholder(),
                           {"name": name, "type": "client_action", "is_led_silent": True,
                            "payload": {}}]}
