"""Вид weather: встроенный поставщик Open-Meteo и сценарий погоды на выбранном поставщике.

Контракт вида: forecast(ctx, location) → прогноз в форме Open-Meteo (current/hourly/daily),
ctx — ExtensionContext плагина (настройки свежие); датасет weather — кеш записей, его
обновляет диспетчер (scenario_plugins.weather_plugin) вызовом forecast. Настоящие
api.open-meteo.com и api.met.no из тестов не вызываются — URL-константы модуля
подменяются на локальный подменный HTTP; сценарные тесты — на заглушке-поставщике
в реестре (механика как в tests/golden).
"""
import datetime
import importlib.util
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import unused_port

import server_emulator
from server_emulator import providers
from server_emulator.runtime import Runtime

MODULE = Path(server_emulator.__file__).parent / "plugins" / "open-meteo" / "weather" / "open_meteo.py"

OPEN_METEO_RESPONSE = {
    "current": {"temperature_2m": 12.4, "apparent_temperature": 10.1, "weather_code": 3, "is_day": 1},
    "hourly": {"time": ["2026-10-27T12:00", "2026-10-27T13:00"],
               "temperature_2m": [12.4, 11.9], "weather_code": [3, 3], "is_day": [1, 1]},
    "daily": {"temperature_2m_max": [14.2], "temperature_2m_min": [8.1],
              "precipitation_probability_max": [10]},
}

MET_NO_TIMESERIES = {"properties": {"timeseries": [
    {"time": "2026-10-27T12:00:00Z", "data": {"instant": {"details": {"air_temperature": 11.0}},
     "next_1_hours": {"summary": {"symbol_code": "partlycloudy_day"},
                      "details": {"precipitation_amount": 0}}}},
]}}


def load_provider():
    """Модуль встроенного плагина как отдельный (без импорта остального кода ядра)."""
    spec = importlib.util.spec_from_file_location("builtin_open_meteo", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExtCtx:
    """Минимальный ExtensionContext-двойник: get(key) — настройка плагина."""

    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, key, default=None):
        return self._values.get(key, default)


@pytest.fixture()
def provider():
    module = load_provider()
    yield module


@pytest.fixture()
async def fake_http(provider, monkeypatch):
    """Локальный подменный HTTP: /forecast (Open-Meteo) и /metno (мет-резерв)."""
    state = {"open_meteo_status": 200, "metno_status": 200,
             "metno_payload": MET_NO_TIMESERIES, "calls": []}

    async def forecast(request):
        state["calls"].append(("open-meteo", dict(request.query)))
        if state["open_meteo_status"] != 200:
            return web.json_response({"error": "x"}, status=state["open_meteo_status"])
        return web.json_response(OPEN_METEO_RESPONSE)

    async def metno(request):
        state["calls"].append(("met.no", dict(request.query)))
        if state["metno_status"] != 200:
            return web.Response(status=state["metno_status"])
        return web.json_response(state["metno_payload"])

    app = web.Application()
    app.router.add_get("/forecast", forecast)
    app.router.add_get("/metno", metno)
    runner = web.AppRunner(app)
    await runner.setup()
    port = unused_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(provider, "OPEN_METEO_URL", base + "/forecast")
    monkeypatch.setattr(provider, "MET_NO_URL", base + "/metno")
    state["base"] = base
    yield state
    await runner.cleanup()


LOCATION = {"latitude": 55.755826, "longitude": 37.617326, "timezone": "Europe/Moscow"}


# --- встроенный плагин: прогноз и резерв -------------------------------------------------


async def test_forecast_primary_source(provider, fake_http):
    """Open-Meteo доступен — форма Open-Meteo как есть, source=open-meteo; query —
    координаты и запрошенные поля."""
    result = await provider.forecast(ExtCtx(), LOCATION)
    assert result["source"] == "open-meteo"
    assert result["current"]["temperature_2m"] == 12.4
    assert result["hourly"]["time"][0] == "2026-10-27T12:00"
    assert result["daily"]["temperature_2m_max"] == [14.2]
    kind, query = fake_http["calls"][0]
    assert kind == "open-meteo"
    assert query["latitude"] == "55.755826" and query["timezone"] == "auto"
    assert "temperature_2m" in query["hourly"] and "precipitation_probability_max" in query["daily"]


