"""informers.weather.widget — служебный get_informers → update_all_informers_directive (тихо).

Температура, иконка и описание для виджета главного экрана. Данные — датасет weather
(ctx.dataset_record: свежая запись из БД, иначе плагин с таймаутом, при сбое — старая запись).
Нет гео или нет данных — сценарий отработал без сообщений (`[]`): виджет не трогаем.
Таблицы — в weather.widget.assets/tables.json. Сезон иконки — месяц в таймзоне устройства.
"""


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


def _max_age(hours):
    """Часы в секунды (целое, если можно) — фильтр mul:3600."""
    seconds = float(hours) * 3600
    return int(seconds) if seconds.is_integer() else seconds


async def handle(ctx):
    effective = ctx.params.get("effective") or {}
    location = effective.get("location") or {}
    if location.get("latitude") is None:  # гео нет — виджет не обновляем
        return []
    settings = effective.get("weather") or {}
    key = f"{location['latitude']},{location['longitude']}"
    record = await ctx.dataset_record(
        "weather", key, max_age=_max_age(settings["max_age_hours"]),
        args={"url": settings.get("url"), "timezone": effective.get("timezone"),
              "query": {"latitude": location["latitude"], "longitude": location["longitude"],
                        "current": "temperature_2m,apparent_temperature,weather_code,is_day",
                        "hourly": "temperature_2m,weather_code,is_day",
                        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                        "forecast_hours": "24", "forecast_days": "1", "timezone": "auto"}},
        timeout=settings.get("refresh_timeout"))
    if record is None:  # нет данных и плагин молчит — тихое завершение
        return []
    current = record["value"]["current"]  # устаревшая запись тоже годится (как у JSON)
    tables = ctx.asset("tables")
    code, is_day = current["weather_code"], current["is_day"]
    icon = _lookup(tables["icons"], code, is_day)
    return {"envelope": "service", "directives": [{
        "name": "update_all_informers_directive",
        "analytics_type": "update_all_informers",
        "is_led_silent": True,
        "proto": "NAlice.TInformersCapability.TUpdateAllInformersDirective",
        "fields": {
            "Name": "update_all_informers",
            "ReminderInformerState": {"ShootAt": {}},
            "WeatherInformerState": {
                "Icon": f"https://yastatic.net/weather/i/icons/funky/png/flat/128/{icon}.png",
                "Temperature": current["temperature_2m"],
                "IconType": icon,
                "Condition": {"Title": _lookup(tables["conditions"], code),
                              "Cloudness": _lookup(tables["cloudness"], code),
                              "Season": _lookup(tables["season"], ctx.now.month)},
            },
        },
    }]}
