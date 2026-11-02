"""device.player.pause — «пауза/стоп»: директивы настоящего сервера (TASK-087).

Образцы журнала (23 ответа, 2026-10-21…2026-10-27: «Стоп», «Подожди», «Ладно, стоп»,
«Выключись» — и голосом, и текстом, в том числе на Idle-плеере): тихий ответ без речи
divui_hide_view + draw_animation(animations/player_pause) + player_pause{smooth:false}
+ clear_queue (tts_play_placeholder — первой директивой). Состояние плеера колонки
видено в запросе (ctx.player), но форма от него не зависит — сервер всегда слал одну.
Своей очереди у эмулятора нет: «что играет» и переживание паузы — дело колонки.
"""
from server_emulator.media_directives import pause_items


async def handle(ctx):
    return {"text": "", "card": False, "directives": pause_items()}
