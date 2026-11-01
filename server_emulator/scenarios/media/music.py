"""media.music — «включи музыку [что-то]»: файлы библиотеки state/media/music.

Поиск — остаток фразы без служебных слов по именам файлов и папок (плейлистам);
пустая библиотека — честный ответ с подсказкой, куда складывать файлы.
"""
import re

from server_emulator.media import KIND_GENITIVE, KIND_TITLES

# Служебные слова фразы: глагол и вид произведения — остальное запрос.
STOP_WORDS = {"включи", "включай", "поставь", "врубай", "послушать", "давай", "музыку", "песню",
              "песни", "трек", "треки", "композицию", "композиции", "сказку", "сказки",
              "аудиосказку", "аудиосказки", "радио", "пожалуйста"}


def _query(text):
    """Что искать: фраза без глагола и вида произведения («включи музыку джаз» → «джаз»)."""
    words = re.split(r"[\s,.;:!?]+", (text or "").strip().lower().replace("ё", "е"))
    return " ".join(word for word in words if word and word not in STOP_WORDS)


def play(ctx, kind):
    """Общий вход сценариев библиотеки: поиск + очередь + директива audio_play."""
    library = ctx.library
    query = _query(ctx.text)
    reply = ctx.media.play_library(library, kind, query=query, text="Включаю.")
    if reply is not None:
        return reply
    if not library.list(kind):
        return {"text": f"{KIND_GENITIVE[kind].capitalize()} в библиотеке нет — добавьте файлы в "
                        f"state/media/{kind} (вкладка «Голос → Медиа»).", "card": False}
    return {"text": f"В библиотеке нет {KIND_GENITIVE[kind]} с названием «{query}».", "card": False} \
        if query else {"text": f"{KIND_GENITIVE[kind].capitalize()} в библиотеке нет.", "card": False}


async def handle(ctx):
    return play(ctx, "music")
