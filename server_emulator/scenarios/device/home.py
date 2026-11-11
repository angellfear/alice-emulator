"""device.home — «главный экран»: директива go_home настоящего сервера.

Образец (захват 2026-11-02, Дуо Макс, «Главный экран» на открытом плеере): тихий ответ
без речи — tts_play_placeholder + go_home с пустым Payload, IsLedSilent, без
AnalyticsType. Закрытие плеера и уход на главный экран колонка делает сама.
"""
from server_emulator.media_directives import tts_placeholder

GO_HOME = {"name": "go_home", "type": "client_action", "is_led_silent": True, "payload": {}}


async def handle(ctx):
    return {"text": "", "card": False, "directives": [tts_placeholder(), dict(GO_HOME)]}
