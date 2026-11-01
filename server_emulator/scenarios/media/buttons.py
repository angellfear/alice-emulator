"""media.buttons — кнопки экрана плеера колонки (TASK-072): purpose=player_* в колбэке
@@mm_typed_callback. Очередь ведёт эмулятор (TASK-071): next/previous двигают её, pause/play —
audio_play с set_pause; лайки, дизлайк, повтор, шаффл и скорость без аккаунта Яндекса —
тихое подтверждение (колонка показывает своё состояние кнопки).
"""
from server_emulator.sdk import silent


async def handle(ctx):
    purpose = ctx.event.purpose or ""
    player = ctx.media
    if purpose == "player_next":
        return player.step(+1) or silent()
    if purpose == "player_previous":
        return player.step(-1) or silent()
    if purpose == "player_pause":
        return player.pause() or silent()
    if purpose == "player_play":
        return player.resume() or silent()
    return silent()  # player_liked/unliked, player_dislike, player_repeat_*, player_shuffle_*, speed
