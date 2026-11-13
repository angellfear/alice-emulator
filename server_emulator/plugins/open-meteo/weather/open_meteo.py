"""Open-Meteo — встроенный поставщик погоды (вид weather, интерфейс ядра).

Что отдаёт: прогноз по координатам в форме Open-Meteo — current (температура,
ощущается, код погоды, день/ночь), hourly (лента на сутки с кодами и вероятностью
осадок), daily (два дня: мин/макс, код погоды, вероятность осадок, ветер — скорость,
порывы, направление) — эту форму читают сценарий informers.weather (карточка и речь
как у настоящего сервера: «Завтра …, слабый южный ветер 2 м/с, порывы до 6 м/с.») и
виджет погоды; свежесть и кеш обеспечивает датасет weather (записи в БД), поставщик
возвращает только свежие данные.

«в <город>»: location с {"city": …} вместо координат — геокодирование через
api.open-meteo.com/v1/geocoding (тот же хост, разрешён расширением); координаты и
таймзона города подставляются в запрос. Не нашёл город — исключение (датасет
ответит честным отказом).

Источник: api.open-meteo.com/v1/forecast (URL — настройка плагина, можно подставить
Open-Meteo-совместимый). Недоступен — резерв api.met.no (locationforecast 2.0
complete), ответ приводится к форме Open-Meteo (met_no_to_open_meteo), чтобы сценарий
не зависел от источника; города met.no не геокодирует.

Сеть — стандартная библиотека (urllib.request в asyncio.to_thread); URL — константы
модуля (OPEN_METEO_URL/MET_NO_URL/GEOCODE_URL): тесты подменяют их на локальный
подменный HTTP, настоящие сервисы из автотестов не вызываются. met.no требует
User-Agent с контактом.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import urllib.request

PROVIDER = {"id": "builtin.open-meteo", "title": "Open-Meteo"}

DEFAULT_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_URL = DEFAULT_URL  # константа для подмены в тестах; настройка url сильнее
GEOCODE_URL = "https://api.open-meteo.com/v1/geocoding"
PRIMARY_TIMEOUT = 4  # секунд на Open-Meteo целиком
HEDGE_DELAY = 1.0    # Open-Meteo молчит дольше — резерв met.no стартует параллельно, побеждает первый ответ
TIMEOUT_S = 10
MET_NO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
MET_NO_AGENT = "alice-station-emulator/1.0 (local)"  # met.no требует User-Agent с контактом

# symbol_code met.no (без суффикса _day/_night) → код WMO, как у Open-Meteo
MET_NO_WMO = {
    "clearsky": 0, "fair": 1, "partlycloudy": 2, "cloudy": 3, "fog": 45,
    "lightrain": 61, "rain": 63, "heavyrain": 65,
    "lightrainshowers": 80, "rainshowers": 81, "heavyrainshowers": 82,
    "lightsleet": 66, "sleet": 67, "heavysleet": 67,
    "lightsleetshowers": 66, "sleetshowers": 67, "heavysleetshowers": 67,
    "lightsnow": 71, "snow": 73, "heavysnow": 75,
    "lightsnowshowers": 85, "snowshowers": 85, "heavysnowshowers": 86,
}


def _wmo(symbol):
    base = str(symbol or "").split("_")[0]
    if "thunder" in base:
        return 95
    return MET_NO_WMO.get(base, 3)


def _is_day(symbol, local):
    if str(symbol).endswith("_night"):
        return 0
    if str(symbol).endswith(("_day", "_polartwilight")):
        return 1
    return 1 if 7 <= local.hour < 19 else 0


def met_no_to_open_meteo(data, timezone=None, hours=48):
    """Ответ met.no (complete) → форма Open-Meteo, которую читают сценарии; дневные
    поля — по суткам (два дня: температура, код погоды по частому коду суток, осадки,
    ветер/порывы/направление)."""
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(timezone) if timezone else datetime.timezone.utc
    except Exception:
        tz = datetime.timezone.utc
    series = (data.get("properties") or {}).get("timeseries") or []
    rows = []
    for item in series[:hours]:
        moment = datetime.datetime.fromisoformat(item["time"].replace("Z", "+00:00")).astimezone(tz)
        block = item.get("data") or {}
        details = (block.get("instant") or {}).get("details") or {}
        nearest = block.get("next_1_hours") or block.get("next_6_hours") or {}
        symbol = (nearest.get("summary") or {}).get("symbol_code") or ""
        precip = nearest.get("details") or {}
        probability = precip.get("probability_of_precipitation")
        if probability is None:
            probability = 70 if (precip.get("precipitation_amount") or 0) > 0 else 0
        rows.append({"time": moment.strftime("%Y-%m-%dT%H:%M"), "temp": details.get("air_temperature"),
                     "feels": details.get("apparent_air_temperature", details.get("air_temperature")),
                     "code": _wmo(symbol), "is_day": _is_day(symbol, moment), "precip": probability,
                     "wind": details.get("wind_speed"),
                     "gust": details.get("wind_speed_of_gust", details.get("wind_speed")),
                     "dir": details.get("wind_from_direction")})
    if not rows:
        raise ValueError("met.no: пустой прогноз")
    now = rows[0]

    days = []
    for row in rows:
        date = row["time"].split("T", 1)[0]
        if not days or days[-1]["date"] != date:
            days.append({"date": date, "rows": []})
        days[-1]["rows"].append(row)
    temps_max, temps_min, precip_max, codes, wind_max, gust_max, dir_dominant = [], [], [], [], [], [], []
    for day in days[:2]:
        day_rows = day["rows"]
        day_temps = [r["temp"] for r in day_rows if r["temp"] is not None]
        temps_max.append(max(day_temps) if day_temps else None)
        temps_min.append(min(day_temps) if day_temps else None)
        precip_max.append(max((r["precip"] or 0) for r in day_rows))
        counts = {}
        for r in day_rows:
            counts[r["code"]] = counts.get(r["code"], 0) + 1
        codes.append(max(counts, key=counts.get))
        winds = [r["wind"] for r in day_rows if r["wind"] is not None]
        gusts = [r["gust"] for r in day_rows if r["gust"] is not None]
        wind_max.append(max(winds) if winds else None)
        gust_max.append(max(gusts) if gusts else None)
        dirs = [r["dir"] for r in day_rows if r["dir"] is not None]
        dir_dominant.append(dirs[0] if dirs else None)

    return {
        "source": "met.no",
        "current": {"temperature_2m": now["temp"], "apparent_temperature": now["feels"],
                    "weather_code": now["code"], "is_day": now["is_day"]},
        "hourly": {"time": [r["time"] for r in rows], "temperature_2m": [r["temp"] for r in rows],
                   "weather_code": [r["code"] for r in rows], "is_day": [r["is_day"] for r in rows],
                   "precipitation_probability": [r["precip"] for r in rows]},
        "daily": {"temperature_2m_max": temps_max, "temperature_2m_min": temps_min,
                  "precipitation_probability_max": precip_max, "weather_code": codes,
                  "wind_speed_10m_max": wind_max, "wind_gusts_10m_max": gust_max,
                  "wind_direction_10m_dominant": dir_dominant},
    }


def _query(location):
    """Координаты → query Open-Meteo: текущая, сутки почасово (с вероятностью осадок),
    два дня (код погоды, ветер и порывы — для речи и карточки «завтра»)."""
    return {"latitude": str(location.get("latitude") or ""),
            "longitude": str(location.get("longitude") or ""),
            "current": "temperature_2m,apparent_temperature,weather_code,is_day",
            "hourly": "temperature_2m,weather_code,is_day,precipitation_probability",
            "daily": ("temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                      "weather_code,wind_speed_10m_max,wind_gusts_10m_max,"
                      "wind_direction_10m_dominant"),
            "forecast_hours": "24", "forecast_days": "2", "timezone": "auto"}


def _http_json(url, query):
    """Синхронный GET JSON (urllib) — вызывается в отдельном потоке."""
    from urllib.parse import urlencode
    request = urllib.request.Request(url + "?" + urlencode(query),
                                     headers={"User-Agent": MET_NO_AGENT,
                                              "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


async def _get_json(url, query):
    return await asyncio.to_thread(_http_json, url, query)


async def geocode(ctx, name):
    """Город → {"latitude", "longitude", "timezone", "name"} (open-meteo geocoding).
    Адрес — настройка geocode_url; иначе сосед прогнозного url (тот же хост —
    подменный сервер тестов), иначе GEOCODE_URL. Не нашёл — исключение."""
    url = str(ctx.get("geocode_url") or "").strip() if ctx is not None else ""
    if not url and ctx is not None:
        forecast_url = str(ctx.get("url") or "").strip()
        url = forecast_url.rsplit("/v1/", 1)[0] + "/v1/geocoding" if "/v1/" in forecast_url else ""
    query = {"name": str(name or ""), "count": "1", "language": "ru", "format": "json"}
    # Без явного таймаута здесь шаг ждал бы до TIMEOUT_S (10 с, сокетный таймаут
    # urlopen) при медленном геокодере — тот же потолок, что у запроса прогноза,
    # а не отдельный повторный простой.
    data = await asyncio.wait_for(_get_json(url or GEOCODE_URL, query), PRIMARY_TIMEOUT)
    results = data.get("results") or []
    if not results:
        raise ValueError(f"геокодирование: {name!r} не найден")
    found = results[0]
    return {"latitude": found.get("latitude"), "longitude": found.get("longitude"),
            "timezone": found.get("timezone"), "name": found.get("name") or name}


async def forecast(ctx, location):
    """Прогноз по координатам или городу (интерфейс ядра): форма Open-Meteo —
    current/hourly/daily (два дня). location {"city": …} — геокодирование
    (api.open-meteo.com); в ответ добавляется "city" — имя города от геокодера.
    Сбой источника — исключение (датасет откатится на старую запись или честный отказ)."""
    location = dict(location or {})
    url = str(ctx.get("url") or "").strip() if ctx is not None else ""
    if location.get("city") and location.get("latitude") is None:
        found = await geocode(ctx, location["city"])
        location = {"latitude": found["latitude"], "longitude": found["longitude"],
                    "timezone": found.get("timezone"), "city": found["name"]}
    query = _query(location)
    if location.get("city"):  # города met.no не геокодирует — резерва нет
        try:
            data = await asyncio.wait_for(_get_json(url or OPEN_METEO_URL, query), PRIMARY_TIMEOUT)
        except Exception as first:
            raise ValueError(f"геокодирование недоступно: {first}") from first
        data["source"] = "open-meteo"
        data["city"] = location["city"]  # имя города от геокодера (nominative)
        return data
    return await _first_source(url or OPEN_METEO_URL, query, location.get("timezone"))


async def _first_source(url, query, timezone):
    """Open-Meteo, а при сбое или молчании дольше HEDGE_DELAY — параллельно резерв api.met.no
    (форма Open-Meteo). Берётся первый успешный ответ, при готовности обоих — Open-Meteo.
    Недоступный Open-Meteo (соединение висит) стоит секунду, а не весь PRIMARY_TIMEOUT."""
    primary = asyncio.ensure_future(asyncio.wait_for(_get_json(url, query), PRIMARY_TIMEOUT))
    params = {"lat": query.get("latitude", ""), "lon": query.get("longitude", "")}
    backup = None
    try:
        await asyncio.wait({primary}, timeout=HEDGE_DELAY)
        if not (primary.done() and primary.exception() is None):
            backup = asyncio.ensure_future(asyncio.to_thread(_http_json, MET_NO_URL, params))
        while True:
            if primary.done() and primary.exception() is None:
                data = primary.result()
                data["source"] = "open-meteo"
                return data
            if backup is not None and backup.done() and backup.exception() is None:
                return met_no_to_open_meteo(backup.result(), timezone)
            pending = {task for task in (primary, backup) if task is not None and not task.done()}
            if not pending:
                raise ValueError(f"open-meteo: {type(primary.exception()).__name__}; met.no: {backup.exception()}")
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (primary, backup):
            if task is not None and not task.done():
                task.cancel()
            elif task is not None and not task.cancelled():
                task.exception()  # результат проигравшего не нужен; ошибка прочитана — без предупреждения цикла
