"""TASK-018: SDK py-сценариев (server_emulator/sdk.py).

Построители отдают РОВНО ту структуру reply, которую несёт JSON-шаг compose, и
композятся в тот же ответ (сравнение байт сообщений reply_compose). ctx.action в
dry_run пишет effect и не трогает реестр; вне dry_run — вызывает. Сервисы —
обёртки над реестрами Executor (fetcher/плагины датасетов/лексиконы).
"""
import base64
import datetime
import pathlib
import re
from types import SimpleNamespace

import pytest

from server_emulator.scenario_engine.executor import Executor
from server_emulator.scenario_engine.reply_compose import compose_reply
from server_emulator.scenario_packages.validate import validate_scenario
from server_emulator.sdk import (SDK_VERSION, Ask, Event, SDKError, ScenarioContext,
                                 ask, card, directive, say, silent)
from server_emulator.state_store import StateStore
from tests.conftest import CTX, ROOT, step

SCHEMAS = ROOT / "server_emulator" / "schemas"


def runtime_of(executor):
    return SimpleNamespace(scenario_executor=executor)


def scenario_of(reply_spec):
    return validate_scenario({"format_version": 2, "id": "sdk.equiv", "version": "1.0.0",
                              "triggers": [{"kind": "phrase", "phrases": ["test"]}],
                              "flow": [step("c", "compose", reply=reply_spec), step("done", "finish")]})


async def compose_via_json(reply_spec):
    """Ответ JSON-шага compose — эталон, с которым сравниваются построители SDK."""
    executor = Executor(schemas_root=SCHEMAS)
    outcome = await executor.execute(scenario_of(reply_spec), CTX, "dlg-sdk")
    assert outcome.status == "finished" and len(outcome.composed) == 1
    ref, message = outcome.composed[0]
    assert ref == ("reply:service" if reply_spec.get("envelope") == "service" else "reply")
    return message


def _fixed(message):
    """Служебные поля, которые compose генерирует заново на каждый вызов (uuid/время)."""
    message.Event.AliceResponse.Header.ResponseId = "fixed"
    message.Timings.SendingTime.FromDatetime(datetime.datetime(2026, 1, 1))
    return message


# --- версия SDK -------------------------------------------------------------------


def test_sdk_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", SDK_VERSION)


# --- построители: байт-в-байт тот же ответ, что у JSON-шага compose -----------------


async def test_say_is_json_compose_reply():
    assert say("Включаю.") == {"text": "Включаю."}  # literal JSON {"reply": {"text": "Включаю."}}
    from_json = await compose_via_json({"text": "Включаю."})
    from_sdk = compose_reply(say("Включаю."), CTX, SCHEMAS)
    assert from_json.SerializeToString() == from_sdk.SerializeToString()


async def test_say_with_speech_and_parts_is_json_compose_reply():
    json_spec = {"text": "Сейчас +2.", "speech": "Сейчас плюс два.",
                 "speech_parts": ["Сейчас", "+2."]}
    from_json = await compose_via_json(json_spec)
    from_sdk = compose_reply(say("Сейчас +2.", speech="Сейчас плюс два.",
                                 speech_parts=["Сейчас", "+2."]), CTX, SCHEMAS)
    assert from_json.SerializeToString() == from_sdk.SerializeToString()
    assert from_sdk.Event.AliceResponse.VoiceResponse.OutputSpeech.Text == "Сейчас плюс два."


async def test_card_is_json_compose_reply():
    assert card("Длинный текст прогноза") == {"text": "Длинный текст прогноза", "card": True}
    from_json = await compose_via_json({"text": "Длинный текст прогноза"})  # card: true по умолчанию
    from_sdk = compose_reply(card("Длинный текст прогноза"), CTX, SCHEMAS)
    assert from_json.SerializeToString() == from_sdk.SerializeToString()
    assert from_sdk.Event.AliceResponse.Response.Cards[0].Type == "simple_text"


