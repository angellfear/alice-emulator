"""media.tales — «включи сказку (про …)»: файлы библиотеки state/media/tales."""
from server_emulator.scenarios.media.music import play


async def handle(ctx):
    return play(ctx, "tales")
