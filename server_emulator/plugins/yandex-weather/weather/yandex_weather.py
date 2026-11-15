"""Яндекс Погода — встроенный поставщик погоды (вид weather, интерфейс ядра).

Источник — API Яндекс Погоды v2 (GET api.weather.yandex.ru/v2/forecast, заголовок
X-Yandex-Weather-Key — ключ из настроек плагина). Ответ приводится к форме Open-Meteo,
которую читают сценарий informers.weather и виджет погоды: current (fact), hourly
(24 часа из forecasts[].hours), daily (два дня по частям суток parts: мин/макс,
условие дня, вероятность осадков, ветер, порывы, направление).

«в <город>»: location {"city": …} — координаты даёт геокодер Open-Meteo
(geocoding-api.open-meteo.com/v1/search); у v2 своего геокодера нет.

Сеть — стандартная библиотека (urllib.request в asyncio.to_thread); URL — константы
модуля (FORECAST_URL/GEOCODE_URL), тесты подменяют их на локальный HTTP.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import urllib.request
from urllib.parse import urlencode

PROVIDER = {"id": "builtin.yandex-weather", "title": "Яндекс Погода"}

FORECAST_URL = "https://api.weather.yandex.ru/v2/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
TIMEOUT_S = 8

# condition Яндекс Погоды → код WMO (как у Open-Meteo)
CONDITION_WMO = {
    "clear": 0, "partly-cloudy": 2, "cloudy": 3, "overcast": 3,
    "drizzle": 51, "light-rain": 61, "rain": 63, "moderate-rain": 63,
    "heavy-rain": 65, "continuous-heavy-rain": 65, "showers": 81,
    "wet-snow": 67, "light-snow": 71, "snow": 73, "snow-showers": 85,
    "hail": 96, "thunderstorm": 95, "thunderstorm-with-rain": 95,
    "thunderstorm-with-hail": 96,
}
# wind_dir → градусы (откуда дует), «c» — штиль
WIND_DEG = {"n": 0, "ne": 45, "e": 90, "se": 135, "s": 180, "sw": 225, "w": 270, "nw": 315, "c": 0}
PARTS = ("night", "morning", "day", "evening")


def _wmo(condition):
    return CONDITION_WMO.get(str(condition or ""), 3)


def _get_json(url, query, headers=None):
    request = urllib.request.Request(url + "?" + urlencode(query),
                                     headers={"Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _minutes(clock, default):
    try:
        hours, minutes = str(clock).split(":")[:2]
        return int(hours) * 60 + int(minutes)
    except (ValueError, TypeError):
        return default


def _local_now(data):
    offset = ((data.get("info") or {}).get("tzinfo") or {}).get("offset") or 0
    zone = datetime.timezone(datetime.timedelta(seconds=int(offset)))
    return datetime.datetime.now(zone).replace(tzinfo=None)


def yandex_to_open_meteo(data, now=None):
    """Ответ v2 → форма Open-Meteo. now — локальное время места (datetime без tz),
    по умолчанию — по смещению info.tzinfo.offset."""
    fact = data.get("fact") or {}
    forecasts = data.get("forecasts") or []
    if not forecasts:
        raise ValueError("Яндекс Погода: пустой прогноз")
    start = (now or _local_now(data)).replace(minute=0, second=0, microsecond=0)
    hourly = {"time": [], "temperature_2m": [], "weather_code": [], "is_day": [],
              "precipitation_probability": []}
    for day in forecasts:
        sunrise = _minutes(day.get("sunrise"), 7 * 60)
        sunset = _minutes(day.get("sunset"), 19 * 60)
        for hour in day.get("hours") or []:
            moment = datetime.datetime.fromisoformat(str(day["date"])).replace(hour=int(hour.get("hour") or 0))
            if moment < start or len(hourly["time"]) >= 24:
                continue
            hourly["time"].append(moment.strftime("%Y-%m-%dT%H:%M"))
            hourly["temperature_2m"].append(hour.get("temp"))
            hourly["weather_code"].append(_wmo(hour.get("condition")))
            hourly["is_day"].append(1 if sunrise <= moment.hour * 60 < sunset else 0)
            hourly["precipitation_probability"].append(hour.get("prec_prob") or 0)
    daily = {"temperature_2m_max": [], "temperature_2m_min": [], "precipitation_probability_max": [],
             "weather_code": [], "wind_speed_10m_max": [], "wind_gusts_10m_max": [],
             "wind_direction_10m_dominant": []}
    for day in forecasts[:2]:
        named = day.get("parts") or {}
        parts = [named[name] for name in PARTS if named.get(name)]
        main = named.get("day") or (parts[0] if parts else {})
        highs = [p.get("temp_max", p.get("temp_avg")) for p in parts]
        lows = [p.get("temp_min", p.get("temp_avg")) for p in parts]
        highs = [t for t in highs if t is not None]
        lows = [t for t in lows if t is not None]
        daily["temperature_2m_max"].append(max(highs) if highs else None)
        daily["temperature_2m_min"].append(min(lows) if lows else None)
        daily["precipitation_probability_max"].append(max([p.get("prec_prob") or 0 for p in parts] or [0]))
        daily["weather_code"].append(_wmo(main.get("condition")))
        daily["wind_speed_10m_max"].append(max([p.get("wind_speed") or 0 for p in parts] or [0]))
        daily["wind_gusts_10m_max"].append(max([p.get("wind_gust") or 0 for p in parts] or [0]))
        daily["wind_direction_10m_dominant"].append(WIND_DEG.get(str(main.get("wind_dir") or ""), 0))
    return {
        "source": "yandex-weather",
        "current": {"temperature_2m": fact.get("temp"),
                    "apparent_temperature": fact.get("feels_like", fact.get("temp")),
                    "weather_code": _wmo(fact.get("condition")),
                    "is_day": 0 if fact.get("daytime") == "n" else 1},
        "hourly": hourly,
        "daily": daily,
    }


async def geocode(name):
    """Город → {"latitude", "longitude", "name"} (геокодер Open-Meteo). Не нашёл — исключение."""
    query = {"name": str(name or ""), "count": "1", "language": "ru", "format": "json"}
    data = await asyncio.to_thread(_get_json, GEOCODE_URL, query)
    results = data.get("results") or []
    if not results:
        raise ValueError(f"геокодирование: {name!r} не найден")
    found = results[0]
    return {"latitude": found.get("latitude"), "longitude": found.get("longitude"),
            "name": found.get("name") or name}


async def forecast(ctx, location):
    """Прогноз по координатам или городу (интерфейс ядра), форма Open-Meteo. Нет ключа
    или сбой источника — исключение (датасет откатится на старую запись или честный отказ)."""
    key = str(ctx.get("api_key") or "").strip() if ctx is not None else ""
    if not key:
        raise ValueError("Яндекс Погода: не задан ключ API в настройках плагина")
    location = dict(location or {})
    city = None
    if location.get("city") and location.get("latitude") is None:
        location = await geocode(location["city"])
        city = location["name"]
    query = {"lat": str(location.get("latitude") or ""), "lon": str(location.get("longitude") or ""),
             "lang": "ru_RU", "limit": "2", "hours": "true", "extra": "false"}
    data = await asyncio.to_thread(_get_json, FORECAST_URL, query, {"X-Yandex-Weather-Key": key})
    result = yandex_to_open_meteo(data)
    if city:
        result["city"] = city
    return result
