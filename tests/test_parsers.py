"""Парсеры шага extract: механика без словарей — слова только из lexicons.json/сценария."""
from server_emulator.scenario_engine.library import LEXICONS
from server_emulator.scenario_engine.parsers import (PARSERS, parse_duration, parse_lexicon, parse_list,
                                                     parse_number, parse_rest, parse_time)

DEVICES = [{"sn": "A", "name": "Дуо", "aliases": ["дуо 24"]},
           {"sn": "B", "name": "Станция в спальне", "aliases": ["дуо 32", "спальня"]}]


def test_registry():
    assert set(PARSERS) == {"lexicon", "list", "rest", "number", "duration", "time"}


def test_lexicon_longest_phrase_wins():
    assert parse_lexicon("включи свет на кухне", {"свет": "a", "свет на кухне": "b"}) == "b"


def test_lexicon_prefix_and_whole_word():
    assert parse_lexicon("выключите свет", {"выключ*": "off"}) == "off"
    assert parse_lexicon("кухнями", {"кухня": "k"}) is None  # целыми словами
    assert parse_lexicon("ёлка", {"елка": 1}) == 1  # ё → е


def test_lexicon_list_and_missing():
    assert parse_lexicon("открой ютуб", ["ютуб", "кинопоиск"]) == "ютуб"
    assert parse_lexicon("ничего", {"свет": 1}) is None


def test_list_by_aliases_longest_wins():
    assert parse_list("поставь будильник на дуо 32", [], items=DEVICES, fields=["name", "aliases"],
                      contains_only=True)["sn"] == "B"
    assert parse_list("таймер на дуо 24", [], items=DEVICES, fields=["name", "aliases"],
                      contains_only=True)["sn"] == "A"


def test_list_contains_only_blocks_reverse_match():
    items = [{"name": "кинопоиск"}]
    assert parse_list("открой кино", ["открой"], items=items)["name"] == "кинопоиск"
    assert parse_list("открой кино", ["открой"], items=items, contains_only=True) is None


def test_list_no_match():
    assert parse_list("поставь будильник на 7", [], items=DEVICES, fields=["name", "aliases"],
                      contains_only=True) is None


def test_rest():
    assert parse_rest("открой ютуб", ["открой"]) == "ютуб"
    assert parse_rest("ютуб", ["открой"]) is None


def test_number():
    numbers = LEXICONS["numbers"]
    assert parse_number("громкость 7", numbers) == 7
    assert parse_number("двадцать пять", numbers) == 25
    assert parse_number("громкость 150", numbers, minimum=0, maximum=100) is None


def test_duration():
    lexicon = LEXICONS["duration"]
    assert parse_duration("таймер на 5 минут", lexicon) == 300
    assert parse_duration("полчаса", lexicon) == 1800
    assert parse_duration("один час тридцать минут", lexicon) == 5400
    assert parse_duration("90", lexicon) == 90
    assert parse_duration("будильник", lexicon) is None


def test_time():
    lexicon = LEXICONS["time"]
    assert parse_time("на 7:30", lexicon) == "07:30"
    assert parse_time("в 9.15", lexicon) == "09:15"
    assert parse_time("на семь", lexicon) == "07:00"
    assert parse_time("25:00", lexicon) is None
    assert parse_time("будильник", lexicon) is None
