"""Видео (каталог ссылок) — встроенный поставщик видео (вид video, интерфейс ядра).

Каталог — настройка плагина «videos»: JSON-список записей, правится во вкладке
«Расширения → Видео (каталог ссылок)» без правки кода. Сети нет: ссылки открывает
плеер колонки. Форма ответа — контракт вида: {"items": [...], "note"?} — note
произносится сценарием как честный отказ.

Запись каталога:
  title   — название (обязательно); идёт в карточку и в фразы выбора «название/N/номер N»;
  embed   — ссылка для плеера колонки (обязательно), обычно video_ext-страница хостинга;
  url     — страница видео (необязательно); без неё карточка берёт embed;
  genre   — жанр для фильтра «список комедий» (необязательно);
  host    — домен-хостинг («vk.com»; необязательно — сценарий сам выведет его из embed);
  player  — имя плеера («vk»; необязательно — сценарий сам выведет его из домена);
  duration— длительность, секунды (необязательно);
  year    — год выпуска (необязательно; карточка получает дату 1 января года);
  preview — картинка-превью (необязательно; без неё карточка без Thumbnail).
"""
from __future__ import annotations

import json
from datetime import datetime

PROVIDER = {"id": "builtin.video-links", "title": "Видео (каталог ссылок)"}


async def catalog(ctx):
    """Каталог (интерфейс ядра): настройка «videos» → {"items": [...], "note"?}."""
    try:
        data = json.loads(str(ctx.get("videos") or "[]"))
    except ValueError as exc:
        return {"items": [], "note": f"Каталог видео не разобрать: проверьте JSON в настройках ({exc})."}
    if not isinstance(data, list):
        return {"items": [], "note": "Каталог видео — не список: проверьте JSON в настройках."}
    items = [_normalize(entry) for entry in data]
    items = [item for item in items if item is not None]
    if not items:
        return {"items": [], "note": "В каталоге видео нет ни одной записи с названием и ссылкой плеера."}
    return {"items": items}


def _normalize(entry):
    """Запись настройки → контракт поставщика; без названия или embed запись пропускается.
    host/player — явные значения записи, без вывода из адреса (вывод ведёт сценарий)."""
    if not isinstance(entry, dict):
        return None
    title = str(entry.get("title") or "").strip()
    embed = str(entry.get("embed") or "").strip()
    if not title or not embed:
        return None
    item = {"title": title, "embed": embed}
    for key in ("url", "genre", "host", "player", "preview"):
        value = str(entry.get(key) or "").strip()
        if value:
            item[key] = value
    duration = _seconds(entry.get("duration"))
    if duration:
        item["duration"] = duration
    year = _seconds(entry.get("year"))
    if year and 1890 <= year <= 2999:
        item["released"] = str(int(datetime(year, 1, 1).timestamp()))
    return item


def _seconds(value):
    """Целое-положительное (секунды или год) из числа или цифровой строки."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None
