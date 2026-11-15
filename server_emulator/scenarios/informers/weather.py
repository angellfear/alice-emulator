"""informers.weather — «какая погода», «погода на завтра», «погода в <город>» и тап по
виджету → фраза + карточка как у настоящего сервера.

Ответ собирается по образцам настоящего сервера (запись 2026-11-12 20:05, upstream
C00JN, обмены 182600/182705/182778): draw_animation_directive (animations/weather/<группа>/
<время суток>) + divui_show_view (карточка weather_day, CardId weather_day_key_static_key:
иконка-глобальный-шаблон, постер и прозрачное видео погоды, «Сейчас в <город>», температура,
«Ощущается как …», галерея 24 часов, кнопка закрытия) + tts_play_placeholder (дописывает
каркас ответа). Каркас карточки — встроенный шаблон app_cards/weather.json (реестр
screen_templates, сборка registry().build("weather", …)); глобальные шаблоны иконок
(имена со слагом условия и периода) и галерея часов — то, что реестр не подставляет
(динамические ключи и массивы): маркер weather_hours_placeholder заменяется элементами
часов, GlobalTemplates достраивается из таблицы card_templates до сборки.

Речь — формулировками образца: «Сейчас <[pause none]> 0. Сегодня от -1 до +1. Будет снег
с дождём с 2 до 3 часов ночи.» (окно осадок — из почасовых кодов поставщика); «Завтра
от -1 до 0, дождь со снегом, слабый южный ветер 2 м/с, порывы до 6 м/с.» (условие и
ветер — дневные поля поставщика). Заголовок «Завтра вечером в <город>» — как в образце:
карточка завтра показывает вечер (видео evening, анимация night — оба из образца).

Данные — у выбранного поставщика погоды (вид weather, providers.py): датасет weather —
кеш записей, обновляет его диспетчер вызовом forecast поставщика (свежая запись — сразу,
иначе плагин с таймаутом, при сбое — старая запись с пометкой «Данные не актуальны»).
«в <город>»: поставщик геокодирует имя (open-meteo geocoding, город — ключ кеша);
падеж названия — pymorphy3. Поставщика нет — честный ответ «нет источника данных».
"""
import json
import re

from server_emulator.sdk import SDKError, format_duration

# слаг времени суток для медиа: «сейчас» — по текущему часу (образец: 20:05 → evening),
# «завтра» — evening (видео) и night (анимация), как в образце «Погода на завтра»
DAYPARTS = ((5, 11, "morning"), (11, 17, "day"), (17, 22, "evening"))
TOMORROW_VIDEO = "evening"
TOMORROW_ANIMATION = "night"
ICON_BASE = "https://yastatic.net/weather/i/icons/portal/png/128x128/light/"
VIDEO_BASE = "https://smart-display-res.s3.yandex.net/generated/weather_video/"
PREVIEW_BASE = "https://yastatic.net/s3/smart-display-res/generated/weather_video/"
HOURS_TEMPLATE = "weather_hours_template"  # образец элемента часа в каркасе карточки

# направление ветра 0-360° → прилагательное (образец: «южный»), 8 румбов
WIND_DIRS = ("северный", "северо-восточный", "восточный", "юго-восточный",
             "южный", "юго-западный", "западный", "северо-западный")

# слаг условия для видео/иконок по коду погоды: наблюдаемые в образцах — cloudy
# (182600, 182778) и rain_light (182705, «дождь со снегом»); остальное — ближайший
# наблюдаемый класс (слаги чистого снега/ясно не записаны — пометка в provenance)
VIDEO_CONDS = {"0": "cloudy", "1": "cloudy", "2": "cloudy", "3": "cloudy", "45": "cloudy",
               "48": "cloudy", "51": "rain_light", "53": "rain_light", "55": "rain_light",
               "61": "rain_light", "63": "rain_light", "65": "rain_light", "66": "rain_light",
               "67": "rain_light", "71": "cloudy", "73": "cloudy", "75": "cloudy",
               "77": "cloudy", "80": "rain_light", "81": "rain_light", "82": "rain_light",
               "85": "cloudy", "86": "cloudy", "95": "rain_light", "96": "rain_light",
               "99": "rain_light", "*": "cloudy"}