async def test_directive_is_json_compose_reply():
    json_spec = {"text": "", "card": False,
                 "directives": [{"name": "sound_set_level", "analytics_type": "sound_set_level",
                                 "payload": {"new_level": 0.5}}]}
    from_json = await compose_via_json(json_spec)
    from_sdk = compose_reply(directive("sound_set_level", payload={"new_level": 0.5},
                                       analytics_type="sound_set_level"), CTX, SCHEMAS)
    assert directive("sound_set_level", payload={"new_level": 0.5}) == \
        {"text": "", "card": False, "directives": [{"name": "sound_set_level", "payload": {"new_level": 0.5}}]}
    assert from_json.SerializeToString() == from_sdk.SerializeToString()
    built = from_sdk.Event.AliceResponse.Response.Directives[0]
    assert built.Name == "sound_set_level" and built.Type == "client_action"
    assert built.Payload["new_level"] == 0.5


async def test_directive_payload_raw_is_json_compose_reply():
    raw = base64.b64encode(b"\x10\x02payload").decode()
    from_json = await compose_via_json({"text": "", "card": False,
                                        "directives": [{"name": "debug_raw", "payload_raw": raw, "debug": True}]})
    from_sdk = compose_reply(directive("debug_raw", payload_raw=raw), CTX, SCHEMAS)
    assert from_json.SerializeToString() == from_sdk.SerializeToString()
    assert from_sdk.Event.AliceResponse.Response.Directives[0].PayloadRaw == b"\x10\x02payload"


async def test_silent_is_bare_service_reply():
    assert silent() == {"envelope": "service", "text": "", "card": False}
    from_json = await compose_via_json({"envelope": "service", "text": "", "card": False})
    from_sdk = compose_reply(silent(), CTX, SCHEMAS)
    assert _fixed(from_json).SerializeToString() == _fixed(from_sdk).SerializeToString()
    for message in (from_json, from_sdk):  # bare: без карточки, голоса и голосовых добавок
        response = message.Event.AliceResponse
        assert not response.Response.Cards and not response.Response.Directives
        assert response.VoiceResponse.OutputSpeech.Text == ""
        assert response.VoiceResponse.HasVoiceResponse is False
        assert response.Response.Alice2EffectiveSettings.HasField("EffectiveSettings")
        assert message.Timings.SendingTime.seconds > 0


def test_ask_returns_descriptor_for_dialog_continuation():
    question = ask("На сколько поставить таймер?", then="on_duration", expect="duration")
    assert isinstance(question, Ask)
    assert question.question == "На сколько поставить таймер?"
    assert question.then == "on_duration" and question.expect == "duration"
    assert ask("Да?", then="on_yes").expect is None


# --- контекст сценария --------------------------------------------------------------


def test_context_fields_come_from_request_ctx():
    inner = {"input": {"text": "включи свет", "text_raw": "Алиса, включи свет", "sn": "SN-1"},
             "device": {"sn": "SN-1", "name": "Дуо", "room": "кухня"},
             "user": {"id": "owner", "name": "O", "location": {"latitude": 55.7}},
             "params": {"device": {"room": "кухня"}, "effective": {"timezone": "Asia/Tashkent"}},
             "slots": {"target": {"sn": "SN-2"}},
             "state": {"hits": 1}}
    ctx = ScenarioContext(runtime_of(Executor()), inner)
    assert ctx.text == "включи свет"
    assert ctx.raw_text == "Алиса, включи свет"
    assert ctx.sn == "SN-1"
    assert ctx.device == {"sn": "SN-1", "name": "Дуо", "room": "кухня"}
    assert ctx.owner == {"id": "owner", "name": "O", "location": {"latitude": 55.7}}
    assert ctx.params["effective"]["timezone"] == "Asia/Tashkent"
    assert ctx.slots == {"target": {"sn": "SN-2"}}


