"""YouTube — встроенный поставщик видео (вид video, интерфейс ядра).

search(ctx, query) — поиск роликов через yt-dlp («ytsearchN:», плоская выдача без
загрузки страниц роликов, ключ API не нужен); запись выдачи — контракт вида:
title, embed (youtube.com/embed/<id> — открывает плеер колонки), url, host, player,
duration, preview. catalog(ctx) — каталога у YouTube нет: честная подсказка искать.

yt-dlp синхронный — вызов в asyncio.to_thread с потолком SEARCH_TIMEOUT.
"""
from __future__ import annotations

import asyncio

PROVIDER = {"id": "builtin.youtube", "title": "YouTube"}

SEARCH_TIMEOUT = 15  # секунд на поиск целиком
MAX_RESULTS = 20
# превью — постоянный адрес без параметров: адреса hq720 из выдачи (с подписью sqp/rs)
# колонка не показывает
PREVIEW = "https://i.ytimg.com/vi/{id}/hqdefault.jpg"


async def catalog(ctx):
    """Каталога нет — выдача только по запросу."""
    return {"items": [], "note": "Скажите, что найти на ютубе: например, «найди на ютубе мультфильмы»."}


async def search(ctx, query):
    """Выдача YouTube по запросу → {"items": [...], "note"?}."""
    count = _count(ctx.get("results") if ctx is not None else None)
    entries = await asyncio.wait_for(asyncio.to_thread(_search, query, count), SEARCH_TIMEOUT)
    items = [item for item in (_item(entry) for entry in entries) if item]
    if not items:
        return {"items": [], "note": f"На ютубе по запросу «{query}» ничего не нашлось."}
    return {"items": items}


def _count(value):
    try:
        return max(1, min(MAX_RESULTS, int(value)))
    except (TypeError, ValueError):
        return 10


def _search(query, count):
    import yt_dlp
    options = {"quiet": True, "no_warnings": True, "skip_download": True,
               "extract_flat": "in_playlist", "noprogress": True}
    with yt_dlp.YoutubeDL(options) as ydl:
        data = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
    return (data or {}).get("entries") or []


def _item(entry):
    """Запись yt-dlp → запись вида video; без id или названия (каналы, плейлисты) — None."""
    video_id = str(entry.get("id") or "").strip()
    title = str(entry.get("title") or "").strip()
    if not video_id or not title or entry.get("ie_key") not in (None, "Youtube"):
        return None
    item = {"title": title, "embed": f"https://www.youtube.com/embed/{video_id}?autoplay=1",
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "host": "youtube.com", "player": "youtube"}
    duration = entry.get("duration")
    if isinstance(duration, (int, float)) and duration > 0:
        item["duration"] = int(duration)
    item["preview"] = PREVIEW.format(id=video_id)
    return item
