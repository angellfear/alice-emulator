"""Плагины данных сценариев: код, который наполняет датасеты в БД (шаг refresh).

Плагин — async (key, args) → значение записи датасета; запись в БД делает
executor. Сценарий знает только форму записи, не провайдера.

weather — ЗАГЛУШКА: настоящий плагин (своё расписание опроса, выбор и
сравнение провайдеров, прогноз на дни) ещё не реализован. Сейчас: Open-Meteo
по args.url/args.query; недоступен — api.met.no, приведённый к форме записи
Open-Meteo (current / hourly / daily), чтобы сценарий не зависел от источника.
"""
from __future__ import annotations

import asyncio
import datetime

PRIMARY_TIMEOUT = 4  # секунд на Open-Meteo, затем met.no
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


def weather_plugin(fetch_json, session_factory):
    """Плагин weather (заглушка). fetch_json(url, query, ttl) — общий GET JSON;
    session_factory() — aiohttp-сессия для met.no (нужен свой User-Agent)."""
    async def run(key, args):
        query = {k: str(v) for k, v in (args.get("query") or {}).items()}
        try:  # короткая попытка: резерв должен успеть до таймаута ожидания сценария
            data = await asyncio.wait_for(fetch_json(str(args.get("url") or ""), query, 0), PRIMARY_TIMEOUT)
            data["source"] = "open-meteo"
            return data
        except Exception as first:
            from aiohttp import ClientTimeout
            params = {"lat": query.get("latitude", ""), "lon": query.get("longitude", "")}
            async with session_factory().get(MET_NO_URL, params=params, headers={"User-Agent": MET_NO_AGENT},
                                             timeout=ClientTimeout(total=10)) as response:
                if response.status != 200:
                    raise ValueError(f"open-meteo: {type(first).__name__}; met.no: HTTP {response.status}")
                payload = await response.json(content_type=None)
            return met_no_to_open_meteo(payload, args.get("timezone"))
    return run