async def test_forecast_url_from_plugin_settings(provider, fake_http):
    """Настройка url плагина сильнее константы: подставляем Open-Meteo-совместимый API."""
    await provider.forecast(ExtCtx({"url": fake_http["base"] + "/forecast"}), LOCATION)
    assert fake_http["calls"][0][0] == "open-meteo"


async def test_forecast_falls_back_to_met_no(provider, fake_http):
    """Open-Meteo недоступен — резерв api.met.no, приведённый к форме Open-Meteo."""
    fake_http["open_meteo_status"] = 500
    result = await provider.forecast(ExtCtx(), LOCATION)
    assert result["source"] == "met.no"
    assert result["current"]["temperature_2m"] == 11.0
    assert result["current"]["weather_code"] == 2  # partlycloudy → WMO 2
    assert [kind for kind, _ in fake_http["calls"]] == ["open-meteo", "met.no"]
    assert fake_http["calls"][1][1]["lat"] == "55.755826"


async def test_forecast_both_sources_down(provider, fake_http):
    """Оба источника недоступны — исключение (датасет откатится на старую запись)."""
    fake_http["open_meteo_status"] = 500
    fake_http["metno_status"] = 503
    with pytest.raises(ValueError, match="met.no"):
        await provider.forecast(ExtCtx(), LOCATION)


# --- реестр и сценарий на выбранном поставщике -------------------------------------------


FORECAST = {
    "source": "open-meteo",
    "current": {"temperature_2m": 12.4, "apparent_temperature": 10.1, "weather_code": 3, "is_day": 1},
    "hourly": {"time": [f"2026-10-27T{h:02d}:00" for h in range(12, 24)],
               "temperature_2m": [10 + h % 5 for h in range(12)], "weather_code": [3] * 12,
               "is_day": [1] * 12},
    "daily": {"temperature_2m_max": [14.2], "temperature_2m_min": [8.1],
              "precipitation_probability_max": [10]},
}


def make_runtime(tmp_path, forecast=None):
    """Runtime с владельцем, колонкой и координатами дома; фразы погоды — из
    справочника включённого open-meteo, данные — заглушка в реестре (если задана
    forecast), сеть не дёргается. Музыкальные поставщики выключены — их мир в
    tests/test_music_*.py."""
    runtime = Runtime(tmp_path)
    state = runtime.settings.setdefault("extensions", {})
    for ext_id in ("builtin.spotify", "builtin.yandex-music"):
        state["enabled"][ext_id] = False
    runtime.extensions.load_enabled()
    runtime.identities.mutate({"operation": "user", "id": "owner", "name": "O"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-W", "user_id": "owner",
                               "name": "Станция"})
    runtime.quasar_params.set_value("user:owner", "user.location.latitude", 55.755826)
    runtime.quasar_params.set_value("user:owner", "user.location.longitude", 37.617326)
    if forecast is not None:
        async def run(ctx, location):
            return json.loads(json.dumps(forecast))

        providers.PROVIDERS["weather"] = {"golden-weather": {
            "id": "golden-weather", "title": "Погода (заглушка)", "service": "",
            "kind": "weather", "ext_id": "golden-weather", "origin": "builtin", "file": "",
            "forecast": run}}
    return runtime


async def _say(runtime, text):
    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg", "sn": "SN-W",
                     "text": text},
           "clock": {"now": datetime.datetime(2026, 10, 27, 21, 7)}, "sequence": 1,
           "route_mode": "emulate", "player_owner": None,
           **runtime._scenario_context(sn="SN-W")}
    return await runtime.dialog.handle_text(text, ctx, "dg", None, deliver=False)


async def test_builtin_open_meteo_in_registry(tmp_path):
    """Свежий Runtime: builtin.open-meteo в реестре вида weather (origin builtin)."""
    runtime = Runtime(tmp_path)
    try:
        records = providers.PROVIDERS["weather"]
        assert "builtin.open-meteo" in records
        assert records["builtin.open-meteo"]["origin"] == "builtin"
        assert callable(records["builtin.open-meteo"]["forecast"])
        assert providers.selected("weather", runtime.settings)["id"] == "builtin.open-meteo"
    finally:
        await runtime.close()


