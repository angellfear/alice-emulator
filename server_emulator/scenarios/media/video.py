"""media.video — выдача каталога видео и запуск по номеру.

Каталог — поставщик вида video (встроенный плагин «Видео (каталог ссылок)», список
правится в настройках плагина); ядру остаётся собрать ответ по образцу записи:
open_search с веб-карточками SearchVideoItem, у каждой — два одинаковых voice-control
с сущностью selected_video_title («название / N / номер N»), плюс tts_play_placeholder;
выбор по номеру — play_web_video_directive и «Запуску.» (в записи led-директивы обе
с IsLedSilent, open_search — без AnalyticsType, play — с названием видео).

Выдача запоминается в данных сценария до новой выдачи — по ней выбирается номер.
«ещё»/«назад» листает сама колонка (сценарий device.screen.navigation), о закрытии
экрана эмулятору не сообщают — память после закрытия остаётся до новой выдачи.
Нет выдачи — номер не наш (отказ), фразу забирает следующий кандидат.

Фильтр выдачи — по последнему слову запроса (нормальная форма слова, wordforms.lemma):
совпадение с жанром или словом названия; общие слова (фильм, кино, видео, ролик,
медиа, подборка) — весь каталог; совпадений меньше трёх — честный отказ (карточек в
образце много, выдача рассчитана на выбор по номеру).

Маршрут «Вместе» (hybrid) — отказ: Яндекс отвечает сам, как у музыки.
"""
from server_emulator import providers
providers.require("video", ("catalog",))
from server_emulator.media_directives import tts_placeholder
from server_emulator.scenario_engine import wordforms
from server_emulator.sdk import say
from urllib.parse import urlparse

MAX_CARDS = 10   # потолок выдачи
MIN_CARDS = 3    # меньше — выбор по номеру не работает, честный отказ
UMBRELLA = ("фильм", "кино", "видео", "ролик", "медиа", "подборка")
SEARCH_PROTO = "NAlice.TSearchCapability.TOpenSearchDirective"
PLAY_PROTO = "NAlice.TWebPlayerCapability.TPlayWebVideoDirective"


async def handle(ctx):
    if ctx.route_mode == "hybrid":
        return None
    entry = ctx.phrase_entry or {}
    number = ctx.slots.get("num")
    if entry.get("id") == "select" or number is not None:
        if number is None:
            return None
        return _select(ctx, int(number))
    return await _search(ctx, str(ctx.slots.get("query") or "").strip())


def _select(ctx, number):
    """Выбор по номеру из последней выдачи; выдачи нет — отказ."""
    memory = ctx.data.get("search")
    if not isinstance(memory, dict) or not isinstance(memory.get("items"), list):
        return None
    items = memory["items"]
    if not 1 <= number <= len(items):
        return say(f"В списке всего {len(items)}.")
    play = {"name": "play_web_video_directive", "type": "client_action", "is_led_silent": True,
            "analytics_type": items[number - 1]["title"], "proto": PLAY_PROTO,
            "fields": _play_fields(items[number - 1])}
    return {"text": "Запускаю.", "card": False, "directives": [play, tts_placeholder()]}


async def _search(ctx, query):
    """Выдача open_search из каталога поставщика."""
    provider = ctx.provider("video")
    if provider is None:
        return ctx.no_data_source()
    try:
        result = await providers.invoke(ctx, provider, provider["catalog"])
    except Exception:  # noqa: BLE001 — сбой поставщика не роняет сценарий
        result = None
    if not isinstance(result, dict):
        return say("Каталог видео недоступен.")
    items = result.get("items") or []
    note = str(result.get("note") or "").strip()
    if not items:
        return say(note or "Каталог видео пуст.")
    matched = _filter(items, query)
    if not matched:
        return say(f"По запросу «{query}» в каталоге видео ничего нет.")
    if len(matched) < MIN_CARDS:
        return say(f"Нашлось всего {len(matched)} — покажу список, когда наберётся хотя бы {MIN_CARDS}.")
    matched = matched[:MAX_CARDS]
    ctx.data.set("search", {"query": query, "items": matched})  # память выдачи
    return {"text": "", "card": False, "directives": [_open_search(query, matched), tts_placeholder()]}


