"""Встроенный поставщик погоды «Яндекс Погода»: ответ API v2 → форма Open-Meteo.

Настоящие api.weather.yandex.ru и геокодер не вызываются — URL-константы модуля
подменяются на локальный HTTP; ключ — фиктивный test-token.
"""
import datetime
import importlib.util
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import unused_port

import server_emulator

MODULE = Path(server_emulator.__file__).parent / "plugins" / "yandex-weather" / "weather" / "yandex_weather.py"

V2_RESPONSE = {
    "info": {"tzinfo": {"offset": 10800, "name": "Europe/Moscow"}},
    "fact": {"temp": 2, "feels_like": -2, "condition": "overcast", "daytime": "n"},
    "forecasts": [
        {"date": "2026-11-15", "sunrise": "08:05", "sunset": "16:20",
         "parts": {"night": {"temp_min": 0, "temp_max": 2, "condition": "cloudy", "prec_prob": 10,
                             "wind_speed": 3, "wind_gust": 7, "wind_dir": "s"},
                   "day": {"temp_min": 1, "temp_max": 4, "condition": "light-rain", "prec_prob": 60,
                           "wind_speed": 4, "wind_gust": 9, "wind_dir": "sw"}},
         "hours": [{"hour": str(h), "temp": h % 5, "condition": "cloudy", "prec_prob": 0} for h in range(24)]},
        {"date": "2026-11-16", "sunrise": "08:07", "sunset": "16:18",
         "parts": {"day": {"temp_min": -1, "temp_max": 1, "condition": "light-snow", "prec_prob": 80,
                           "wind_speed": 5, "wind_gust": 11, "wind_dir": "n"},
                   "evening": {"temp_avg": -2, "condition": "snow", "prec_prob": 90,
                               "wind_speed": 6, "wind_gust": 12, "wind_dir": "nw"}},
         "hours": [{"hour": str(h), "temp": -h % 3, "condition": "snow", "prec_prob": 90} for h in range(24)]},
    ],
}


def load_provider():
    spec = importlib.util.spec_from_file_location("builtin_yandex_weather", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExtCtx:
    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, key, default=None):
        return self._values.get(key, default)


def test_v2_to_open_meteo_form():
    module = load_provider()
    data = module.yandex_to_open_meteo(V2_RESPONSE, now=datetime.datetime(2026, 11, 15, 18, 30))
    assert data["current"] == {"temperature_2m": 2, "apparent_temperature": -2,
                               "weather_code": 3, "is_day": 0}
    hourly = data["hourly"]
    assert hourly["time"][0] == "2026-11-15T18:00" and len(hourly["time"]) == 24
    assert hourly["time"][-1] == "2026-11-16T17:00"
    assert hourly["is_day"][0] == 0 and hourly["is_day"][hourly["time"].index("2026-11-16T12:00")] == 1
    daily = data["daily"]
    assert daily["temperature_2m_min"] == [0, -2] and daily["temperature_2m_max"] == [4, 1]
    assert daily["weather_code"] == [61, 71]  # условие дневной части
    assert daily["precipitation_probability_max"] == [60, 90]
    assert daily["wind_speed_10m_max"] == [4, 6] and daily["wind_gusts_10m_max"] == [9, 12]
    assert daily["wind_direction_10m_dominant"] == [225, 0]


async def test_forecast_without_key_fails():
    module = load_provider()
    with pytest.raises(ValueError, match="ключ"):
        await module.forecast(ExtCtx(), {"latitude": 55.75, "longitude": 37.62})


async def test_forecast_city_via_geocoder(monkeypatch):
    module = load_provider()
    seen = {}

    async def geocode(request):
        seen["geocode"] = request.query["name"]
        return web.json_response({"results": [{"latitude": 55.75, "longitude": 37.62, "name": "Москва"}]})

    async def forecast(request):
        seen["key"] = request.headers.get("X-Yandex-Weather-Key")
        seen["query"] = dict(request.query)
        return web.json_response(V2_RESPONSE)

    app = web.Application()
    app.router.add_get("/v1/search", geocode)
    app.router.add_get("/v2/forecast", forecast)
    runner = web.AppRunner(app)
    await runner.setup()
    port = unused_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(module, "GEOCODE_URL", base + "/v1/search")
    monkeypatch.setattr(module, "FORECAST_URL", base + "/v2/forecast")
    try:
        result = await module.forecast(ExtCtx({"api_key": "test-token"}), {"city": "москва"})
    finally:
        await runner.cleanup()
    assert seen["geocode"] == "москва" and seen["key"] == "test-token"
    assert seen["query"]["lat"] == "55.75" and seen["query"]["hours"] == "true"
    assert result["city"] == "Москва" and result["source"] == "yandex-weather"
