"""Погода (scenarios/informers/weather.json): данные из БД (датасет weather),
свежесть из параметров, пинок плагина с таймаутом, честные ответы без фиктивных данных."""
import datetime

from aiohttp import web

from server_emulator.runtime import Runtime
from server_emulator.schema import load_type

LAT, LON = 55.755826, 37.617326
KEY = f"{LAT},{LON}"
FORECAST = {
    "current": {"temperature_2m": 12.4, "apparent_temperature": 10.1, "weather_code": 3, "is_day": 1},
    "hourly": {"time": [f"2026-10-27T{h % 24:02d}:00" for h in range(12, 36)],
               "temperature_2m": [10 + h % 5 for h in range(24)], "weather_code": [3] * 24, "is_day": [1] * 24},
    "daily": {"temperature_2m_max": [14.2], "temperature_2m_min": [8.1], "precipitation_probability_max": [10]},
}


def _runtime(tmp_path, url=None):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-W', 'user_id': 'owner', 'device_ids': []})
    runtime.quasar_params.set_value("user:owner", "user.location.latitude", LAT)
    runtime.quasar_params.set_value("user:owner", "user.location.longitude", LON)
    if url:
        runtime.quasar_params.set_value("server", "server.weather.url", url)
    return runtime


def _down(runtime):
    async def plugin(key, args):
        raise ConnectionError("провайдер недоступен")
    runtime.scenario_plugins["weather"] = plugin


def _age(runtime, seconds):
    db = runtime.store._live()
    with db:
        db.execute("UPDATE scenario_data SET updated_ts=updated_ts-? WHERE scenario_id='dataset:weather'", (seconds,))


async def _say(runtime, phrase, sn="SN-W"):
    sent = []

    async def send(data):
        sent.append(data)

    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg", "sn": sn, "text": phrase},
           "clock": {"now": datetime.datetime.now()}}
    ctx.update(runtime._scenario_context(sn=sn))
    report = await runtime.dialog.handle_text(phrase, ctx, "dg", send)
    return report, sent


def _response(sent):
    cls = load_type("server_emulator/schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
    for raw in sent:
        message = cls.FromString(raw[4:])
        if message.Event.WhichOneof("Event") == "AliceResponse":
            return message.Event.AliceResponse
    raise AssertionError("нет AliceResponse")


async def _provider():
    hits = {"n": 0}

    async def handler(request):
        hits["n"] += 1
        assert request.query["latitude"] == str(LAT) and "current" in request.query
        return web.json_response(FORECAST)

    app = web.Application()
    app.router.add_get("/v1/forecast", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}/v1/forecast", hits


async def test_weather_from_plugin_then_from_db(tmp_path):
    provider, url, hits = await _provider()
    runtime = _runtime(tmp_path, url)
    try:
        report, sent = await _say(runtime, "какая погода")
        assert report["handled"] and report["scenario"] == "informers.weather"
        response = _response(sent)
        assert response.Response.Cards[0].Text == "Сейчас +12. Сегодня от +8 до +14. Осадки не ожидаются."
        names = [d.Name for d in response.Response.Directives]  # + tts_play_placeholder каркаса диалога
        assert names[:2] == ["draw_animation_directive", "divui_show_view"]
        assert report["speech_parts"][:4] == ["Сейчас", "+12.", "Сегодня от", "+8"]  # части для кеша озвучки
        record = runtime.store.scenario_data_record("dataset:weather", "system", "", KEY)
        assert record[0]["current"]["temperature_2m"] == 12.4
        await _say(runtime, "погода на улице")  # свежая запись в БД: плагин не пинается
        assert hits["n"] == 1
    finally:
        await runtime.close()
        await provider.cleanup()


async def test_stale_data_refreshed_when_plugin_answers(tmp_path):
    provider, url, hits = await _provider()
    runtime = _runtime(tmp_path, url)
    runtime.store.scenario_data_set("dataset:weather", "system", "", KEY,
                                    dict(FORECAST, current=dict(FORECAST["current"], temperature_2m=-5)))
    _age(runtime, 3 * 3600)  # старше порога по умолчанию (2 ч)
    try:
        _, sent = await _say(runtime, "погода")
        assert _response(sent).Response.Cards[0].Text.startswith("Сейчас +12.") and hits["n"] == 1
    finally:
        await runtime.close()
        await provider.cleanup()


async def test_stale_data_spoken_with_age_when_plugin_silent(tmp_path):
    runtime = _runtime(tmp_path)
    _down(runtime)
    runtime.store.scenario_data_set("dataset:weather", "system", "", KEY, FORECAST)
    _age(runtime, 3 * 3600)
    try:
        report, sent = await _say(runtime, "погода")
        text = _response(sent).Response.Cards[0].Text
        assert text.startswith("Сейчас +12.") and "Данные не актуальны: обновлены 3 часа" in text
        assert report["speech_parts"][-1].strip().startswith("Данные не актуальны")
    finally:
        await runtime.close()


async def test_freshness_threshold_from_params(tmp_path):
    runtime = _runtime(tmp_path)
    _down(runtime)
    runtime.quasar_params.set_value("server", "server.weather.max_age_hours", "6")
    runtime.store.scenario_data_set("dataset:weather", "system", "", KEY, FORECAST)
    _age(runtime, 3 * 3600)  # 3 ч < 6 ч — свежо, без пометки
    try:
        _, sent = await _say(runtime, "погода")
        assert "не актуальны" not in _response(sent).Response.Cards[0].Text
    finally:
        await runtime.close()


async def test_no_data_honest(tmp_path):
    runtime = _runtime(tmp_path)
    _down(runtime)
    try:
        report, sent = await _say(runtime, "погода")
        assert report["status"] == "finished"
        assert _response(sent).Response.Cards[0].Text == "Нет данных о погоде: плагин погоды не ответил."
    finally:
        await runtime.close()


async def test_no_geo_honest(tmp_path):
    runtime = Runtime(tmp_path)
    _down(runtime)
    try:
        _, sent = await _say(runtime, "погода", sn="")
        assert _response(sent).Response.Cards[0].Text.startswith("Не знаю, где вы")
    finally:
        await runtime.close()


async def test_user_reply_scenario_v2(tmp_path):
    """Пользовательский сценарий-ответ v2: фраза → текст, без кода."""
    runtime = Runtime(tmp_path)
    runtime.settings["scenarios"] = [{
        "format_version": 2, "id": "user.hello", "version": "1.0.0", "category": "custom",
        "triggers": [{"kind": "phrase", "phrases": ["привет"]}],
        "flow": [{"id": "reply", "op": "compose", "reply": {"text": "Привет! Чем помочь?"}},
                 {"id": "done", "op": "finish"}]}]
    try:
        report, sent = await _say(runtime, "привет", sn="")
        assert report["scenario"] == "user.hello" and report["status"] == "finished"
        assert _response(sent).Response.Cards[0].Text == "Привет! Чем помочь?"
    finally:
        await runtime.close()