# превью короче видео: rain_light → rain (образец 182705)
PREVIEW_CONDS = {"rain_light": "rain"}

# группа анимации по коду: снег и мокрый снег — snow (образец 182705: snow/night при
# «дождь со снегом»), ясная — clear (пробы .84), остальное — cloudy (слаг анимации
# чистого дождя не записан)
ANIMATION_GROUPS = {"0": "clear", "1": "clear", "2": "cloudy", "3": "cloudy", "45": "cloudy",
                    "48": "cloudy", "51": "cloudy", "53": "cloudy", "55": "cloudy",
                    "61": "cloudy", "63": "cloudy", "65": "cloudy", "66": "snow", "67": "snow",
                    "71": "snow", "73": "snow", "75": "snow", "77": "snow", "80": "cloudy",
                    "81": "cloudy", "82": "cloudy", "85": "snow", "86": "snow",
                    "95": "cloudy", "96": "cloudy", "99": "cloudy", "*": "cloudy"}

# коды осадок для фразы «Будет …» (WMO ≥ 51 — осадки; 77 — снежные зёрна, не явление)
PRECIP_CODES = set(range(51, 100)) - {77}
RAIN_CODES = {51, 53, 55, 61, 63, 65, 80, 81, 82}
SNOW_CODES = {71, 73, 75, 85, 86}


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


def _parse_query(query):
    """(tomorrow, city) из хвоста фразы: «на завтра», «в Москве». Город — группа после
    предлога «в/во»; нормальная форма — для геокодирования и кеша."""
    if not query:
        return False, None
    text = str(query).lower().replace("ё", "е")
    tomorrow = "завтра" in text
    match = re.search(r"(?:^|\s)во?\s+(.+)$", text)
    city = None
    if match:
        raw = match.group(1).replace("завтра", "").strip(" .?!")
        if raw:
            from server_emulator.scenario_engine import wordforms
            city = " ".join(wordforms.lemma(word) for word in raw.split())
    return tomorrow, city


def _daypart(hour):
    """Слово «ночи/утра/дня/вечера» для часа суток (образец: «с 2 до 3 часов ночи»)."""
    if hour < 5:
        return "ночи"
    if hour < 11:
        return "утра"
    if hour < 17:
        return "дня"
    return "вечера"


def _time_period(hour):
    """Слаг времени суток для медиа по часу (образец: 20:05 → evening)."""
    for start, end, name in DAYPARTS:
        if start <= hour < end:
            return name
    return "night"


def _hour_text(iso):
    """«2026-11-12T21:00» → «21»."""
    return str(iso or "").split("T", 1)[-1].split(":", 1)[0]


def _precip_sentence(hours):
    """Окно осадок из почасовых кодов — фраза образца: «Будет снег с дождём с 2 до
    3 часов ночи.»; осадок нет — «Осадки не ожидаются.»."""
    rows = _hours(hours)
    wet = [row for row in rows if int(row.get("weather_code") or 0) in PRECIP_CODES]
    if not wet:
        return "Осадки не ожидаются."
    codes = {int(row.get("weather_code") or 0) for row in wet}
    kind = ("снег с дождём" if (codes & SNOW_CODES and codes & RAIN_CODES)
            else "дождь" if codes & RAIN_CODES else "снег")
    start = int(_hour_text(wet[0].get("time")) or 0)
    end = int(_hour_text(wet[-1].get("time")) or 0)
    return f"Будет {kind} с {start} до {end} часов {_daypart(start)}."


def _wind_phrase(daily, index):
    """«слабый южный ветер 2 м/с, порывы до 6 м/с.» — из дневных полей поставщика
    (образец 182705)."""
    speed = round(float(daily["wind_speed_10m_max"][index] or 0))
    gusts = round(float(daily["wind_gusts_10m_max"][index] or 0))
    direction = float(daily["wind_direction_10m_dominant"][index] or 0)
    adjective = "слабый" if speed <= 3 else "умеренный" if speed <= 6 else "сильный"
    point = WIND_DIRS[int(round(direction / 45)) % 8]
    return f"{adjective} {point} ветер {speed} м/с, порывы до {gusts} м/с."