def test_state_and_slots_are_live_dicts_of_execution():
    inner = {"input": {"text": "привет"}, "state": {"hits": 1}, "slots": {"n": 2}}
    ctx = ScenarioContext(runtime_of(Executor()), inner)
    assert ctx.state is inner["state"] and ctx.slots is inner["slots"]
    ctx.state["hits"] += 1  # правки сценария переживают шаги диалога (TASK-020)
    ctx.slots["n"] = 3
    assert inner["state"] == {"hits": 2} and inner["slots"] == {"n": 3}


def test_event_fields_from_service_request():
    inner = {"input": {"event_name": "@@mm_typed_callback", "callback": "NAlice.TGetInformersTypedCallback",
                       "purpose": "get_informers", "frame": "weather_semantic_frame"}}
    ctx = ScenarioContext(runtime_of(Executor()), inner)
    assert ctx.event == Event(name="@@mm_typed_callback", callback="NAlice.TGetInformersTypedCallback",
                              purpose="get_informers", frame="weather_semantic_frame")
    assert ScenarioContext(runtime_of(Executor()), {"input": {"text": "привет"}}).event == Event()


def test_now_is_aware_datetime_in_device_timezone():
    ctx = ScenarioContext(runtime_of(Executor()),
                          {"clock": {"now": datetime.datetime(2026, 10, 30, 9, 0,
                                                              tzinfo=datetime.timezone.utc)},
                           "params": {"device": {"timezone": "Asia/Tashkent"}}})
    assert ctx.now.utcoffset() == datetime.timedelta(hours=5)
    assert ctx.now.hour == 14
    # цепочка device → user → effective → input.timezone
    ctx = ScenarioContext(runtime_of(Executor()),
                          {"clock": {"now": datetime.datetime(2026, 10, 30, 9, 0,
                                                              tzinfo=datetime.timezone.utc)},
                           "params": {"user": {"timezone": "Europe/Moscow"}}})
    assert ctx.now.utcoffset() == datetime.timedelta(hours=3)
    ctx = ScenarioContext(runtime_of(Executor()),
                          {"clock": {"now": datetime.datetime(2026, 10, 30, 9, 0,
                                                              tzinfo=datetime.timezone.utc)},
                           "input": {"timezone": "Europe/Moscow"}})
    assert ctx.now.utcoffset() == datetime.timedelta(hours=3)
    # без имени таймзоны — aware в таймзоне сервера
    naive = ScenarioContext(runtime_of(Executor()),
                            {"clock": {"now": datetime.datetime(2026, 10, 30, 9, 0)}})
    assert naive.now.tzinfo is not None
    garbage = ScenarioContext(runtime_of(Executor()),
                              {"clock": {"now": datetime.datetime(2026, 10, 30, 9, 0)},
                               "params": {"device": {"timezone": "Марс/Олимп"}}})
    assert garbage.now.tzinfo is not None


# --- сервисы: действия реестра Executor ----------------------------------------------


async def test_action_outside_dry_run_calls_registry():
    calls = []

    def timer_start(arguments, execution):
        calls.append((dict(arguments), (execution.ctx.get("input") or {}).get("sn")))
        return {"id": "t1", "seconds": arguments["seconds"]}

    async def device_push(arguments, execution):
        calls.append((arguments["action"], "async"))
        return "sent"

    executor = Executor(actions={"timer.start": timer_start, "device.push": device_push})
    ctx = ScenarioContext(runtime_of(executor), {"input": {"sn": "SN-1"}})
    assert await ctx.action("timer.start", seconds=90) == {"id": "t1", "seconds": 90}
    assert await ctx.action("device.push", action="light", arguments={"on": True}) == "sent"
    assert calls == [({"seconds": 90}, "SN-1"), ("light", "async")]


async def test_action_in_dry_run_records_effect_without_registry():
    calls = []

    def timer_start(arguments, execution):
        calls.append(dict(arguments))
        return {"id": "t1"}

    executor = Executor(actions={"timer.start": timer_start})
    ctx = ScenarioContext(runtime_of(executor), dict(CTX), dry_run=True)
    assert ctx.dry_run is True
    assert await ctx.action("timer.start", seconds=90) is None
    assert calls == []  # реестр не тронут — проверка сценария без эффектов
    assert ctx.effects == [{"kind": "action", "name": "timer.start", "arguments": {"seconds": 90}}]


