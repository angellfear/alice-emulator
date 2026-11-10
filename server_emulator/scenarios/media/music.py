"""media.music — музыка от поставщика.

Ядро — server_emulator/music.py: треки и очередь отдаёт поставщик вида music
включённого плагина, эмулятор только собирает стек директорий виджета по образцу
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
  текст note произносится пользователю вместо общего «не нашла». При пустом
  запросе («включи музыку») note — вопрос уточнения: колонка слушает ответ, он
  становится запросом поиска того же поставщика (on_query).
- «включи музыку» без запроса, когда плеер колонки на паузе/остановлен с текущим
  треком поставщика, — продолжение с места (_resume), вопроса нет.
- Колбэк @@mm_stack_engine_get_next (provide music.stack_next) — neighbor(+1)
  поставщика: следующая очередь + audio_play, как в образце «дальше»; поставщика
  нет или дальше нечего — отказ (умолчание маршрута — тихое подтверждение).
- recoverCommit (provide music.recover_commit): audio_play уходил с токеном
  (callbacks.on_started.payload.music) — колонка подтвердила старт трека; токен
  снимается, ответ тихий (следующий трек здесь заменил бы начавшийся).
- Фразы «дальше»/«предыдущий»/«продолжи» — записи next/prev/continue справочника фраз
  поставщика: neighbor(±1) стеком «дальше»; «продолжи» — с места паузы (_resume),
  иначе текущий трек поставщика сначала.
- Кнопки виджета музыки (provide music.touch, TMusicTouchTypedCallback): Continue,
  NextTrack, PrevTrack — те же ответы, что фразы «продолжи», «дальше», «предыдущий»,
  без речи (колбэк просит disable_output_speech); соседа нет — тихий отказ.

Не-музыкальные домены голого шаблона отклоняет guard сценария (music.json,
привязка media_other_domain в scenario_engine.bindings).
"""
import re

from server_emulator import music
from server_emulator.sdk import ask, say

NOT_FOUND = "Не нашла подходящего. Попробуйте назвать исполнителя или песню."
STEPS = {"next": 1, "prev": -1}
PLAYER_INTENTS = ("next", "prev", "continue")  # только при треке в плеере колонки  # записи справочника фраз поставщика → шаг очереди
NOTHING = {"next": "Дальше ничего нет.", "prev": "Это первый трек."}
RESUMABLE = ("paused", "stopped")  # плеер колонки: «включи музыку» продолжает трек
# ссылка/URI трека Spotify: шаблонный слот режет URL — сырую реплику разбираем сами
LINK = re.compile(r"(?:open\.spotify\.com/(?:intl-[a-z]{2,5}/)?(?:embed/)?track/|spotify:track:)"
                  r"([0-9A-Za-z]{22})")

GET_NEXT = ("music.stack_next", "@@mm_stack_engine_get_next")
RECOVER = "music.recover_commit"  # recoverCommit: подтверждение перехода трека
TOUCH = "music.touch"  # кнопка виджета музыки: ctx.event.music_command — continue/next/prev

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
        return None  # контент Яндекса ушёл наверх до маршрута; свой — тихий ack
    provider = ctx.music
    if provider is None:
        return None
    track = await music.neighbor(ctx, provider, ctx.event.callback or None, None, 1)
    if track is None:  # дальше нечего — колонка остановится сама
        return None
    return _with_commit(ctx, provider, track)


