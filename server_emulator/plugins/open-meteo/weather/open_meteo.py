"""Open-Meteo — встроенный поставщик погоды (вид weather, интерфейс ядра).

Что отдаёт: прогноз по координатам в форме Open-Meteo — current (температура,
ощущается, код погоды, день/ночь), hourly (лента на сутки), daily (мин/макс,
вероятность осадок) — эту форму уже читают сценарий informers.weather и виджет
погоды; свежесть и кеш обеспечивает датасет weather (записи в БД), поставщик
возвращает только свежие данные.

Источник: api.open-meteo.com/v1/forecast (URL — настройка плагина, можно подставить
Open-Meteo-совместимый). Недоступен — резерв api.met.no (locationforecast 2.0
complete), ответ приводится к форме Open-Meteo (met_no_to_open_meteo), чтобы сценарий
не зависел от источника.

Сеть — стандартная библиотека (urllib.request в asyncio.to_thread); URL — константы
модуля (OPEN_METEO_URL/MET_NO_URL): тесты подменяют их на локальный подменный HTTP,
настоящие сервисы из автотестов не вызываются. met.no требует User-Agent с контактом.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import urllib.request

PROVIDER = {"id": "builtin.open-meteo", "title": "Open-Meteo"}

DEFAULT_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_URL = DEFAULT_URL  # константа для подмены в тестах; настройка url сильнее
PRIMARY_TIMEOUT = 4  # секунд на Open-Meteo, затем met.no
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


def met_no_to_open_meteo(data, timezone=None, hours=24):
    """Ответ met.no (complete) → форма Open-Meteo, которую читают сценарии."""
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
                     "code": _wmo(symbol), "is_day": _is_day(symbol, moment), "precip": probability})
    if not rows:
        raise ValueError("met.no: пустой прогноз")
    temps = [r["temp"] for r in rows if r["temp"] is not None]
    now = rows[0]
    return {
        "source": "met.no",
        "current": {"temperature_2m": now["temp"], "apparent_temperature": now["feels"],
                    "weather_code": now["code"], "is_day": now["is_day"]},
        "hourly": {"time": [r["time"] for r in rows], "temperature_2m": [r["temp"] for r in rows],
                   "weather_code": [r["code"] for r in rows], "is_day": [r["is_day"] for r in rows]},
        "daily": {"temperature_2m_max": [max(temps)], "temperature_2m_min": [min(temps)],
                  "precipitation_probability_max": [max(r["precip"] for r in rows)]},
    }


def _query(location):
    """Координаты дома → query Open-Meteo (текущая + сутки почасово)."""
    return {"latitude": str(location.get("latitude") or ""),
            "longitude": str(location.get("longitude") or ""),
            "current": "temperature_2m,apparent_temperature,weather_code,is_day",
            "hourly": "temperature_2m,weather_code,is_day",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_hours": "24", "forecast_days": "1", "timezone": "auto"}


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


async def forecast(ctx, location):
    """Прогноз по координатам (интерфейс ядра): форма Open-Meteo — current/hourly/daily.
    Сбой источника — исключение (датасет откатится на старую запись или честный отказ)."""
    location = dict(location or {})
    url = str(ctx.get("url") or "").strip() if ctx is not None else ""
    query = _query(location)
    try:  # короткая попытка: резерв должен успеть до таймаута ожидания сценария
        data = await asyncio.wait_for(_get_json(url or OPEN_METEO_URL, query), PRIMARY_TIMEOUT)
        data["source"] = "open-meteo"
        return data
    except Exception as first:
        params = {"lat": query.get("latitude", ""), "lon": query.get("longitude", "")}
        try:  # резерв api.met.no — тот же таймаут потока, ответ в форме Open-Meteo
            payload = await asyncio.to_thread(_http_json, MET_NO_URL, params)
            return met_no_to_open_meteo(payload, location.get("timezone"))
        except Exception as exc:
            raise ValueError(f"open-meteo: {type(first).__name__}; met.no: {exc}") from exc