async def test_unknown_action_raises_honest_error():
    ctx = ScenarioContext(runtime_of(Executor()), dict(CTX))
    with pytest.raises(SDKError):
        await ctx.action("ghost.action")


# --- сервисы: fetch/датасеты/лексиконы ------------------------------------------------


async def test_fetch_json_goes_through_engine_fetcher():
    calls = []

    async def fetcher(url, query, ttl):
        calls.append((url, dict(query), ttl))
        return {"ok": True}

    ctx = ScenarioContext(runtime_of(Executor(fetcher=fetcher)), dict(CTX))
    assert await ctx.fetch_json("https://example.test/x", ttl=60) == {"ok": True}
    assert calls == [("https://example.test/x", {}, 60)]


async def test_fetch_json_without_fetcher_raises():
    ctx = ScenarioContext(runtime_of(Executor()), dict(CTX))
    with pytest.raises(SDKError):
        await ctx.fetch_json("https://example.test/x")


async def test_dataset_goes_to_plugin_and_executor_persists(tmp_path):
    calls = []

    async def weather(key, args):
        calls.append((key, dict(args)))
        return {"temp": 5}

    store = StateStore(tmp_path / "emulator.sqlite3")
    executor = Executor(plugins={"weather": weather}, data_store=store)
    ctx = ScenarioContext(runtime_of(executor), dict(CTX))
    value = await ctx.dataset("weather", "55.7,37.6", max_age=600, args={"query": {"latitude": "55.7"}})
    assert value == {"temp": 5}
    assert calls == [("55.7,37.6", {"query": {"latitude": "55.7"}})]
    # запись в БД сделана на стороне executor (как шаг refresh): повтор — из БД, плагин спит
    assert await ctx.dataset("weather", "55.7,37.6", max_age=600) == {"temp": 5}
    assert len(calls) == 1


async def test_dataset_without_plugin_returns_stale_record_or_raises(tmp_path):
    async def weather(key, args):
        return {"temp": 5}

    store = StateStore(tmp_path / "emulator.sqlite3")
    ctx = ScenarioContext(runtime_of(Executor(plugins={"weather": weather}, data_store=store)), dict(CTX))
    await ctx.dataset("weather", "55.7,37.6")
    stale = ScenarioContext(runtime_of(Executor(data_store=store)), dict(CTX))  # плагина больше нет
    assert await stale.dataset("weather", "55.7,37.6", max_age=3600) == {"temp": 5}
    with pytest.raises(SDKError):
        await stale.dataset("weather", "нет такой записи")


def test_lexicon_returns_common_lexicon():
    ctx = ScenarioContext(runtime_of(Executor(lexicons={"rooms": ["кухня", "гостиная"]})), dict(CTX))
    assert ctx.lexicon("rooms") == ["кухня", "гостиная"]  # список — данные, не код
    assert ctx.lexicon("ghost") == {}


def test_lexicon_prefers_scenario_own_lexicon():
    executor = Executor(lexicons={"rooms": ["кухня"]})
    ctx = ScenarioContext(runtime_of(executor), {**CTX, "lexicon": {"rooms": {"на кухне": "кухня"}}})
    assert ctx.lexicon("rooms") == {"на кухне": "кухня"}


def test_context_without_runtime_services_raises():
    ctx = ScenarioContext(SimpleNamespace(), dict(CTX))
    with pytest.raises(SDKError):
        ctx.lexicon("rooms")


# --- SDK 1.1.0 (TASK-050): устройства, парсеры, форматтеры, датасеты, ресурсы ---------------

from server_emulator.scenario_engine.bindings import expand  # noqa: E402
from server_emulator.scenario_engine.library import LEXICONS  # noqa: E402
from server_emulator.sdk import format_duration, format_hhmm, plural, time_ru  # noqa: E402

