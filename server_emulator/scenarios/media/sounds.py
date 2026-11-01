"""media.sounds — звуки природы: файлы библиотеки state/media/sounds.

Название звука — слот {nature_sound} шаблона (перечень из справки); пустая папка
или промах — честный ответ, куда складывать файлы.
"""
from server_emulator.media import KIND_GENITIVE


async def handle(ctx):
    slots = ctx.slots or {}
    query = str(slots.get("nature_sound") or "").strip() or (ctx.text or "").lower()
    library = ctx.library
    reply = ctx.media.play_library(library, "sounds", query=query, text="Включаю.",
                                   user=(ctx.user or {}).get("id") or ctx.sn)
    if reply is not None:
        return reply
    if not library.list("sounds"):
        return {"text": "Звуков природы в библиотеке нет — добавьте файлы в state/media/sounds "
                        "(вкладка «Голос → Медиа»).", "card": False}
    return {"text": f"В библиотеке нет {KIND_GENITIVE['sounds']} с названием «{query}».", "card": False}