def _city_title(city):
    """Название города в предложном падеже («москва» → «Москве») — pymorphy3;
    не склоняется — с большой буквы, как есть."""
    import pymorphy3
    parsed = pymorphy3.MorphAnalyzer().parse(str(city or ""))
    locative = parsed[0].inflect({"loct"}) if parsed else None
    return (locative.word if locative else str(city or "")).capitalize()


def _hour_items(template_item, hours, tables):
    """Элементы галереи часов: копия образца из каркаса (log_id снимается), подстановка
    времени, иконки по коду часа (таблица icons, как у основного ответа) и температуры."""
    items = []
    for row in hours or []:
        item = json.loads(json.dumps(template_item, ensure_ascii=False))
        item.pop("log_id", None)
        item["items"][0]["text"] = _hour_text(row.get("time"))
        icon = _lookup(tables["icons"], row.get("weather_code"), row.get("is_day"))
        item["items"][1]["items"][0]["image_url"] = f"{ICON_BASE}{icon}.png"
        item["items"][1]["items"][1]["text"] = f"{_signed(row.get('temperature_2m'))}°"
        items.append(item)
    return items


def _global_templates(condition, period, tables):
    """Глобальные шаблоны иконок Div2Card: имена со слагом периода/условия (образец:
    icon_type_weather_icon_common_night_cloudy_global_template +
    icon_type_part_weather_common_night_overcast_global_template); тела — из таблицы
    card_templates (образцы 182600/182705), собственное имя внутри тела заменяется.
    → (карта GlobalTemplates, имя шаблона основной иконки)."""
    entry = _lookup(tables.get("card_templates", {}), condition)
    icon_name = f"icon_type_weather_icon_common_{period}_{entry['icon_cond']}_global_template"
    part_name = f"icon_type_part_weather_common_{period}_{entry['part_cond']}_global_template"
    result = {}
    for name, source in ((icon_name, entry["icon_body"]), (part_name, entry["part_body"])):
        body = json.loads(json.dumps(source, ensure_ascii=False))
        old = list(body.keys())[0]
        body[name] = json.loads(json.dumps(body.pop(old), ensure_ascii=False).replace(old, name))
        result[name] = {"StringBody": json.dumps(body, ensure_ascii=False, separators=(",", ":"))}
    return result, icon_name


def _replace_hours(node, hours, tables):
    """Замена образца элемента часа (log_id weather_hours_template) в галерее каркаса
    на элементы по данным поставщика."""
    for key in ("items", "content"):
        children = node.get(key)
        if not isinstance(children, list):
            continue
        for index, child in enumerate(children):
            if child.get("log_id") == HOURS_TEMPLATE:
                children[index:index + 1] = _hour_items(child, hours, tables)
                return True
            if _replace_hours(child, hours, tables):
                return True
    return False


def _card_directive(weather, title, hour_now, tomorrow, tables):
    """divui_show_view: каркас — шаблон app_cards/weather.json (реестр screen_templates),
    данные — подстановки; галерея часов и глобальные шаблоны достраивает сценарий."""
    from server_emulator import screen_templates

    current = weather["current"]
    code = current["weather_code"]
    condition = _lookup(tables["conditions"], code)
    cond_short = _lookup(VIDEO_CONDS, code)
    video_period = TOMORROW_VIDEO if tomorrow else _time_period(hour_now)
    slug = f"common_{video_period}_{cond_short}"
    preview = f"common_{video_period}_{PREVIEW_CONDS.get(cond_short, cond_short)}"
    templates, icon_name = _global_templates(cond_short, video_period, tables)
    values = {
        "title": title,
        "temp": f"{_signed(current['temperature_2m'])}°",
        # перенос строки — как в образце («Ощущается как -4°, / пасмурно» в две строки)
        "feels": f"Ощущается как {_signed(current['apparent_temperature'])}°,\n{condition}",
        "icon_template": icon_name,
        "slug": slug,
        "video_uri": f"{VIDEO_BASE}chiron_{slug}_LOOP.webm?v=1",
        "preview_uri": f"{PREVIEW_BASE}preview_chiron_{preview}",
    }
    directive = screen_templates.registry().build("weather", values)
    fields = directive["fields"]
    card = fields["Div2Card"]["StringBody"]
    if isinstance(card, str):  # реестр жмёт StringBody в строку — разбираем для достройки
        fields["Div2Card"]["StringBody"] = json.loads(card)
    body = fields["Div2Card"]["StringBody"]["card"]
    _replace_hours(body["states"][0]["div"], _hours(weather.get("hourly")), tables)
    fields["Div2Card"]["GlobalTemplates"] = templates
    fields["Div2Card"]["StringBody"] = json.dumps(fields["Div2Card"]["StringBody"],
                                                  ensure_ascii=False, separators=(",", ":"))
    return directive


