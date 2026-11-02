"""Дата, дни недели и время крупного города (TASK-080).

Образцы настоящего сервера (захват 2026-11-02): «Сегодня 2 ноября», «Сегодня понедельник»,
«8 марта 2027 года будет понедельник» (следующее 8 марта), «В понедельник 2 ноября»
(ближайший такой день недели, сегодня тоже считается), «Сейчас в Екатеринбурге 22:35,
понедельник, 2 ноября». Часовой пояс — по словарю крупных городов; неизвестный город —
честный ответ без промаха в журнале.
"""
import datetime
from zoneinfo import ZoneInfo

from server_emulator.sdk import say

MONTHS_GEN = {1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
              7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"}
WEEKDAYS = {0: "понедельник", 1: "вторник", 2: "среда", 3: "четверг",
            4: "пятница", 5: "суббота", 6: "воскресенье"}
WEEKDAY_ON_DATE = {1: "в понедельник", 2: "во вторник", 3: "в среду", 4: "в четверг",
                   5: "в пятницу", 6: "в субботу", 7: "в воскресенье"}

# Город (предложный падеж, как в фразе) → часовой пояс IANA
CITY_TZ = {
    "москве": "Europe/Moscow", "санкт-петербурге": "Europe/Moscow", "питере": "Europe/Moscow",
    "екатеринбурге": "Asia/Yekaterinburg", "новосибирске": "Asia/Novosibirsk",
    "казани": "Europe/Moscow", "нижнем новгороде": "Europe/Moscow", "челябинске": "Asia/Yekaterinburg",
    "самаре": "Europe/Samara", "омске": "Asia/Omsk", "красноярске": "Asia/Krasnoyarsk",
    "перми": "Asia/Yekaterinburg", "воронеже": "Europe/Moscow", "волгограде": "Europe/Volgograd",
    "ростове-на-дону": "Europe/Moscow", "уфе": "Asia/Yekaterinburg", "краснодаре": "Europe/Moscow",
    "владивостоке": "Asia/Vladivostok", "иркутске": "Asia/Irkutsk", "тюмени": "Asia/Yekaterinburg",
    "сочи": "Europe/Moscow", "калининграде": "Europe/Kaliningrad", "сургуте": "Asia/Yekaterinburg",
    "новомосковске": "Europe/Moscow", "березниках": "Asia/Yekaterinburg",
}


def _today(now):
    return now.weekday(), now.day, now.month, now.year


def _next_date(weekday_name, now):
    """Ближайшая дата с таким днём недели (1–7, пн=1); сегодня тоже считается."""
    target = weekday_name - 1
    shift = (target - now.weekday()) % 7
    return now.date() + datetime.timedelta(days=shift)


def _next_month_day(day, month, now):
    """Ближайшее {day} {month} от сегодня включительно (образец: «8 марта 2027 года»)."""
    candidate = now.date().replace(month=month, day=day)
    if candidate < now.date():
        candidate = candidate.replace(year=candidate.year + 1)
    return candidate


def handle(ctx):
    text = ctx.text.lower().strip()
    slots = ctx.slots
    now = ctx.now

    city = (slots.get("query") or "").strip()
    if city and text.rstrip(".!?, ").endswith(city.lower()):
        key = city.lower().rstrip(".!?, ")
        title = key[:1].upper() + key[1:]
        tz_name = CITY_TZ.get(key)
        if not tz_name:
            return say(f"Не знаю часовой пояс города {title}.")
        local = datetime.datetime.now(ZoneInfo(tz_name))
        weekday, day, month, _ = _today(local)
        hhmm = local.strftime("%H:%M")
        return say(f"Сейчас в {title} {hhmm}, {WEEKDAYS[weekday]}, {day} {MONTHS_GEN[month]}.")

    weekday_slot = slots.get("weekday")
    if weekday_slot is not None:
        date = _next_date(int(weekday_slot), now)
        phrase = WEEKDAY_ON_DATE[int(weekday_slot)]
        return say(f"{phrase[0].upper()}{phrase[1:]} {date.day} {MONTHS_GEN[date.month]}.")

    day, month = slots.get("day"), slots.get("month")
    if day is not None and month is not None:
        date = _next_month_day(int(day), int(month), now)
        return say(f"{date.day} {MONTHS_GEN[date.month]} {date.year} года будет {WEEKDAYS[date.weekday()]}.")

    if slots.get("date_word") == "завтра":
        date = now.date() + datetime.timedelta(days=1)
        return say(f"Завтра {WEEKDAYS[date.weekday()]}.")
    if slots.get("date_word") == "послезавтра":
        date = now.date() + datetime.timedelta(days=2)
        return say(f"Послезавтра {WEEKDAYS[date.weekday()]}.")

    _, d, m, _ = _today(now)
    if "день" in text:
        return say(f"Сегодня {WEEKDAYS[now.weekday()]}.")
    return say(f"Сегодня {d} {MONTHS_GEN[m]}.")