def _filter(items, query):
    """Совпадения по последнему слову запроса; общее слово — весь каталог."""
    words = query.lower().replace("ё", "е").split()
    if not words:
        return list(items)
    head = wordforms.lemma(words[-1])
    if head in {wordforms.lemma(word) for word in UMBRELLA}:
        return list(items)
    return [item for item in items if head in _tags(item)]


def _tags(item):
    """Нормальные формы слов жанра и названия."""
    text = f"{item.get('genre') or ''} {item.get('title') or ''}".lower().replace("ё", "е")
    return {wordforms.lemma(word) for word in text.split() if word}


def _open_search(query, items):
    cards = [_card(number, item) for number, item in enumerate(items, 1)]
    return {"name": "open_search", "type": "client_action", "is_led_silent": True,
            "proto": SEARCH_PROTO,
            "fields": {"SearchQuery": query,
                       "SearchResultData": {"SearchText": query,
                                            "Galleries": [{"BasicCarousel": {
                                                "Title": "Результаты поиска по видео",
                                                "Items": cards}}]}}}


def _host(item):
    """Домен хостинга: явный host записи, иначе hostname адреса embed («example.com»)."""
    host = str(item.get("host") or "").strip()
    if host:
        return host
    try:
        return (urlparse(item["embed"]).hostname or "").lower()
    except (KeyError, ValueError):  # embed нет (не бывает — запись проверена) или не адрес
        return ""


def _player(item):
    """Плеер: явный player записи, иначе первая метка домена («example.com» → «example»)."""
    player = str(item.get("player") or "").strip()
    if player:
        return player
    return _host(item).split(".")[0]


def _card(number, item):
    """Карточка выдачи: SearchVideoItem + два одинаковых voice-control, как в образце."""
    host, player = _host(item), _player(item)
    video = {"Id": item.get("url") or item["embed"], "ContentType": "video",
             "Title": item["title"], "EmbedUri": item["embed"]}
    if player:
        video["PlayerId"] = player
    if host:
        video["Hosting"] = host
    if item.get("preview"):
        video["Thumbnail"] = {"BaseUrl": item["preview"]}
    if item.get("released"):
        video["ReleaseDate"] = str(item["released"])
    duration = _seconds(item.get("duration"))
    if duration:
        video["Duration"] = str(duration)
    return {"Number": number, "VoiceControls": [_voice_control(number, item, duration)] * 2,
            "SearchVideoItem": video}


def _voice_control(number, item, duration):
    """Сущность selected_video_title: фразы «название / N / номер N»."""
    meta = {"Type": "video", "Position": number, "Title": item["title"]}
    if duration:
        meta["Duration"] = duration
    player = _player(item)
    if player:
        meta["ProviderName"] = player
    return {"ActivationConditions": [{"VisibleCondition": {"VisibleShare": 0.01}}],
            "Actions": [{}],
            "ExternalEntitiesDescriptions": [{"Name": "selected_video_title",
                                              "Items": [{"Value": {"StringValue": f"movie_{number}"},
                                                         "Phrases": [{"Phrase": item["title"]},
                                                                     {"Phrase": str(number)},
                                                                     {"Phrase": f"номер {number}"}],
                                                         "VideoMeta": meta}]}]}


def _play_fields(item):
    """Поля запуска плеера колонки (в образце: EmbedUri, HostDomain, Title, PlaybackTiming,
    SearchUrl, SandboxName, Name; Doc2docUrl и VideoId внешнего поиска — нет)."""
    fields = {"EmbedContent": {"EmbedUri": item["embed"]},
              "Title": item["title"], "Name": item["title"]}
    host, player = _host(item), _player(item)
    if host:
        fields["HostDomain"] = host
    if player:
        fields["SandboxName"] = player
    if item.get("url"):
        fields["SearchUrl"] = item["url"]
    duration = _seconds(item.get("duration"))
    if duration:
        fields["PlaybackTiming"] = {"Duration": duration}
    return fields


def _seconds(value):
    """Положительное целое (секунды) из числа или цифровой строки."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None
