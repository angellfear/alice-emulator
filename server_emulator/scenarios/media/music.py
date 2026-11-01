"""media.music — «включи музыку [что-то]»: файлы библиотеки state/media/music.

Поиск — остаток фразы без служебных слов по именам файлов и папок (плейлистам);
пустая библиотека — честный ответ с подсказкой, куда складывать файлы.
"""
import re

from server_emulator.media import KIND_GENITIVE, KIND_TITLES

# Служебные слова фразы: глагол и вид произведения — остальное запрос.
STOP_WORDS = {"включи", "включай", "поставь", "врубай", "послушать", "давай", "музыку", "песню",
              "песни", "трек", "треки", "композицию", "композиции", "сказку", "сказки",
              "аудиосказку", "аудиосказки", "радио", "пожалуйста", "мне", "слушай"}


def _query(text):
    """Что искать: фраза без глагола и вида произведения («включи музыку джаз» → «джаз»)."""
    words = re.split(r"[\s,.;:!?]+", (text or "").strip().lower().replace("ё", "е"))
    return " ".join(word for word in words if word and word not in STOP_WORDS)


def play(ctx, kind):
    """Общий вход сценариев библиотеки: поиск + очередь + директива audio_play.
    Запрос — слот {query} шаблона (та же зачистка служебных слов — филлер может
    попасть в wildcard), без слота — прежний разбор фразы. Общий шаблон музыки
    («включи <что-то>» без вида произведения) играет только явную находку: промах
    возвращает None — сценарий отказывается, реплика идёт дальше (другим сценариям,
    «Интеллекту»), а не отвечает «нет в библиотеке» на чужой запрос."""
    library = ctx.library
    slots = ctx.slots or {}
    # Отказ только у общего шаблона («включи <что-то>» — есть слоты, но нет вида
    # произведения); путь фраз и шаблон с видом произведения — всегда честный ответ.
    explicit = kind != "music" or not slots or bool(slots.get("media_object"))
    slot_query = slots.get("query")
    query = _query(slot_query) if slot_query else _query(ctx.text)
    reply = ctx.media.play_library(library, kind, query=query, text="Включаю.")
    if reply is not None:
        return reply
    if not explicit:
        return None
    if not library.list(kind):
        return {"text": f"{KIND_GENITIVE[kind].capitalize()} в библиотеке нет — добавьте файлы в "
                        f"state/media/{kind} (вкладка «Голос → Медиа»).", "card": False}
    return {"text": f"В библиотеке нет {KIND_GENITIVE[kind]} с названием «{query}».", "card": False} \
        if query else {"text": f"{KIND_GENITIVE[kind].capitalize()} в библиотеке нет.", "card": False}


async def handle(ctx):
    return play(ctx, "music")