HOME = {"devices": [
    {"sn": "SN-1", "name": "Дуо 32", "aliases": ["кухонная колонка"], "current": True},
    {"sn": "SN-2", "name": "Мини", "aliases": ["спальня"], "current": False}]}


def ctx_of(text="", executor=None, **inner):
    base = {"input": {"text": text}, "clock": {"now": datetime.datetime(2026, 10, 30, 21, 7)},
            "lexicon": dict(LEXICONS), **inner}
    return ScenarioContext(runtime_of(executor or Executor(lexicons=LEXICONS)), base)


def test_sdk_version_is_1_1_0():
    assert SDK_VERSION == "1.1.0"


def test_devices_come_from_home_and_are_read_only_copy():
    ctx = ctx_of(home=HOME)
    assert ctx.devices == HOME["devices"]
    ctx.devices[0]["name"] = "испорчено"  # копия: контекст запроса не меняется
    ctx.devices.append({})
    assert HOME["devices"][0]["name"] == "Дуо 32" and len(ctx.devices) == 2
    assert ctx_of().devices == []


def test_find_device_by_name_alias_and_remainder():
    ctx = ctx_of("поставь таймер на кухонная колонка пять минут", home=HOME)
    assert ctx.find_device(fields=["name", "aliases"], contains_only=True)["sn"] == "SN-1"
    assert ctx.find_device("включи мини")["sn"] == "SN-2"  # field=name по умолчанию
    assert ctx.find_device("дуо")["sn"] == "SN-1"  # без contains_only: остаток текста входит в имя
    assert ctx.find_device("дуо", contains_only=True) is None
    assert ctx.find_device("поставь таймер", contains_only=True) is None
    assert ctx_of("мини").find_device() is None  # нет home.devices


def test_find_in_arbitrary_list_with_lexicon_commands():
    apps = [{"name": "YouTube"}, {"name": "Кинопоиск"}]
    ctx = ctx_of("открой приложение кинопоиск", device={"apps": apps},
                 lexicon={"commands": ["открой", "открой приложение"]})
    assert ctx.find_in(ctx.device["apps"], lexicon="commands") == {"name": "Кинопоиск"}
    assert ctx.find_in(apps, "открой youtube", lexicon=["открой"]) == {"name": "YouTube"}
    assert ctx.find_in(apps, "открой нетфликс", lexicon=["открой"]) is None


def test_parsers_match_engine_parsers():
    assert ctx_of("на пять минут").parse_duration() == 300
    assert ctx_of("на полчаса").parse_duration() == 1800
    assert ctx_of("ничего").parse_duration() is None
    assert ctx_of("на семь тридцать").parse_time() == "07:30"  # формат движка ЧЧ:ММ
    assert ctx_of("").parse_time("разбуди в 8:05") == "08:05"
    assert ctx_of("громкость пять").parse_number(minimum=0, maximum=10) == 5
    assert ctx_of("громкость 50").parse_number() == 50
    assert ctx_of("громкость 50").parse_number(maximum=10) is None
    assert ctx_of("").parse_number("двадцать пять") == 25


def test_parse_strip_removes_target_aliases_from_text():
    ctx = ctx_of("поставь таймер на кухонная колонка пять минут")
    assert ctx.parse_duration(strip=["кухонная колонка"]) == 300
    assert ctx.parse_duration(strip="кухонная колонка") == 300
    assert ctx_of("будильник спальня восемь тридцать").parse_time(strip=["спальня"]) == "08:30"


def test_parse_lexicon_and_rest_by_name_or_inline():
    own = {"power": {"включи": "on", "выключи": "off"}, "commands": ["открой", "запусти"]}
    ctx = ctx_of("выключи свет", lexicon=own)
    assert ctx.parse_lexicon("power") == "off"  # имя раздела lexicon сценария
    assert ctx.parse_lexicon({"свет": "light"}) == "light"  # inline
    assert ctx.parse_lexicon("power", "привет") is None
    assert ctx_of("открой Ютуб", lexicon=own).parse_rest("commands") == "ютуб"
    assert ctx_of("Ютуб").parse_rest(["открой"]) is None
    # общий словарь движка: имя без раздела сценария
    shared = ScenarioContext(runtime_of(Executor(lexicons={"rooms": {"кухн*": "kitchen"}})),
                             {"input": {"text": "свет на кухне"}})
    assert shared.parse_lexicon("rooms") == "kitchen"
    assert shared.parse_lexicon("rooms", "в спальне") is None


