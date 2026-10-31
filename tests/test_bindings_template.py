"""RF-09: binding-источник template — реплика целиком в JSON.

Золотые фразы зафиксированы пиннутыми литералами: после миграции RF-09
прежние computed-функции (local_time_phrase/duration_phrase/alarm_confirm_phrase)
удалены, литералы защищают русские склонения фильтров time_ru/duration/hhmm.
"""
import datetime

import pytest

from server_emulator.scenario_engine.bindings import BindingError, evaluate

CLOCK = {"now": datetime.datetime(2026, 10, 23, 12, 5)}
CTX = {"clock": CLOCK, "slots": {}, "state": {}, "input": {}}


def template(value, ctx=None):
    return evaluate({"source": "template", "value": value}, ctx or CTX)


# --- золотые фразы фильтров (пиннутые литералы, RF-09) ----------------------------


@pytest.mark.parametrize("now, expected", [
    (datetime.datetime(2026, 10, 23, 12, 5), "Сейчас 12 часов 5 минут."),
    (datetime.datetime(2026, 10, 23, 0, 0), "Сейчас 0 часов."),      # полночь
    (datetime.datetime(2026, 10, 23, 21, 0), "Сейчас 21 час."),      # ноль минут — без минут
    (datetime.datetime(2026, 10, 23, 1, 2), "Сейчас 1 час 2 минуты."),
    (datetime.datetime(2026, 10, 23, 12, 21), "Сейчас 12 часов 21 минута."),
])
def test_time_ru_matches_legacy_phrase(now, expected):
    assert template("Сейчас {{clock|time_ru}}.", {"clock": {"now": now}}) == expected


@pytest.mark.parametrize("seconds, expected", [
    (45, "Таймер на 45 секунд."),
    (60, "Таймер на 1 минуту."),
    (90, "Таймер на 1 минуту 30 секунд."),
    (300, "Таймер на 5 минут."),
    (5400, "Таймер на 1 час 30 минут."),
    (3600, "Таймер на 1 час."),
    (7325, "Таймер на 2 часа 2 минуты 5 секунд."),
    (22, "Таймер на 22 секунды."),
])
def test_duration_matches_legacy_phrase(seconds, expected):
    assert template("Таймер на {{slots.seconds|duration}}.",
                    {"slots": {"seconds": seconds}}) == expected


@pytest.mark.parametrize("time, expected", [
    ("07:30", "Будильник на 7:30."),
    ("7:05", "Будильник на 7:05."),
    ("23:59", "Будильник на 23:59."),
    ("00:00", "Будильник на 0:00."),
])
def test_hhmm_matches_legacy_phrase(time, expected):
    assert template("Будильник на {{slots.time|hhmm}}.",
                    {"slots": {"time": time}}) == expected


# --- самостоятельное поведение template -------------------------------------------


def test_template_without_filter_stringifies_value():
    assert template("Привет, {{user.name}}!", {"user": {"name": "Оля"}}) == "Привет, Оля!"


def test_template_dotted_path_into_nested_fields():
    ctx = {"device": {"fields": {"room": "Кухня"}}}
    assert template("Свет: {{device.fields.room}}.", ctx) == "Свет: Кухня."


def test_plural_filter_forms():
    ctx = {"slots": {"count": 5}}
    assert template("{{slots.count|plural:минуту,минуты,минут}}", ctx) == "5 минут"
    for count, word in ((1, "минуту"), (2, "минуты"), (4, "минуты"), (11, "минут"),
                        (21, "минуту"), (100, "минут")):
        assert template("{{slots.count|plural:минуту,минуты,минут}}",
                        {"slots": {"count": count}}) == f"{count} {word}"


def test_template_multiple_tokens_and_plain_text():
    ctx = {"slots": {"seconds": 90}, "state": {"label": "чай"}}
    assert template("Таймер «{{state.label}}» на {{slots.seconds|duration}} готов.",
                    ctx) == "Таймер «чай» на 1 минуту 30 секунд готов."


def test_template_errors():
    with pytest.raises(BindingError):
        template("{{nowhere.x}}")  # неизвестный корень
    with pytest.raises(BindingError):
        template("{{slots.x}}")  # путь отсутствует
    with pytest.raises(BindingError):
        template("{{slots.seconds|nope}}", {"slots": {"seconds": 1}})  # фильтр не реестровый
    with pytest.raises(BindingError):
        template("{{slots.count|plural:одна,две}}", {"slots": {"count": 1}})  # не три формы
    with pytest.raises(BindingError):
        template("{{slots.time|hhmm}}", {"slots": {"time": "завтра"}})  # не Ч:ММ
    with pytest.raises(BindingError):
        template("{{slots.seconds|duration}}", {"slots": {"seconds": 0}})  # не положительная
    with pytest.raises(BindingError):
        evaluate({"source": "template", "value": 5}, CTX)  # value не строка


def test_template_braces_without_tokens_are_literal():
    assert template("{{a.b}} и {not_a_token}", {"a": {"b": "1"}}) == "1 и {not_a_token}"
