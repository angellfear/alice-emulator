"""media.playback — поставщик колбэков плеера (TASK-071): трек закончился или колонка
просит следующий (@@mm_stack_engine_get_next, music_thin_client_on_stopped) — включает
следующий трек очереди; очередь пуста — тихий ответ без реплики."""
from server_emulator.sdk import silent


async def handle(ctx):
    reply = ctx.media.step(+1)
    return reply or silent()