def test_input_timezone():
    assert ctx_of(input={"timezone": " Asia/Tashkent "}).input_timezone == "Asia/Tashkent"
    assert ctx_of(input={"text": "x"}).input_timezone is None
    assert ScenarioContext(runtime_of(Executor()), {}).input_timezone is None


def test_formatters_equal_template_filters():
    for seconds in (5, 60, 90, 300, 3600, 5400, 11101):
        assert format_duration(seconds) == expand("{{x|duration}}", {"x": seconds})
    assert format_duration(90) == "1 минуту 30 секунд" and format_duration(5400) == "1 час 30 минут"
    for value in ("07:30", "7:30", "12:05", "00:00"):
        assert format_hhmm(value) == expand("{{x|hhmm}}", {"x": value})
    assert format_hhmm("07:30") == "7:30"
    for n in (1, 2, 5, 11, 21, 24):
        assert plural(n, ("минуту", "минуты", "минут")) == expand(
            "{{x|plural:минуту,минуты,минут}}", {"x": n})
        assert plural(n, "минуту,минуты,минут") == plural(n, ["минуту", "минуты", "минут"])
    assert plural(5, ("минуту", "минуты", "минут")) == "5 минут"
    now = datetime.datetime(2026, 10, 30, 12, 5)
    assert time_ru(now) == expand("{{clock|time_ru}}", {"clock": {"now": now}}) == "12 часов 5 минут"
    assert time_ru({"now": datetime.datetime(2026, 10, 30, 12, 0)}) == "12 часов"
    with pytest.raises(ValueError):
        format_duration(0)
    with pytest.raises(ValueError):
        format_hhmm("полвосьмого")


# --- dataset_record ---------------------------------------------------------------------


def age_record(store, name, key, seconds):
    """Состарить запись датасета на seconds секунд (прямая правка updated_ts в БД)."""
    import time
    db = store._live()
    with db:
        db.execute("UPDATE scenario_data SET updated_ts=? WHERE scenario_id=? AND key=?",
                   (time.time() - seconds, "dataset:" + name, key))


async def test_dataset_record_fresh_record_has_no_plugin_call(tmp_path):
    calls = []

    async def weather(key, args):
        calls.append(key)
        return {"temp": 5}

    store = StateStore(tmp_path / "emulator.sqlite3")
    ctx = ScenarioContext(runtime_of(Executor(plugins={"weather": weather}, data_store=store)), dict(CTX))
    first = await ctx.dataset_record("weather", "k", max_age=600)
    assert calls == ["k"] and first["value"] == {"temp": 5}
    assert first["fresh"] is True and first["stale"] is False and first["age_s"] <= 1
    second = await ctx.dataset_record("weather", "k", max_age=600)
    assert calls == ["k"]  # свежая запись из БД — плагин не будили
    assert set(second) == {"value", "updated_at", "age_s", "fresh", "stale"}


async def test_dataset_record_stale_record_is_refreshed(tmp_path):
    values = iter([{"temp": 1}, {"temp": 2}])

    async def weather(key, args):
        return next(values)

    store = StateStore(tmp_path / "emulator.sqlite3")
    ctx = ScenarioContext(runtime_of(Executor(plugins={"weather": weather}, data_store=store)), dict(CTX))
    await ctx.dataset_record("weather", "k")
    age_record(store, "weather", "k", 4000)
    # max_age=0 — любая запись свежа (как у шага load): плагин не зовётся
    assert (await ctx.dataset_record("weather", "k"))["value"] == {"temp": 1}
    record = await ctx.dataset_record("weather", "k", max_age=3600)
    assert record["value"] == {"temp": 2} and record["fresh"] is True and record["age_s"] <= 1