async def handle(ctx):
    event = ctx.event.name or ""
    if event in GET_NEXT:
        return await _stack_next(ctx)
    # маршрутизатор даёт provide-имя, voice.py кладёт в ctx wire-имя события (у колонки
    # @@mm_semantic_frame или пустое) — recoverCommit различаем по полю колбэка
    if event == RECOVER or ctx.event.music_type == "recoverCommit":
        return await _recover_commit(ctx)
    if ctx.route_mode == "hybrid":
        return None  # «Вместе»: музыку отдаёт Яндекс (релей голоса)
    if event == TOUCH or ctx.event.music_command:  # кнопка виджета (wire-имя @@mm_typed_callback)
        return await _touch(ctx)
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
    intent = str(entry.get("id") or "").rsplit(".", 1)[-1]  # id записи — «<плагин>.<запись>»
    if intent in PLAYER_INTENTS and not str(ctx.player.get("title") or "").strip():
        return None  # плеер колонки пуст: «дальше»/«продолжи» — не музыке (игры, модель)
    if intent in STEPS:  # «дальше» / «предыдущий» — сосед в очереди поставщика
        track = await music.neighbor(ctx, provider, None, None, STEPS[intent])
        return _with_commit(ctx, provider, track) if track is not None else say(NOTHING[intent])
    if intent == "continue":  # «продолжи»: с места паузы, иначе текущий трек сначала
        resumed = await _resume(ctx, provider)
        if resumed is not None:
            return resumed
        track = await music.neighbor(ctx, provider, None, None, 0)
        return _with_commit(ctx, provider, track) if track is not None else say(NOT_FOUND)

    if not query:
        resumed = await _resume(ctx, provider)
        if resumed is not None:
            return resumed
    if "search_result" in entry:  # ступень свободных хвостов: поиск сделал диспетчер
        found = music.normalize_result(entry["search_result"], provider)
    else:
        found = await music.search(ctx, provider, query, kind)
    if not query and found is not None and not found["tracks"] and "?" in str(found.get("note") or ""):
        # «включи музыку» без запроса, поставщик спрашивает «что включить?» — вопрос
        # уточнения: колонка слушает ответ, ответ — запрос поиска того же поставщика
        ctx.state["kind"] = kind
        ctx.state["ext_id"] = entry.get("ext_id") or ""
        return ask(str(found["note"]), then="on_query")
    return _reply(ctx, found, query, entry.get("ext_id") or "")


async def _touch(ctx):
    """Кнопка виджета: «плей» — продолжение с места паузы, иначе текущий трек сначала;
    «далее»/«назад» — сосед в очереди поставщика. Без речи: нечего играть — отказ,
    умолчание маршрута — тихое подтверждение."""
    provider = ctx.music
    intent = ctx.event.music_command
    if provider is None:
        return None
    if intent in STEPS:
        track = await music.neighbor(ctx, provider, None, None, STEPS[intent])
    else:
        resumed = await _resume(ctx, provider)
        if resumed is not None:
            return resumed
        track = await music.neighbor(ctx, provider, None, None, 0)
    return _with_commit(ctx, provider, track) if track is not None else None


async def _resume(ctx, provider):
    """«включи музыку» на паузе: колонка сообщает плеер paused/stopped с тем же треком,
    что текущий у поставщика (по заголовку) — продолжение с места (образец «продолжи»:
    очередь + состояние + audio_play того же трека с stream.offset_ms). Иначе None."""
    player = ctx.player
    title = str(player.get("title") or "").strip().lower()
    if player.get("state") not in RESUMABLE or not title:
        return None
    track = await music.neighbor(ctx, provider, None, None, 0)
    if track is None or str(track.get("title") or "").strip().lower() != title:
        return None  # на паузе чужой трек (не этого поставщика) — обычный путь
    return _with_commit(ctx, provider, track,
                        offset_ms=int(player.get("played_ms") or 0))


async def on_query(ctx, answer):
    """Ответ на «что включить?»: реплика целиком — запрос поиска."""
    query = str(answer or "").strip()
    provider = None
    if ctx.state.get("ext_id"):
        from server_emulator import providers
        provider = providers.for_ext("music", ctx.state["ext_id"])
    provider = provider or ctx.music
    if provider is None:
        return ctx.no_data_source()
    found = await music.search(ctx, provider, query, ctx.state.get("kind") or "music") if query else None
    return _reply(ctx, found, query, ctx.state.get("ext_id") or "")


def _reply(ctx, found, query, ext_id=""):
    """Ответ по результату поиска: треки — запуск (audio_play с токеном
    recoverCommit), отказ с note — его текст."""
    if found is None:  # пусто или поставщик не дал треков — честный ответ
        return say(NOT_FOUND)
    if not found["tracks"]:  # поставщик объяснил отказ (note) — произносим его
        return say(str(found.get("note") or NOT_FOUND))
    track = found["tracks"][found["start"]]
    token = music.keep_commit(track, ext_id)
    return music.start_reply(found, text=f"Включаю {track['title']}." if query else "Включаю.",
                             commit_token=token)


def _with_commit(ctx, provider, track, offset_ms=0):
    """step_reply «дальше»/«продолжи» с токеном recoverCommit (колонка вернёт его при
    старте трека)."""
    token = music.keep_commit(track)
    return music.step_reply(track, provider, offset_ms=offset_ms, commit_token=token)


async def _recover_commit(ctx):
    """recoverCommit: колонка подтвердила старт трека (присылает его при старте каждого
    трека) — токен снимается, ответ тихий. Следующий трек здесь не отдаётся: audio_play
    в ответ заменил бы только что начавшийся трек."""
    music.take_commit(ctx.event.music_token)
    return None
