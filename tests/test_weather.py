"""Погода (scenarios/informers/weather.json): данные у выбранного поставщика
(builtin.open-meteo на подменном HTTP), датасет weather — кеш записей, свежесть из
параметров, ожидание с таймаутом, честные ответы без фиктивных данных. Карточка —
форма образцов настоящего сервера (запись 2026-11-12)."""
import datetime
import json

from aiohttp import web

from server_emulator.runtime import Runtime
from server_emulator.schema import load_type

LAT, LON = 55.755826, 37.617326
KEY = f"{LAT},{LON}"
FORECAST = {
    "current": {"temperature_2m": 12.4, "apparent_temperature": 10.1, "weather_code": 3, "is_day": 1},
    "hourly": {"time": [f"2026-10-27T{h % 24:02d}:00" for h in range(12, 36)],
               "temperature_2m": [10 + h % 5 for h in range(24)], "weather_code": [3] * 24, "is_day": [1] * 24,
               "precipitation_probability": [0] * 24},
    "daily": {"time": ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31",
                       "2026-11-01", "2026-11-02"],  # с вторника, как в оригинале
              "temperature_2m_max": [14.2, 15.0, 13.0, 12.0, 14.0, 11.0, 10.0],
              "temperature_2m_min": [8.1, 9.0, 7.0, 6.0, 8.0, 5.0, 6.0],
              "precipitation_probability_max": [10, 20, 30, 40, 10, 0, 20],
              "weather_code": [3, 67, 3, 3, 71, 3, 3],
              "wind_speed_10m_max": [2.0, 3.0, 4.0, 5.0, 2.0, 3.0, 2.0],
              "wind_gusts_10m_max": [5.0, 6.0, 7.0, 8.0, 5.0, 6.0, 5.0],
              "wind_direction_10m_dominant": [0.0, 180.0, 90.0, 270.0, 0.0, 45.0, 90.0]},
}


def _runtime(tmp_path, url=None):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-W', 'user_id': 'owner', 'device_ids': []})
    runtime.quasar_params.set_value("user:owner", "user.location.latitude", LAT)
    runtime.quasar_params.set_value("user:owner", "user.location.longitude", LON)
    if url:  # API поставщика — настройка плагина builtin.open-meteo (не параметр сервера)
        runtime.settings.setdefault("extensions", {}).setdefault("settings", {})[
            "builtin.open-meteo"] = {"url": url}
    return runtime


def _down(runtime):
    async def plugin(key, args):
        raise ConnectionError("поставщик погоды недоступен")
    runtime.scenario_plugins["weather"] = plugin  # подмена диспетчера датасета


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
        assert "current" in request.query  # геокодированный город приходит с другими координатами
        return web.json_response(FORECAST)

    async def geocode(request):
        assert request.query["name"] and request.query["language"] == "ru"
        return web.json_response({"results": [{"latitude": 56.01, "longitude": 92.87,
                                               "timezone": "Asia/Krasnoyarsk", "name": "Москва"}]})

    app = web.Application()
    app.router.add_get("/v1/forecast", handler)
    app.router.add_get("/v1/search", geocode)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    return runner, base + "/v1/forecast", hits


def _card(sent):
    """Div2-карточка divui_show_view ответа (декод payload → поля директивы):
    PayloadRaw собирает реестр шаблонов схемой «centaur» — тем же файлом декодируем."""
    from server_emulator.scenario_engine.proto_build import message_class
    from server_emulator.schema import message_dict
    response = _response(sent)
    directive = next(d for d in response.Response.Directives if d.Name == "divui_show_view")
    cls = message_class("server_emulator/schemas",
                        "NAlice.TLayeredDivUICapability.TDivUIShowViewDirective")
    payload = message_dict(cls.FromString(bytes(directive.PayloadRaw)))
    return payload["Div2Card"]