async def test_dataset_record_plugin_failure_falls_back_to_old_record(tmp_path):
    state = {"fail": False}

    async def weather(key, args):
        if state["fail"]:
            raise RuntimeError("сеть недоступна")
        return {"temp": 7}

    store = StateStore(tmp_path / "emulator.sqlite3")
    ctx = ScenarioContext(runtime_of(Executor(plugins={"weather": weather}, data_store=store)), dict(CTX))
    await ctx.dataset_record("weather", "k")
    age_record(store, "weather", "k", 4000)
    state["fail"] = True
    record = await ctx.dataset_record("weather", "k", max_age=3600)
    assert record["value"] == {"temp": 7} and record["stale"] is True and record["fresh"] is False
    assert 3999 <= record["age_s"] <= 4005
    # без старой записи сбой — None (сценарий отвечает фразой сбоя), не исключение
    assert await ctx.dataset_record("weather", "другая запись", max_age=3600) is None


async def test_dataset_record_missing_plugin(tmp_path):
    store = StateStore(tmp_path / "emulator.sqlite3")
    ctx = ScenarioContext(runtime_of(Executor(data_store=store)), dict(CTX))
    with pytest.raises(SDKError):
        await ctx.dataset_record("weather", "k")
    store.scenario_data_set("dataset:weather", "system", "", "k", {"temp": 3})
    age_record(store, "weather", "k", 4000)
    record = await ctx.dataset_record("weather", "k", max_age=3600)
    assert record["value"] == {"temp": 3} and record["stale"] is True  # плагина нет — старая запись


async def test_dataset_record_parallel_calls_share_one_refresh(tmp_path):
    import asyncio
    calls = []

    async def weather(key, args):
        calls.append(key)
        await asyncio.sleep(0.2)
        return {"temp": 9}

    store = StateStore(tmp_path / "emulator.sqlite3")
    executor = Executor(plugins={"weather": weather}, data_store=store)
    ctx = ScenarioContext(runtime_of(executor), dict(CTX))
    first, second = await asyncio.gather(ctx.dataset_record("weather", "k", max_age=60),
                                         ctx.dataset_record("weather", "k", max_age=60))
    assert len(calls) == 1  # параллельный вызов ждёт то же обновление (_refreshing)
    assert first["value"] == second["value"] == {"temp": 9}
    assert executor._refreshing == {}


async def test_dataset_record_timeout_keeps_plugin_running_in_background(tmp_path):
    import asyncio
    calls = []

    async def weather(key, args):
        calls.append(key)
        await asyncio.sleep(0.4)
        return {"temp": 11}

    store = StateStore(tmp_path / "emulator.sqlite3")
    executor = Executor(plugins={"weather": weather}, data_store=store)
    ctx = ScenarioContext(runtime_of(executor), dict(CTX))
    assert await ctx.dataset_record("weather", "k", max_age=60, timeout=0.1) is None  # нет старой записи
    await asyncio.sleep(0.5)  # плагин дорабатывает в фоне, запись появляется к следующему запросу
    record = await ctx.dataset_record("weather", "k", max_age=60, timeout=0.1)
    assert record["value"] == {"temp": 11} and record["fresh"] is True and calls == ["k"]
    # таймаут при старой записи — откат на неё
    age_record(store, "weather", "k", 4000)
    stale = await ctx.dataset_record("weather", "k", max_age=3600, timeout=0.1)
    assert stale["value"] == {"temp": 11} and stale["stale"] is True


async def test_dataset_record_passes_args_and_works_without_store():
    seen = []

    async def weather(key, args):
        seen.append((key, args))
        return {"temp": 1}

    ctx = ScenarioContext(runtime_of(Executor(plugins={"weather": weather})), dict(CTX))
    record = await ctx.dataset_record("weather", "k", args={"url": "https://x"})
    assert seen == [("k", {"url": "https://x"})] and record["value"] == {"temp": 1}


