"""device.player.pause — «пауза»: директива audio_play с set_pause (сведение TASK-071).
Ничего не играет — честный ответ."""
async def handle(ctx):
    reply = ctx.media.pause()
    return reply or {"text": "Сейчас ничего не играет.", "card": False}