async def test_weather_from_plugin_then_from_db(tmp_path):
    provider, url, hits = await _provider()
    runtime = _runtime(tmp_path, url)
    try:
        report, sent = await _say(runtime, "какая погода")
        assert report["handled"] and report["scenario"] == "informers.weather"
        response = _response(sent)
        assert response.Response.Cards[0].Text == \
            "Сейчас <[pause none]> +12. Сегодня от +8 до +14. Осадки не ожидаются."  # речь по образцу 182600
        names = [d.Name for d in response.Response.Directives]  # + tts_play_placeholder каркаса диалога
        assert names[:2] == ["draw_animation_directive", "divui_show_view"]
        assert report["speech_parts"] == ["Сейчас <[pause none]> +12. Сегодня от +8 до +14. "
                                          "Осадки не ожидаются."]  # часть для кеша озвучки
        record = runtime.store.scenario_data_record("dataset:weather", "system", "", KEY)
        assert record[0]["current"]["temperature_2m"] == 12.4
        await _say(runtime, "погода на улице")  # свежая запись в БД: поставщик не дёргается
        assert hits["n"] == 1
    finally:
        await runtime.close()
        await provider.cleanup()


async def test_weather_card_matches_sample(tmp_path):
    """Карточка — форма образца 182600 (карточка weather_day, CardId
    weather_day_key_static_key): иконка-шаблон и видео со слагом условия, глобальные
    шаблоны иконок (два, имена со слагом), «Сейчас в <город>», температура,
    «Ощущается как …», галерея 24 часов с иконками по коду часа, кнопка закрытия."""
    provider, url, hits = await _provider()
    runtime = _runtime(tmp_path, url)
    runtime.quasar_params.set_value("user:owner", "user.location.city_in", "Березниках")
    try:
        _, sent = await _say(runtime, "какая погода")
        card = _card(sent)
        assert card["CardName"] == "WeatherDayHoursData"
        assert card["Id"] == {"CardName": "weather", "CardId": "weather_day_key_static_key"}
        templates = card["GlobalTemplates"]
        assert len(templates) == 2
        assert any(name.startswith("icon_type_weather_icon_") for name in templates)
        assert any(name.startswith("icon_type_part_weather_") for name in templates)
        body = json.loads(card["StringBody"])["card"]
        assert body["log_id"] == "weather_day"
        left = body["states"][0]["div"]["items"][0]
        assert left["items"][1]["text"] == "Сейчас в Березниках"  # заголовок образца
        assert left["items"][2]["items"][0]["text"] == "+12°"
        assert left["items"][2]["items"][1]["text"] == "Ощущается как +10°,\nпасмурно"
        gallery = left["items"][3]
        assert len(gallery["items"]) == 24
        first = gallery["items"][0]
        assert first["items"][0]["text"] == "12:00"  # полное время, как в оригинале
        assert first["border"] == {"corners_radius": {"top-left": 24, "bottom-left": 24}}
        assert "border" not in gallery["items"][1]  # середина ленты — квадратные стыки
        assert gallery["items"][-1]["border"] == {"corners_radius": {"top-right": 24, "bottom-right": 24}}
        assert first["items"][1]["items"][0]["image_url"] == \
            "https://yastatic.net/weather/i/icons/portal/png/128x128/light/ovc.png"
        assert first["items"][1]["items"][1]["text"] == "+10°"
        header = left["items"][0]
        assert header["items"][0]["type"].startswith("icon_type_weather_icon_")
        video = header["items"][2]["items"][0]
        assert video["custom_props"]["source_uri"].startswith(
            "https://smart-display-res.s3.yandex.net/generated/weather_video/chiron_")
        assert header["items"][1]["image_url"].startswith(
            "https://yastatic.net/s3/smart-display-res/generated/weather_video/preview_chiron_")
        close_button = left["items"][4]
        assert close_button["actions"][0]["log_id"] == "weather_close_button_element_click"
    finally:
        await runtime.close()
        await provider.cleanup()


