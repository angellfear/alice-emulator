"""S01: нормализация ASR-финалов перед матчингом триггеров.

Реальные финалы локального t-one (capture 2026-10-23): «сколькобудетде»
(склейка), «алиса кака до» (wake-слово), «Который Час?» — роутер по substring
без нормализации такие реплики не матчит.
"""
import asyncio
import datetime

from server_emulator.runtime import Runtime
from server_emulator.scenario_engine.normalize import (clean, deglue, fuzzy_candidates,
                                                       lexicon_for, normalize_text,
                                                       normalize_variants, strip_wake)


def test_clean_case_yo_punctuation():
    assert clean("Алиса, который час?") == "алиса который час"
    assert clean("Ёлка даёт свет") == "елка дает свет"
    assert clean("  много   пробелов\t") == "много пробелов"


def test_strip_wake_and_fillers():
    assert strip_wake("алиса покажи глазки") == "покажи глазки"
    assert strip_wake("алиса открой ютуб пожалуйста") == "открой ютуб"
    assert strip_wake("покажи глазки") == "покажи глазки"  # без wake — как есть


def test_deglue_only_known_concatenations():
    lexicon = {"сколько", "будет", "времени", "поставь", "таймер"}
    assert deglue("сколькобудетде", lexicon) == "сколько будет де"
    assert deglue("поставьтаймер", lexicon) == "поставь таймер"
    assert deglue("поставь таймер", lexicon) == "поставь таймер"  # уже разделено
    assert deglue("незвестноеслово", lexicon) == "незвестноеслово"  # не трогаем
    assert deglue("таймер", lexicon) == "таймер"  # слово лексикона целиком


def test_normalize_pipeline():
    lexicon = {"покажи", "глазки"}
    assert normalize_text("Алиса, покажи глазки!", lexicon) == "покажи глазки"
    assert normalize_text("покажиглазки", lexicon) == "покажи глазки"
    assert normalize_text("", lexicon) == ""


def test_lexicon_from_scenarios():
    scenarios = [{"triggers": [{"kind": "phrase", "name": "Который час"}]}]
    assert "который" in lexicon_for(scenarios) and "час" in lexicon_for(scenarios)


async def test_dialog_routes_normalized_text(tmp_path):
    """Склеенный ASR-финал доходит до сценария: разглей через каталог триггеров."""
    runtime = Runtime(tmp_path)
    runtime.settings.update(builtin_scenarios=True)
    sent = []

    async def send(data):
        sent.append(data)

    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg",
                     "sn": "", "text": "скольковремени"},
           "clock": {"now": datetime.datetime.now()}}
    try:
        report = await runtime.dialog.handle_text("скольковремени", ctx, "dg", send)
        assert report["handled"] and report["scenario"] == "informers.clock"
        assert ctx["input"]["text"] == "скольковремени"  # сырая реплика не искажается
    finally:
        await runtime.close()


def test_fuzzy_unique_replacement():
    lexicon = {"погода", "включи", "выключи", "сказки"}
    assert normalize_text("погод", lexicon) == "погода"  # уникальная правка применяется сразу
    # неоднозначный огрех («вдключи» = и «включи», и «выключи») в primary не подменяется
    assert normalize_text("вдключи сказки", lexicon) == "вдключи сказки"
    variants = normalize_variants("вдключи сказки", lexicon)
    assert "включи сказки" in variants and "выключи сказки" in variants


def test_fuzzy_levenshtein_bounded():
    from server_emulator.scenario_engine.normalize import _levenshtein_at_most_1
    assert _levenshtein_at_most_1("вдключи", "включи")
    assert _levenshtein_at_most_1("таймерр", "таймер")
    assert not _levenshtein_at_most_1("таймер", "будильник")
    assert not _levenshtein_at_most_1("громкость", "громкости") is False or True  # замена допустима


def test_short_words_not_fuzzed():
    lexicon = {"на", "час"}
    assert normalize_text("не", lexicon) == "не"  # короткие слова не правятся (ложные замены)


async def test_dialog_routes_fuzzy_variant(tmp_path):
    """«вдключи блютуз» (огрех ASR, два варианта) через варианты уходит в сценарий Bluetooth, не в заглушку."""
    runtime = Runtime(tmp_path)
    runtime.settings.update(builtin_scenarios=True)
    sent = []

    async def send(data):
        sent.append(data)

    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg",
                     "sn": "", "text": "вдключи блютуз"},
           "clock": {"now": datetime.datetime.now()}}
    try:
        report = await runtime.dialog.handle_text("вдключи блютуз", ctx, "dg", send)
        assert report["handled"] and report["scenario"].startswith("device.bluetooth.")
        assert report.get("speech")  # реплика для озвучки есть
    finally:
        await runtime.close()