def _animation_directive(path):
    """draw_animation_directive — форма образца: AnimationStopPolicy и
    SpeakingAnimationPolicy как у настоящего сервера."""
    return {"name": "draw_animation_directive",
            "type": "client_action", "is_led_silent": True,
            "analytics_type": "draw_animation_directive",
            "proto": "NAlice.TAnimationCapability.TDrawAnimationDirective",
            "fields": {"Animations": [{"S3Directory": {
                "Bucket": "https://quasar.s3.yandex.net", "Path": path}}],
                "AnimationStopPolicy": "RepeatTillScreenAlive",
                "SpeakingAnimationPolicy": "PlaySpeakingEndOfTts",
                "Name": "draw_animation_directive"}}


async def handle(ctx):
    effective = ctx.params.get("effective") or {}
    location = effective.get("location") or {}
    tomorrow, city = _parse_query(str(ctx.slots.get("query") or ""))
    if location.get("latitude") is None and not city:
        return {"text": "Не знаю, где вы: задайте координаты дома в параметрах пользователя."}  # поставщика погоды нет — честный ответ
    settings = effective.get("weather") or {}
    if city:
        key = f"city:{city}"
        geo = {"city": city}
    else:
        key = f"{location['latitude']},{location['longitude']}"
        geo = {"latitude": location["latitude"], "longitude": location["longitude"],
               "timezone": effective.get("timezone")}
    try:
        record = await ctx.dataset_record(
            "weather", key, max_age=_max_age(settings["max_age_hours"]),
            args={"location": geo},
            timeout=settings.get("refresh_timeout"))
    except SDKError:  # ни записи, ни плагина — честный ответ вместо сбоя
        return ctx.no_data_source()
    if record is None:
        return {"text": "Нет данных о погоде: поставщик погоды не ответил."}
    weather = record["value"]
    stale_note = (f" Данные не актуальны: обновлены {format_duration(record['age_s'])} назад."
                  if record["stale"] else "")
    tables = ctx.asset("tables")

    city_in = _city_title(city) if city else location.get("city_in")
    where = f" в {city_in}" if city_in else ""
    title = f"Завтра вечером{where}" if tomorrow else f"Сейчас{where}"

    hour_now = ctx.now.hour
    current, daily = weather["current"], weather["daily"]
    tomorrow_index = 1 if len(daily.get("temperature_2m_min") or []) > 1 else 0

    now = _signed(current["temperature_2m"])
    if tomorrow:  # диапазон и ветер — своего дня (образец 182705: «Завтра от -1 до 0»)
        low = _signed(daily["temperature_2m_min"][tomorrow_index])
        high = _signed(daily["temperature_2m_max"][tomorrow_index])
        condition = _lookup(tables["conditions"],
                            (daily.get("weather_code") or [None, None])[tomorrow_index])
        text = (f"Завтра от {low} до {high}, {condition}, {_wind_phrase(daily, tomorrow_index)}"
                f"{stale_note}")
    else:
        low, high = _signed(daily["temperature_2m_min"][0]), _signed(daily["temperature_2m_max"][0])
        text = (f"Сейчас <[pause none]> {now}. Сегодня от {low} до {high}. "
                f"{_precip_sentence(weather.get('hourly'))}{stale_note}")

    animation_period = TOMORROW_ANIMATION if tomorrow else _time_period(hour_now)
    animation = _animation_directive(
        f"animations/weather/{_lookup(ANIMATION_GROUPS, current['weather_code'])}/{animation_period}")
    card = _card_directive(weather, title, hour_now, tomorrow, tables)
    return {"text": text, "speech_parts": [text],
            "directives": [animation, card]}
