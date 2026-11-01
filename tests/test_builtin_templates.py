"""Шаблонные триггеры встроенных сценариев: маршрут и слоты (порядок каталога,
слой пользователя, first-hit слоты, чек-валидатор, пол dry-run)."""
import datetime

import pytest

from server_emulator.scenario_engine import library
from server_emulator.scenario_engine.executor import Executor
from server_emulator.scenario_engine.library import SCENARIOS
from server_emulator.scenario_engine.normalize import deglue, lexicon_for, normalize_text
from server_emulator.scenario_engine.probe import dependencies_ok
from server_emulator.scenario_engine.router import candidates, with_templates
from server_emulator.scenario_engine.templates import TemplateIndex, check_template

CATALOG = list(SCENARIOS.values())
INDEX = TemplateIndex(CATALOG)
LEXICON = lexicon_for(CATALOG)


def _executor():
    """Эффекты-примитивы и код py-сценариев доступны: маршрут не режется зависимостями."""
    actions = {"device.push": lambda a, e: {}, "timer.start": lambda a, e: {},
               "alarm.start": lambda a, e: {}, "timer.cancel": lambda a, e: 0,
               "light.set": lambda a, e: {}}
    return Executor(actions=actions,
                    script_path=lambda scenario: (library.ROOT / scenario["_file"]).with_suffix(".py"))


def full_route(text, user_scenarios=()):
    """Тот же порядок, что dialog._candidates: свой слой (пользователь + расширения)
    раньше встроенного, внутри слоя шаблон раньше фразы; первый supported."""
    replica = normalize_text(text, LEXICON)
    hits = INDEX.match_all(replica)
    by_id = {str(s.get("id")): s for s in CATALOG}
    slots_by_id = {}
    for sid, slots in hits:
        slots_by_id.setdefault(sid, slots)
    matched_own, matched_builtin = [], []
    for sid in slots_by_id:
        scenario = by_id.get(sid)
        if scenario is None:
            continue
        (matched_builtin if scenario.get("_origin") == "builtin" else matched_own).append(scenario)
    found = candidates(list(user_scenarios) + CATALOG, text=replica)
    own = [pair for pair in found if pair[0].get("_origin") != "builtin"]
    builtin = [pair for pair in found if pair[0].get("_origin") == "builtin"]
    ordered = (with_templates(own, matched_own) + with_templates(builtin, matched_builtin))
    executor = _executor()
    for scenario, reason in ordered:
        verdict, _ = dependencies_ok(scenario, executor)
        if verdict == "supported":
            return scenario.get("id"), reason, slots_by_id.get(scenario.get("id"))
    return None, "no_match", {}


# --- маршрут и слоты (по 3–5 фраз на сценарий) --------------------------------------

TABLE = [
    # таймер: fixed, цифры, слова, композит
    ("поставь таймер на полчаса", "timers.timer.set", {"duration_fixed": 1800}),
    ("установи таймер на 10 минут", "timers.timer.set", {"duration_number": 10, "duration_unit": 60}),
    ("заведи таймер на два часа пятнадцать минут", "timers.timer.set",
     {"duration_number": 2, "duration_unit": 3600, "duration_number2": 15, "duration_unit2": 60}),
    ("поставь таймер на 2 часа 15 минут", "timers.timer.set",
     {"duration_number": 2, "duration_unit": 3600, "duration_number2": 15, "duration_unit2": 60}),
    # громкость
    ("громкость на семь", "device.volume", {"number": 7}),
    ("громкость на 50", "device.volume", {"number": 50}),
    ("сделай громче", "device.volume", {"volume_mode": "up"}),
    ("выключи звук", "device.volume", {"volume_fixed": "mute"}),
    ("вруби на всю", "device.volume", {"volume_fixed": "max"}),
    # музыка: общий шаблон и шаблон с видом произведения
    ("включи майкла джексона", "media.music", {"query": "майкла джексона"}),
    ("давай послушаем что-нибудь новенькое", "media.music", {"query": "что нибудь новенькое"}),
    ("включи музыку", "media.music", {"media_object": "музыка"}),
    ("включи музыку джаз", "media.music", {"media_object": "музыка", "query": "джаз"}),
    ("запусти песню ромашки", "media.music", {"media_object": "песня", "query": "ромашки"}),
    # сказки и радио
    ("включи сказки", "media.tales", {}),
    ("расскажи сказку про колобка", "media.tales", {"query": "про колобка"}),
    ("включи радио", "media.radio", {}),
    ("включи радио европа плюс", "media.radio", {"query": "европа плюс"}),
    # плеер
    ("следующий трек", "media.player", {"player_command": "next"}),
    ("что играет", "media.player", {"player_command": "what"}),
    ("продолжай играть", "media.player", {"player_command": "resume"}),
    ("верни музыку", "media.player", {"player_command": "resume"}),
    # таймеры: отмена с порядковым, будильник со временем и датой
    ("выключи второй таймер", "timers.timer.cancel", {"ordinal": 2}),
    ("удали таймер", "timers.timer.cancel", {}),
    ("стоп таймер", "timers.timer.cancel", {}),
    ("будильник на 7:30 завтра", "timers.alarm.set",
     {"hour": 7, "minute": 30, "date_word": "завтра"}),
    ("разбуди меня в 7 утра", "timers.alarm.set", {"hour": 7, "day_period": "утра"}),
    ("поставь будильник на полночь", "timers.alarm.set", {"time_word": "00:00"}),
    ("будильник на 9 вечера в пятницу", "timers.alarm.set",
     {"hour": 9, "day_period": "вечера", "weekday": 5}),
    ("отмени будильник", "timers.alarm.cancel", {}),
    ("удали все будильники", "timers.alarm.cancel", {}),
    # свет, bluetooth, приложение, пауза, эмоции, время, погода, сброс
    ("включи свет на кухне", "smart_home.light", {"power": "on", "room": "кухня"}),
    ("свет включи", "smart_home.light", {"power": "on"}),
    ("потуши свет", "smart_home.light", {"power": "off"}),
    ("включи блютуз", "device.bluetooth.on", {}),
    ("выключи блютуз", "device.bluetooth.off", {}),
    ("открой ютуб", "device.app.open", {"app": "ютуб"}),
    ("запусти ютуб", "device.app.open", {"app": "ютуб"}),
    ("пауза", "device.player.pause", {}),
    ("останови музыку", "device.player.pause", {}),
    ("стоп", "device.player.pause", {}),
    ("покажи глазки", "device.led.emotion", {"led_emotion": "joy"}),
    ("подмигни мне", "device.led.emotion", {"led_emotion": "wink"}),
    ("сколько времени", "informers.clock", {}),
    ("который час", "informers.clock", {}),
    ("погода", "informers.weather", {}),
    ("какая погода в москве", "informers.weather", {"query": "в москве"}),
    ("забудь всё", "system.dialog.reset", {}),
]


