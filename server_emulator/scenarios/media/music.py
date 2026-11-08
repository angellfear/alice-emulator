"""media.music — музыка от поставщика.

Ядро — server_emulator/music.py: треки и очередь отдаёт поставщик (music/*.py
включённых расширений), эмулятор только собирает стек директорий виджета по образцу
захвата 2026-11-02 (draw_animation + show_audio_player_screen + очередь +
sequence_state + audio_play). Своей очереди у ядра нет — очередь только от поставщика.

Маршруты (решение владельца):
- «Вместе» (hybrid) — сценарий отказывается: Яндекс отвечает сам через релей
  голоса; колбэки контента Яндекса уходят наверх до маршрута.
- «Напрямую»/«Эмуляция» — поставщик есть: играет он во всех режимах «Интеллекта»;
  поставщика нет: «Простой» — ctx.no_data_source() («Для этого нет источника
  данных.»), «Модель/Агент» — отказ (None), фразу забирает модель. Голый шаблон
  («запусти {query}») без поставщика уступает — следующий кандидат (приложение,
  модель).
- Поставщик вернул отказ с объяснением ({"note": текст} без треков) —
  текст note произносится пользователю вместо общего «не нашла».
- Колбэк @@mm_stack_engine_get_next (provide music.stack_next) — neighbor(+1)
  поставщика: следующая очередь + audio_play, как в образце «дальше»; поставщика
  нет или дальше нечего — отказ (умолчание маршрута — тихое подтверждение).

Не-музыкальные домены голого шаблона отклоняет guard сценария (music.json,
привязка media_other_domain в scenario_engine.bindings).
"""
import re

from server_emulator import music
from server_emulator.sdk import say

# ссылка/URI трека Spotify: шаблонный слот режет URL — сырую реплику разбираем сами
LINK = re.compile(r"(?:open\.spotify\.com/(?:intl-[a-z]{2,5}/)?(?:embed/)?track/|spotify:track:)"
                  r"([0-9A-Za-z]{22})")

GET_NEXT = ("music.stack_next", "@@mm_stack_engine_get_next")

# kind поиска по реплике: сказки/аудиокниги/подкасты или music
KINDS = (("сказк", "tales"), ("аудиокниг", "audiobook"), ("подкаст", "podcast"))
HEADS = ("включи музыку", "поставь музыку", "запусти музыку", "давай послушаем музыку",
         "давай музыку", "послушать музыку", "включи сказку", "включи подкаст",
         "включи аудиокнигу")


def _explicit_music(text):
    """Фраза явно про музыку — головы «включи музыку», сказки/подкасты/аудиокниги,
    маркеры «песню/трек/альбом/исполнителя/группу». Голый шаблон («запусти спотифай»)
    — не явно: без поставщика сценарий уступает следующему кандидату."""
    lowered = str(text or "").lower().replace("ё", "е")
    if any(head in lowered for head in HEADS) or any(token in lowered for token, _ in KINDS):
        return True
    words = set(re.sub(r"[^\w\s]", " ", lowered).split())
    return bool(words & {"песню", "трек", "альбом", "исполнителя", "группу"})


def _request(ctx):
    """(kind, query) из реплики: ссылка/URI — из сырой реплики целиком (шаблонный слот
    режет URL), иначе хвост шаблона (slots.query) или остаток после команды."""
    text = ctx.text.lower().replace("ё", "е")
    kind = next((value for token, value in KINDS if token in text), "music")
    link = LINK.search(ctx.text)
    if link:
        return kind, link.group(0)
    tail = str((ctx.slots or {}).get("query") or "").strip()
    if not tail:
        for head in HEADS:
            if text.startswith(head):
                tail = text[len(head):].strip()
                break
    return kind, tail


async def _stack_next(ctx):
    """Колбэк «включи следующий»: поставщик есть — тройка очередь+состояние+audio_play
    следующего трека (образец «дальше»); иначе отказ — тихое подтверждение."""
    if ctx.route_mode == "hybrid":
        return None  # контент Яндекса ушёл наверх до маршрута (091); свой — тихий ack
    provider = ctx.music
    if provider is None:
        return None
    track = await music.neighbor(ctx, provider, ctx.event.callback or None, None, 1)
    if track is None:  # дальше нечего — колонка остановится сама
        return None
    return music.step_reply(track, provider)


async def handle(ctx):
    event = ctx.event.name or ""
    if event in GET_NEXT:
        return await _stack_next(ctx)
    if ctx.route_mode == "hybrid":
        return None  # «Вместе»: музыку отдаёт Яндекс (релей голоса)
    # совпавшая запись справочника фраз ведёт к поставщику её владельца (не глобально
    # выбранного); прямой запуск (агент, колбэки) — выбранный поставщик
    entry = ctx.phrase_entry or {}
    provider = None
    if entry.get("ext_id"):
        from server_emulator import providers
        provider = providers.for_ext("music", entry.get("ext_id"))
    if provider is None:
        provider = ctx.music
    kind, query = _request(ctx)
    if provider is None:
        if query and not _explicit_music(ctx.text):
            return None  # голый шаблон («запусти {query}») без поставщика — следующий кандидат
        return ctx.no_data_source()

    if "search_result" in entry:  # ступень свободных хвостов: поиск сделал диспетчер
        found = entry["search_result"]
    else:
        found = await music.search(ctx, provider, query, kind)
    if found is None:  # пусто или поставщик не дал треков — честный ответ
        return say("Не нашла подходящего. Попробуйте назвать исполнителя или песню.")
    if not found["tracks"]:  # поставщик объяснил отказ (note) — произносим его
        return say(str(found.get("note")
                       or "Не нашла подходящего. Попробуйте назвать исполнителя или песню."))
    track = found["tracks"][found["start"]]
    return music.start_reply(found, text=f"Включаю {track['title']}." if query else "Включаю.")