async def test_executor_refresh_dataset_shares_one_refresh_with_sdk(tmp_path):
    """Executor.refresh_dataset и SDK делят одну задачу: параллельные запуски — одно обновление."""
    import asyncio
    calls = []

    async def weather(key, args):
        calls.append(key)
        await asyncio.sleep(0.1)
        return {"temp": 4}

    store = StateStore(tmp_path / "emulator.sqlite3")
    executor = Executor(plugins={"weather": weather}, data_store=store)
    sdk_ctx = ScenarioContext(runtime_of(executor), dict(CTX))
    value, record = await asyncio.gather(executor.refresh_dataset("weather", "k"),
                                         sdk_ctx.dataset_record("weather", "k", max_age=60))
    assert value == {"temp": 4} and len(calls) == 1 and record["value"] == {"temp": 4}


# --- asset ------------------------------------------------------------------------------


def test_asset_reads_json_from_assets_dir(tmp_path):
    assets = tmp_path / "weather.assets"
    assets.mkdir()
    (assets / "card.json").write_text('{"templates": {"a": 1}, "заголовок": "Погода"}', encoding="utf-8")
    ctx = ScenarioContext(runtime_of(Executor()), dict(CTX), assets_dir=assets)
    assert ctx.asset("card") == {"templates": {"a": 1}, "заголовок": "Погода"}
    assert ctx.asset("card.json") == ctx.asset("card")


def test_asset_rejects_traversal_and_missing(tmp_path):
    assets = tmp_path / "weather.assets"
    assets.mkdir()
    (tmp_path / "secret.json").write_text("{}", encoding="utf-8")
    (assets / "broken.json").write_text("{не json", encoding="utf-8")
    ctx = ScenarioContext(runtime_of(Executor()), dict(CTX), assets_dir=assets)
    for bad in ("../secret", "..", "a/b", "a\\b", "", "C:card", "/etc/passwd"):
        with pytest.raises(SDKError):
            ctx.asset(bad)
    with pytest.raises(SDKError):
        ctx.asset("нет")
    with pytest.raises(SDKError):
        ctx.asset("broken")
    with pytest.raises(SDKError):
        ScenarioContext(runtime_of(Executor()), dict(CTX)).asset("card")  # каталога ресурсов нет


def script_scenario_of(sid):
    return validate_scenario({"format_version": 2, "id": sid, "version": "1.0.0",
                              "triggers": [{"kind": "phrase", "phrases": ["test"]}],
                              "executor": {"kind": "script"}})


async def test_executor_passes_assets_dir_next_to_script(tmp_path):
    section = tmp_path / "informers"
    (section / "card.assets").mkdir(parents=True)
    (section / "card.assets" / "layout.json").write_text('{"k": 1}', encoding="utf-8")
    script = section / "card.py"
    script.write_text("from server_emulator.sdk import say\n\n\n"
                      "async def handle(ctx):\n    return say(str(ctx.asset('layout')['k']))\n",
                      encoding="utf-8")
    executor = Executor(schemas_root=SCHEMAS, script_path=lambda scenario: script)
    outcome = await executor.execute(script_scenario_of("sdk.asset"), {"input": {}}, "dlg-asset")
    assert outcome.status == "finished" and len(outcome.composed) == 1


# --- return [] = тихое завершение ---------------------------------------------------------


async def test_script_returning_empty_list_finishes_silently(tmp_path):
    script = tmp_path / "quiet.py"
    script.write_text("async def handle(ctx):\n    return []\n", encoding="utf-8")
    executor = Executor(schemas_root=SCHEMAS, script_path=lambda scenario: script)
    scenario = script_scenario_of("sdk.quiet")
    outcome = await executor.execute(scenario, {"input": {}}, "dlg-quiet")
    assert outcome.status == "finished" and outcome.composed == [] and outcome.error is None
    # None — отказ, а не тихое завершение
    script.write_text("async def handle(ctx):\n    return None\n", encoding="utf-8")
    declined = await executor.execute(scenario, {"input": {}}, "dlg-quiet-2")
    assert declined.status == "declined"
