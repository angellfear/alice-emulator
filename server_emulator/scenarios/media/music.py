"""media.music — музыка от поставщика (TASK-099, по образцу 089/PLAN-spotify-music).

Ядро — server_emulator/music.py: треки и очередь отдаёт поставщик (music/*.py
включённых расширений), эмулятор только собирает стек директорий виджета по образцу
захвата 2026-11-02 (draw_animation + show_audio_player_screen + очередь +
sequence_state + audio_play). Своей очереди у ядра нет — правило
tasks/PLAN-station-skills.

Маршруты (решение владельца):
- «Вместе» (hybrid) — сценарий отказывается: Яндекс отвечает сам через релей
  голоса (098); колбэки контента Яндекса уходят наверх до маршрута (091).
- «Напрямую»/«Эмуляция» — поставщик есть: играет он во всех режимах «Интеллекта»;
  поставщика нет: «Простой» — ctx.no_data_source() («Для этого нет источника
  данных.»), «Модель/Агент» — отказ (None), фразу забирает модель.
- Колбэк @@mm_stack_engine_get_next (provide music.stack_next) — neighbor(+1)
  поставщика: следующая очередь + audio_play, как в образце «дальше»; поставщика
  нет или дальше нечего — отказ (умолчание маршрута — тихое подтверждение).
"""
from server_emulator import music
from server_emulator.sdk import say

GET_NEXT = ("music.stack_next", "@@mm_stack_engine_get_next")

# kind поиска по реплике (089: music | tales | podcast | audiobook)
KINDS = (("сказк", "tales"), ("аудиокниг", "audiobook"), ("подкаст", "podcast"))
HEADS = ("включи музыку", "поставь музыку", "запусти музыку", "давай послушаем музыку",
         "давай музыку", "послушать музыку", "включи сказку", "включи подкаст",
         "включи аудиокнигу")


def _request(ctx):
    """(kind, query) из реплики: хвост шаблона (slots.query) или остаток после команды."""
    text = ctx.text.lower().replace("ё", "е")
    kind = next((value for token, value in KINDS if token in text), "music")
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
        return None  # «Вместе»: музыку отдаёт Яндекс (релей голоса, 098)
    provider = ctx.music
    if provider is None:
        return ctx.no_data_source()
    kind, query = _request(ctx)
    found = await music.search(ctx, provider, query, kind)
    if found is None:  # пусто или поставщик не дал треков — честный ответ
        return say("Не нашла подходящего. Попробуйте назвать исполнителя или песню.")
    track = found["tracks"][found["start"]]
    return music.start_reply(found, text=f"Включаю {track['title']}." if query else "Включаю.")
