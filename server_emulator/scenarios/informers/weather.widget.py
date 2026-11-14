"""informers.weather.widget — служебный get_informers → update_all_informers_directive (тихо).

Температура, иконка и описание для виджета главного экрана. Данные — датасет weather
(ctx.dataset_record: свежая запись из БД, иначе плагин, при сбое — старая запись).
Ответ — мгновенный, как у настоящего сервера (его ответ на get_informers уходит в ту же
секунду): при кеше обновление ждётся не дольше секунды — медленный источник дорабатывает
в фоне, свежую запись возьмёт следующий запрос. Нет гео или нет данных — сценарий
отработал без сообщений (`[]`): виджет не трогаем.
Таблицы — в weather.widget.assets/tables.json. Сезон иконки — месяц в таймзоне устройства.
"""

WIDGET_REFRESH_WAIT_S = 1  # кеш есть: ответ не держим дольше секунды


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
    # форма args — как у карточки погоды: диспетчер плагина читает location
    # (координаты и таймзона дома); остальное он берёт из настроек поставщика
    args = {"location": {"latitude": location["latitude"], "longitude": location["longitude"],
                         "timezone": effective.get("timezone")}}
    # Подглядка в кеш: запись есть — ответ не держим дольше секунды (медленный источник
    # дорабатывает в фоне); записи нет — ждать нечего, ждём плагин целиком.
    cached = await ctx.dataset_record("weather", key, timeout=0.1, args=args)
    wait = WIDGET_REFRESH_WAIT_S if cached is not None else settings.get("refresh_timeout")
    record = await ctx.dataset_record(
        "weather", key, max_age=_max_age(settings["max_age_hours"]), args=args, timeout=wait)
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