@pytest.mark.parametrize("text,sid,slots", TABLE)
def test_template_route_and_slots(text, sid, slots):
    got_sid, reason, got_slots = full_route(text)
    assert (got_sid, reason) == (sid, "template"), text
    assert got_slots == slots, text


# --- прежние фразы не ушли в другие сценарии ----------------------------------------

LEGACY_EXACT = [
    "поставь таймер", "поставь будильник", "разбуди меня", "заведи будильник",
    "включи музыку", "включи песню", "включай музыку", "послушать музыку",
    "включи сказку", "расскажи сказку", "включи радио", "поставь радио",
    "следующий трек", "предыдущий", "дальше", "назад", "продолжи", "что играет",
    "громкость", "громче", "тише", "выключи звук", "максимальная громкость",
    "выключи музыку", "стоп музыка", "пауза",
    "включи свет", "выключи свет", "зажги свет", "потуши свет",
    "открой", "запусти", "отмени таймер", "отключи таймер", "стоп таймер",
    "отмени будильник", "удали будильник", "выключи будильник",
    "забудь всё", "начни заново", "сбрось диалог", "очисти контекст",
    "глазки", "покажи глазки", "подмигни",
    "сколько времени", "который час", "время",
    "погода", "какая погода", "погода на улице", "сколько градусов",
    "включи блютуз", "выключи bluetooth",
]

# вариации прежнего поведения без дословных фраз в каталоге (кросс-колонка, значения)
LEGACY_CASES = [
    ("поставь таймер на дуо 32 на минуту", "timers.timer.set"),
    ("таймер на 5 минут", "timers.timer.set"),
    ("будильник на 7:30", "timers.alarm.set"),
    ("поставь таймер на дуо 32", "timers.timer.set"),
]


@pytest.mark.parametrize("text", LEGACY_EXACT)
def test_legacy_phrases_keep_their_scenario(text):
    sid, _reason, _slots = full_route(text)
    assert sid is not None, text  # прежняя фраза по-прежнему маршрутизируется
    # сценарий тот же, что и раньше: у каждой прежней фразы единственный сценарий
    # каталога с этой фразой в phrase-триггере
    owners = [s["id"] for s in CATALOG
              if any(text == p for trigger in s.get("triggers", [])
                     if trigger.get("kind") == "phrase"
                     for p in [trigger.get("name") or "", *(trigger.get("phrases") or [])])]
    assert sid in owners, (text, sid, owners)


@pytest.mark.parametrize("text,sid", LEGACY_CASES)
def test_legacy_variations_keep_scenario(text, sid):
    got, _reason, _slots = full_route(text)
    assert got == sid, text


# --- пользовательский сценарий перебивает шаблон встроенного -------------------------

def test_user_phrase_beats_builtin_template():
    user = {"format_version": 2, "id": "agent.mine", "version": "1.0.0", "category": "custom",
            "triggers": [{"kind": "phrase", "phrases": ["включи гирлянду"]}],
            "flow": [{"id": "say", "op": "compose", "reply": {"text": "Готово"}},
                     {"id": "done", "op": "finish"}]}
    sid, reason, _slots = full_route("включи гирлянду", user_scenarios=[user])
    assert (sid, reason) == ("agent.mine", "phrase:exact")


# --- компиляция и валидатор ----------------------------------------------------------

def test_every_phrase_scenario_has_template():
    """У каждого встроенного фразового сценария есть хотя бы один template-триггер."""
    with_phrases = [s["id"] for s in CATALOG
                    if any(t.get("kind") == "phrase" for t in s.get("triggers", []))]
    with_templates = [s["id"] for s in CATALOG
                      if any(t.get("kind") == "template" for t in s.get("triggers", []))]
    assert set(with_phrases) <= set(with_templates)
    assert len(with_templates) >= 18


def test_all_builtin_templates_pass_check_template():
    for scenario in CATALOG:
        for trigger in scenario.get("triggers", []):
            if trigger.get("kind") == "template":
                check_template(trigger)


def test_normalize_keeps_nested_words_whole():
    """Слова с вложенным словом лексикона не режутся разглеем («полчаса» ≠ «пол час а»)."""
    assert deglue("полчаса", LEXICON) == "полчаса"
    assert normalize_text("поставь таймер на полчаса", LEXICON) == "поставь таймер на полчаса"
    assert deglue("поставьтаймер", LEXICON) == "поставь таймер"  # склейка по-прежнему делится