async def test_weather_week_days_card(tmp_path):
    """«погода на неделю» — карточка-оригинал 462155: WeatherDaysRangeData, галерея
    7 дней (день недели, иконка, днём и ночью — ночная приглушённая), лента без
    зазоров со скруглением краёв; анимации в ответе нет."""
    provider, url, hits = await _provider()
    runtime = _runtime(tmp_path, url)
    try:
        report, sent = await _say(runtime, "погода на неделю")
        assert report["handled"] and report["scenario"] == "informers.weather"
        # оригинал 462155: карточка + tts_play_placeholder, анимации нет
        assert [d.Name for d in _response(sent).Response.Directives] == \
            ["divui_show_view", "tts_play_placeholder"]
        card = _card(sent)
        assert card["CardName"] == "WeatherDaysRangeData"
        assert card["Id"] == {"CardName": "weather", "CardId": "weather_day_key_static_key"}
        body = json.loads(card["StringBody"])["card"]
        assert body["log_id"] == "weather_days"
        left = body["states"][0]["div"]["items"][0]
        assert left["items"][1]["text"] == "Погода на неделю"  # заголовок оригинала
        gallery = left["items"][3]
        assert len(gallery["items"]) == 7
        first = gallery["items"][0]
        assert first["items"][0]["text"] == "Вт"  # 2026-10-27 — вторник
        assert first["items"][1]["items"][1]["text"] == "+14°"  # днём
        assert first["items"][1]["items"][2]["text"] == "+8°"  # ночью
        assert first["items"][1]["items"][2]["alpha"] == 0.5  # приглушённая, как в оригинале
        assert first["items"][1]["items"][0]["image_url"].endswith("/ovc.png")
        assert first["border"] == {"corners_radius": {"top-left": 24, "bottom-left": 24}}
        assert "border" not in gallery["items"][1]  # середина ленты — квадратные стыки
        last = gallery["items"][-1]
        assert last["border"] == {"corners_radius": {"top-right": 24, "bottom-right": 24}}
        assert last["items"][0]["text"] == "Пн"
    finally:
        await runtime.close()
        await provider.cleanup()


async def test_weather_tomorrow_speech_per_sample(tmp_path):
    """«погода на завтра» — речь образца 182705: «Завтра от …, дождь со снегом,
    слабый южный ветер …, порывы до … м/с.» (код и ветер — дневные поля поставщика)."""
    provider, url, hits = await _provider()
    runtime = _runtime(tmp_path, url)
    try:
        report, sent = await _say(runtime, "погода на завтра")
        assert report["handled"] and report["scenario"] == "informers.weather"
        text = _response(sent).Response.Cards[0].Text
        assert text == ("Завтра от +9 до +15, дождь со снегом, "
                        "слабый южный ветер 3 м/с, порывы до 6 м/с.")
        assert hits["n"] == 1
    finally:
        await runtime.close()
        await provider.cleanup()


async def test_weather_city_geocoded(tmp_path):
    """«погода в Москве» — геокодирование поставщиком (отдельный ключ кеша),
    падеж города — предложный, речь и карточка по образцу 182778."""
    provider, url, hits = await _provider()
    runtime = _runtime(tmp_path, url)
    try:
        report, sent = await _say(runtime, "погода в москве")
        assert report["handled"] and report["scenario"] == "informers.weather"
        card = _card(sent)
        body = json.loads(card["StringBody"])["card"]
        title = body["states"][0]["div"]["items"][0]["items"][1]["text"]
        assert title == "Сейчас в Москве"
        record = runtime.store.scenario_data_record("dataset:weather", "system", "", "city:москва")
        assert record[0]["city"] == "Москва"  # имя от геокодера
        assert record[0]["current"]["temperature_2m"] == 12.4
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
        assert _response(sent).Response.Cards[0].Text.startswith("Сейчас <[pause none]> +12.") and hits["n"] == 1
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
        assert text.startswith("Сейчас <[pause none]> +12.") and "Данные не актуальны: обновлены 3 часа" in text
        assert report["speech_parts"][-1].strip().endswith("Данные не актуальны: обновлены 3 часа назад.")
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
        assert _response(sent).Response.Cards[0].Text == "Нет данных о погоде: поставщик погоды не ответил."
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


def test_parse_query_strips_time_words_from_city():
    from server_emulator.scenarios.informers.weather import _parse_query
    assert _parse_query("в москве на завтра") == (True, False, "москва")
    assert _parse_query("на завтра в москве") == (True, False, "москва")
    assert _parse_query("в нижнем новгороде сегодня") == (False, False, "нижний новгород")
    assert _parse_query("завтра") == (True, False, None)
    assert _parse_query("на неделю") == (False, True, None)
    assert _parse_query("на неделю в москве") == (False, True, "москва")
