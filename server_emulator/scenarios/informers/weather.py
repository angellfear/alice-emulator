"""informers.weather — «какая погода», «сколько градусов» и тап по виджету → фраза + карточка.

Фраза и озвучка собираются здесь; своя Div2-карточка WeatherNow (заголовок, температура, почасовая
лента) — ресурс weather.assets/directives.json (директивы draw_animation + divui_show_view), таблицы —
weather.assets/tables.json. Подстановку в карточку делает compose: скрипт кладёт значения
в ctx.slots["weather"] и ctx.state (title, animation, condition, hours), а шаблоны
ресурса {{state.*}} / {{slots.weather.*}} раскрываются тем же движком, что у JSON-сценариев.
Данные — датасет weather (ctx.dataset_record: свежая запись, иначе плагин с таймаутом,
при сбое — старая запись с пометкой «Данные не актуальны»).
"""
from server_emulator.sdk import SDKError, format_duration


def _lookup(table, key, outer=None):
    """Значение таблицы по ключу ("*" — по умолчанию); outer — ключ первого уровня
    двухуровневой таблицы (is_day → код погоды), как фильтр lookup шаблонов."""
    if outer is not None:
        table = table.get(str(outer), table.get("*"))
        if not isinstance(table, dict):
            raise KeyError(f"нет {outer!r} в таблице")
    key = str(key)
    if key in table:
        return table[key]
    if "*" in table:
        return table["*"]
    raise KeyError(f"нет {key!r} в таблице")


def _signed(value):
    """«+2» / «-5» / «0» (округление до целого) — фильтр signed."""
    number = round(float(value))
    return f"{number:+d}" if number else "0"


def _max_age(hours):
    """Часы в секунды (целое, если можно) — фильтр mul:3600."""
    seconds = float(hours) * 3600
    return int(seconds) if seconds.is_integer() else seconds


def _hours(hourly, limit=24):
    """Почасовой прогноз Open-Meteo (колонки {поле: [..]}) → строки для карточки."""
    columns = {name: values for name, values in (hourly or {}).items() if isinstance(values, list)}
    width = min((len(values) for values in columns.values()), default=0)
    return [{name: values[index] for name, values in columns.items()} for index in range(width)][:limit]


def _precip(weather):
    """Осадки: вероятность на сегодня ≥ 50 %; нет значения — «не ожидаются»."""
    try:
        return ("Возможны осадки."
                if float(weather["daily"]["precipitation_probability_max"][0]) >= 50
                else "Осадки не ожидаются.")
    except (KeyError, IndexError, TypeError, ValueError):
        return "Осадки не ожидаются."


async def handle(ctx):
    effective = ctx.params.get("effective") or {}
    location = effective.get("location") or {}
    if location.get("latitude") is None:
        return {"text": "Не знаю, где вы: задайте координаты дома в параметрах пользователя."}
    settings = effective.get("weather") or {}
    key = f"{location['latitude']},{location['longitude']}"
    try:
        record = await ctx.dataset_record(
            "weather", key, max_age=_max_age(settings["max_age_hours"]),
        args={"url": settings.get("url"), "timezone": effective.get("timezone"),
              "query": {"latitude": location["latitude"], "longitude": location["longitude"],
                        "current": "temperature_2m,apparent_temperature,weather_code,is_day",
                        "hourly": "temperature_2m,weather_code,is_day",
                        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                        "forecast_hours": "24", "forecast_days": "1", "timezone": "auto"}},
            timeout=settings.get("refresh_timeout"))
    except SDKError:  # ни записи, ни плагина — честный ответ вместо сбоя (090)
        return ctx.no_data_source()
    if record is None:
        return {"text": "Нет данных о погоде: плагин погоды не ответил."}
    weather = record["value"]
    stale_note = (f" Данные не актуальны: обновлены {format_duration(record['age_s'])} назад."
                  if record["stale"] else "")
    city = location.get("city_in")
    precip = _precip(weather)
    current, daily = weather["current"], weather["daily"]
    code, is_day = current["weather_code"], current["is_day"]
    tables = ctx.asset("tables")

    # значения для шаблонов карточки (ресурс directives.json)
    ctx.slots["weather"] = weather
    ctx.state.update(
        title=f"Сейчас в {city}" if city is not None else "Сейчас",
        animation=_lookup(tables["animation"], code, is_day),
        condition=_lookup(tables["conditions"], code),
        hours=_hours(weather.get("hourly")))

    now, low, high = _signed(current["temperature_2m"]), _signed(daily["temperature_2m_min"][0]), \
        _signed(daily["temperature_2m_max"][0])
    return {
        "text": f"Сейчас {now}. Сегодня от {low} до {high}. {precip}{stale_note}",
        "speech_prefix": '<speaker is_whisper="true"> ',
        "speech_parts": ["Сейчас", f"{now}.", "Сегодня от", low, "до", f"{high}.", precip, stale_note],
        "directives": ctx.asset("directives"),
    }