async def test_weather_answers_from_provider_through_dataset(tmp_path):
    """«Какая погода»: диспетчер дёргает forecast выбранного поставщика, датасет — кеш:
    повторная реплика свежей записи не вызывает forecast второй раз."""
    calls = []
    runtime = make_runtime(tmp_path)

    async def counting(ctx, location):
        calls.append(dict(location))
        return json.loads(json.dumps(FORECAST))

    providers.PROVIDERS["weather"] = {"golden-weather": {
        "id": "golden-weather", "title": "Погода (заглушка)", "service": "",
        "kind": "weather", "ext_id": "golden-weather", "origin": "builtin", "file": "",
        "forecast": counting}}
    try:
        report = await _say(runtime, "какая погода")
        assert report["handled"], report
        assert "Сейчас" in report["speech"] and "+12" in report["speech"]
        assert "от +8" in report["speech"] and "до +14" in report["speech"]
        assert calls == [{"latitude": 55.755826, "longitude": 37.617326,
                          "timezone": calls[0]["timezone"]}]  # координаты — из параметров дома
        assert "timezone" in calls[0]
        await _say(runtime, "сколько градусов")
        assert len(calls) == 1  # свежая запись датасета — поставщик не дёргался
    finally:
        await runtime.close()


async def test_weather_provider_failure_honest_answer(tmp_path):
    """Поставщик не ответил (и записи нет) — честный отказ вместо сбоя."""
    runtime = make_runtime(tmp_path)

    async def broken(ctx, location):
        raise ConnectionError("недоступен")

    providers.PROVIDERS["weather"] = {"golden-weather": {
        "id": "golden-weather", "title": "Погода (заглушка)", "service": "",
        "kind": "weather", "ext_id": "golden-weather", "origin": "builtin", "file": "",
        "forecast": broken}}
    try:
        report = await _say(runtime, "какая погода")
        assert report["handled"], report
        assert report["speech"] == "Нет данных о погоде: поставщик погоды не ответил."
    finally:
        await runtime.close()


async def test_weather_without_provider_no_data_source(tmp_path):
    """Поставщика погоды нет — честный ответ «нет источника данных» («Простой»)."""
    runtime = make_runtime(tmp_path)
    try:
        runtime.settings["intelligence"] = "scenarios"
        state = runtime.settings["extensions"]["enabled"]
        state["builtin.open-meteo"] = False  # нет плагина — нет фраз
        runtime.extensions.load_enabled()
        report = await _say(runtime, "какая погода")
        assert not report["handled"] and report["reason"] == "no_match", report
    finally:
        await runtime.close()


async def test_weather_extension_provider_collected(tmp_path):
    """Внешнее расширение с папкой weather/*.py собирается в реестр вида weather."""
    runtime = make_runtime(tmp_path)
    ext = runtime.directory / "ext" / "wext"
    (ext / "weather").mkdir(parents=True)
    (ext / "extension.json").write_text(json.dumps(
        {"id": "wext", "name": "W", "version": "1.0.0", "description": "d", "author": "A",
         "license": "MIT", "requires": {"emulator": ">=0.1.0", "sdk": ">=2.0.0"},
         "permissions": {}, "contributes": {"providers": [
             {"kind": "weather", "title": "Погода", "module": "weather/wfake.py"}]}}), encoding="utf-8")
    (ext / "weather" / "wfake.py").write_text(
        'PROVIDER = {"id": "wfake", "title": "Фейковая погода"}\n'
        "async def forecast(ctx, location):\n"
        "    return {'source': 'fake'}\n", encoding="utf-8")
    state = runtime.settings["extensions"]
    state["installed"]["wext"] = {"version": "1.0.0", "dev": str(ext)}
    state["enabled"]["wext"] = True
    runtime.extensions.load_enabled()
    try:
        records = providers.PROVIDERS["weather"]
        assert "wfake" in records and records["wfake"]["origin"] == "extension"
        runtime.settings.setdefault("providers", {})["weather"] = "wfake"  # их двое — выбранный явный
        assert providers.selected("weather", runtime.settings)["id"] == "wfake"
    finally:
        await runtime.close()
