"""media.music — музыка из библиотеки state/media/music.

Поиск — по тегам и именам файлов/папок с склонением по основе («майкла джексона»
найдёт «Майкл Джексон», жанр/настроение/эпоху ищет так же). Вся библиотека без
запроса — вперемешку; «вперемешку» и «на повторе» вычленяются из запроса;
«что-нибудь новенькое» — давно не игранное; «моя музыка»/«плейлист» без названия —
избранное («нравится»). Общий шаблон («включи <что-то>» без вида произведения)
играет только явную находку: промах — отказ (None), реплика идёт дальше.
"""
import re

from server_emulator.media import KIND_GENITIVE, KIND_TITLES

# Служебные слова фразы: глагол и вид произведения — остальное запрос.
STOP_WORDS = {"включи", "включай", "поставь", "врубай", "послушать", "давай", "музыку", "песню",
              "песни", "трек", "треки", "композицию", "композиции", "сказку", "сказки",
              "аудиосказку", "аудиосказки", "радио", "пожалуйста", "мне", "слушай"}

# Режимы, которые пользователь говорит в конце запроса.
MODE_WORDS = (("вперемешку", "shuffle"), ("на повторе", "repeat"))
NEW_QUERIES = {"что нибудь новенькое", "новенькое", "новое", "что нибудь новое"}
MINE_QUERIES = {"мою", "моя", "мой", "мою музыку", "моя музыка"}


def _query(text):
    """Что искать: фраза без глагола и вида произведения («включи музыку джаз» → «джаз»)."""
    words = re.split(r"[\s,.;:!?]+", (text or "").strip().lower().replace("ё", "е"))
    return " ".join(word for word in words if word and word not in STOP_WORDS)


def play(ctx, kind):
    """Общий вход сценариев библиотеки: поиск + очередь + директива audio_play.
    Запрос — слот {query} шаблона (та же зачистка служебных слов — филлер может
    попасть в wildcard), без слота — прежний разбор фразы."""
    library = ctx.library
    slots = ctx.slots or {}
    # Отказ только у общего шаблона («включи <что-то>» — есть слоты, но нет вида
    # произведения); путь фраз и шаблон с видом произведения — всегда честный ответ.
    explicit = kind != "music" or not slots or bool(slots.get("media_object"))
    slot_query = slots.get("query")
    query = _query(slot_query) if slot_query else _query(ctx.text)
    mode = None
    for word, name in MODE_WORDS:
        if word in query:
            mode, query = name, _query(query.replace(word, " "))
    if query in NEW_QUERIES:
        mode, query = "new", ""
    user = (ctx.user or {}).get("id") or ctx.sn
    reply = ctx.media.play_library(library, kind, query=query, text="Включаю.",
                                   user=user, mode=mode)
    if reply is not None:
        return reply
    if not explicit:  # голый запрос не угадан — реплика идёт дальше, не отвечаем «нет в библиотеке»
        return None
    if not library.list(kind):
        return {"text": f"{KIND_GENITIVE[kind].capitalize()} в библиотеке нет — добавьте файлы в "
                        f"state/media/{kind} (вкладка «Голос → Медиа»).", "card": False}
    hint = " Скажите «включи музыку» — включу всё вперемешку." if kind == "music" else ""
    return {"text": f"В библиотеке нет {KIND_GENITIVE[kind]} с названием «{query}».{hint}",
            "card": False} if query \
        else {"text": f"{KIND_GENITIVE[kind].capitalize()} в библиотеке нет.", "card": False}


async def handle(ctx):
    slots = ctx.slots or {}
    user = (ctx.user or {}).get("id") or ctx.sn
    raw_query = slots.get("query")
    query = _query(raw_query) if raw_query else _query(ctx.text)
    # избранное: «включи мою музыку», «включи плейлист» без названия
    if query in MINE_QUERIES or (slots.get("media_object") == "плейлист" and not query):
        reply = ctx.media.play_marked(ctx.library, "music", user, "like", text="Включаю.")
        return reply or {"text": "В избранном пока ничего нет — скажите «нравится» во время песни.",
                         "card": False}
    return play(ctx, "music")
