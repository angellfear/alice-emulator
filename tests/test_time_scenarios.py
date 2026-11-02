"""TASK-079: таймеры/будильники по директивам настоящего сервера.

station_state (разбор EnvironmentStateRaw), остаток/активные/сигнал, и WS-сценарии с
подменённым состоянием колонки: установка/отмена/остаток таймера, будильник, «хватит»
при сигнале. Порядок директив, type/is_led_silent и payload каждого ответа сверяются
с обезличенными образцами настоящего сервера tests/fixtures/directives/*.json
(журнал 2026-10-22…27); значения-плейсхолдеры "<…>" проверяются на «присутствует и
не пусто», остальное — буквально.
"""
import datetime
import json
import re
import time
from pathlib import Path

import pytest
from google.protobuf import json_format

from server_emulator import station_state
from server_emulator.runtime import Runtime
from server_emulator.schema import load_type, schemas_dir, station_file
from server_emulator.scenario_engine.library import LEXICONS
from server_emulator.scenario_engine.parsers import parse_duration
from server_emulator.sdk import format_duration
from server_emulator.server import create_app
from tests.conftest import _open_session, listen

TIMER_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{8}-[0-9a-f]{8}-[0-9a-f]{8}$")
TIMER_A = "aaaaaaaa-bbbbbbbb-cccccccc-dddddddd"
TIMER_B = "11111111-22222222-33333333-44444444"
ALARM_A = "eeeeeeee-ffffffff-00000000-11111111"

TIMER_SET = "NAlice.TAlarmCapability.TTimerSetDirective"
TIMER_CANCEL = "NAlice.TAlarmCapability.TTimerCancelDirective"
TIMER_STOP = "NAlice.TAlarmCapability.TTimerStopDirective"
ALARM_ADD = "NAlice.TAlarmCapability.TAlarmAddDirective"
ALARM_STOP = "NAlice.TAlarmCapability.TAlarmStopDirective"
OPEN_LIST = "NAlice.TClockAppCapability.TOpenScreenTimerListDirective"
OPEN_INFO = "NAlice.TClockAppCapability.TOpenScreenTimerInfoDirective"
OPEN_ALARM_LIST = "NAlice.TClockAppCapability.TOpenScreenAlarmListDirective"
CLOSE_ALARM = "NAlice.TClockAppCapability.TCloseScreenAlarmActiveDirective"
CLOSE_TIMER = "NAlice.TClockAppCapability.TCloseScreenTimerActiveDirective"


def _env_type():
    return load_type(schemas_dir(), "alice-api.pb", "NAlice.NAliceApi.TEnvironmentState")


def _station_type(full_name):
    return load_type(schemas_dir(), station_file(), full_name)


def _state_raw(timers=(), alarms=(), screens=None, volume=None):
    """EnvironmentStateRaw как у колонки: cap 21 (TAlarmCapability.State) + cap 144 (экраны)
    + cap 23 (TVolumeCapability.State, TASK-083).

    timers: [{"timer_id","start_ts","duration","disabled"?,"currently_playing"?,"paused"?}]
    alarms: [{"alarm_id","hours","minutes"?,"disabled"?,"currently_playing"?}]
    screens: {"timer_list": True, "alarm_active": <alarm_id>, ...}
    volume: {"level","max_level","percent_level"?,"muted"?} (журнал: Level 4, MaxLevel 10,
    PercentLevel 40.0 — процент считает колонка)
    """
    env = _env_type()()
    endpoint = env.Endpoints.add()
    endpoint.Id = "dev-t"
    if timers or alarms:
        cap = _station_type("NAlice.TAlarmCapability")()
        for timer in timers:
            item = cap.State.Timers.add()
            item.TimerId = timer["timer_id"]
            item.StartTimestamp = int(timer["start_ts"])
            item.Duration = int(timer["duration"])
            item.Remaining = int(timer["duration"])
            item.Disabled = bool(timer.get("disabled"))
            item.CurrentlyPlaying = bool(timer.get("currently_playing"))
            item.Paused = bool(timer.get("paused"))
        for alarm in alarms:
            item = cap.State.Alarms.add()
            item.AlarmId = alarm["alarm_id"]
            item.LocalTime.Hours = int(alarm["hours"])
            item.LocalTime.Minutes = int(alarm.get("minutes") or 0)
            item.Disabled = bool(alarm.get("disabled"))
            item.CurrentlyPlaying = bool(alarm.get("currently_playing"))
        holder = endpoint.AnyCapabilities.add()
        holder.CapabilityType = station_state.ALARM_CAPABILITY
        holder.RawCapability = cap.SerializeToString()
    if screens:
        clock = _station_type("NAlice.TClockAppCapability")()
        if screens.get("timer_list"):
            # presence пустого сообщения теряется при сериализации рядом с другими полями —
            # у реальных экранов всегда есть содержимое
            clock.State.TimerListScreenState.TimerDuration = 60
        if screens.get("timer_info"):
            clock.State.TimerInfoScreenState.TimerId = screens["timer_info"]
        if screens.get("timer_active"):
            clock.State.TimerActiveScreenState.TimerId = screens["timer_active"]
        if screens.get("alarm_list"):
            clock.State.AlarmListScreenState.SetInParent()
        if screens.get("alarm_active"):
            clock.State.AlarmActiveScreenState.AlarmId = screens["alarm_active"]
        holder = endpoint.AnyCapabilities.add()
        holder.CapabilityType = station_state.CLOCK_APP_CAPABILITY
        holder.RawCapability = clock.SerializeToString()
    if volume:
        cap = _station_type("NAlice.TVolumeCapability")()
        cap.State.Level = int(volume.get("level") or 0)
        cap.State.MaxLevel = int(volume["max_level"])
        cap.State.PercentLevel = float(volume.get("percent_level") or
                                       100.0 * cap.State.Level / cap.State.MaxLevel)
        cap.State.IsMuted = bool(volume.get("muted"))
        holder = endpoint.AnyCapabilities.add()
        holder.CapabilityType = station_state.VOLUME_CAPABILITY
        holder.RawCapability = cap.SerializeToString()
    return env.SerializeToString()


# --- парсер длительностей (остался из снятого test_timers) --------------------------


@pytest.mark.parametrize("phrase,seconds", [
    ("поставь таймер на 5 минут", 300),
    ("таймер на десять секунд", 10),
    ("установи таймер на полтора часа", 5400),
    ("таймер на полчаса", 1800),
])
def test_parse_duration_ru(phrase, seconds):
    assert parse_duration(phrase, LEXICONS["duration"]) == seconds


# --- station_state: разбор состояния из запроса ------------------------------------


def test_station_state_parse_and_active():
    now = time.time()
    raw = _state_raw(timers=[{"timer_id": TIMER_A, "start_ts": now - 2866, "duration": 3600},
                             {"timer_id": TIMER_B, "start_ts": now, "duration": 60, "disabled": True}],
                    alarms=[{"alarm_id": ALARM_A, "hours": 7, "minutes": 30}])
    state = station_state.parse(raw)
    assert [t["timer_id"] for t in state["timers"]] == [TIMER_A, TIMER_B]
    assert state["alarms"][0]["time"] == {"hours": 7, "minutes": 30, "seconds": 0}
    assert state["alarms"][0]["currently_playing"] is False
    live = station_state.active_timers(state["timers"], now)
    assert [t["timer_id"] for t in live] == [TIMER_A]
    assert 733 <= live[0]["remaining"] <= 734  # Duration − (now − StartTimestamp), как в образце
    assert station_state.ringing(state, now) is None


def test_station_state_ringing():
    now = time.time()
    ringing_alarm = {"alarm_id": ALARM_A, "hours": 7, "currently_playing": True}
    expired = {"timer_id": TIMER_A, "start_ts": now - 100, "duration": 60}
    assert station_state.ringing({"timers": [], "alarms": [ringing_alarm]}, now) == ("alarm", ALARM_A)
    assert station_state.ringing({"timers": [expired], "alarms": []}, now) == ("timer", TIMER_A)
    fresh = {"timer_id": TIMER_B, "start_ts": now, "duration": 300}
    assert station_state.ringing({"timers": [fresh], "alarms": []}, now) is None
    assert station_state.parse(b"not-a-protobuf-bytes") == {"timers": [], "alarms": [], "screens": {},
                                                            "player": {}, "volume": {}}


def test_station_state_screens():
    # экраны в TState в одном oneof — у колонки открыт ровно один экран
    state = station_state.parse(_state_raw(screens={"timer_list": True}))
    assert state["screens"] == {"timer_list": True}
    state = station_state.parse(_state_raw(screens={"alarm_active": ALARM_A}))
    assert state["screens"] == {"alarm_active": ALARM_A}


def test_station_state_volume_cap23():
    """TASK-083: cap 23 TVolumeCapability.State (есть в каждом запросе, журнал 10-24…29)."""
    state = station_state.parse(_state_raw(volume={"level": 4, "max_level": 10}))
    assert state["volume"] == {"muted": False, "level": 4, "max_level": 10,
                               "percent_level": 40.0}  # PercentLevel как у колонки в образце
    state = station_state.parse(_state_raw(volume={"level": 2, "max_level": 23,
                                                   "percent_level": 8.7, "muted": True}))
    assert state["volume"] == {"muted": True, "level": 2, "max_level": 23,
                               "percent_level": 8.7}
    assert station_state.parse(_state_raw())["volume"] == {}  # запрос без cap 23 — не ошибка
    from server_emulator import sdk
    props = {name for name in dir(sdk.ScenarioContext) if not name.startswith("_")}
    assert "volume" in props


# --- WS: ответы директивами по образцам ---------------------------------------------


async def _ask(tmp_path, phrase, env_state=None):
    """Реплика по WS (колонка SN-T): settings как в test_simple_commands, модель выключена."""
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    runner, url = await listen(create_app(runtime=runtime))
    try:
        reply, _, ws = await _open_session(runtime, url, phrase, env_state=env_state)
        await ws.close()
        return reply
    finally:
        await runner.cleanup()
        await runtime.close()


def _names(reply):
    return [d.Name for d in reply.Event.AliceResponse.Response.Directives]


def _directive(reply, name):
    return next(d for d in reply.Event.AliceResponse.Response.Directives if d.Name == name)


def _decoded(reply, name, proto):
    directive = _directive(reply, name)
    message = _station_type(proto)()
    message.ParseFromString(bytes(directive.PayloadRaw))
    return json_format.MessageToDict(message, preserving_proto_field_name=True)


def _speech(reply):
    return reply.Event.AliceResponse.VoiceResponse.OutputSpeech.Text


# --- сверка с обезличенными образцами (tests/fixtures/directives) --------------------

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "directives"


def _check_sample(sample_name, reply):
    """Как в образце: имена и порядок директив (кроме status "blocker-snooze" — сnooze
    не отправляем), type, is_led_silent, payload. Плейсхолдер "<…>" = поле присутствует
    и непустое; прочие значения — точное совпадение."""
    sample = json.loads((FIXTURES / sample_name).read_text(encoding="utf-8"))
    expected = [d for d in sample["directives"] if d.get("status") != "blocker-snooze"]
    assert _names(reply) == [d["name"] for d in expected], sample_name
    for want in expected:
        directive = _directive(reply, want["name"])
        assert directive.Type == want["type"]
        assert directive.IsLedSilent is bool(want["is_led_silent"])
        if "decoded" in want:
            got = _decoded(reply, want["name"], want["proto"])
            _like_sample(want["decoded"], got, want["name"])
        elif "payload" in want:
            got = json_format.MessageToDict(directive.Payload,
                                            preserving_proto_field_name=True)
            assert got == want["payload"], want["name"]
        else:
            assert bytes(directive.PayloadRaw) == b"", want["name"]


def _like_sample(expected, actual, path):
    if isinstance(expected, str) and expected.startswith("<") and expected.endswith(">"):
        assert actual is not None and actual not in ("", 0, [], {}, False), path
    elif isinstance(expected, dict):
        assert set(actual) == set(expected), path
        for key, value in expected.items():
            _like_sample(value, actual[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert len(actual) == len(expected), path
        for index, (value, got) in enumerate(zip(expected, actual)):
            _like_sample(value, got, f"{path}[{index}]")
    else:
        assert actual == expected, path


# --- сравнение с обезличенными образцами (tests/fixtures/directives, TASK-079) ------

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "directives"


def _check_sample(sample_name, reply):
    """Директивы ответа совпадают с образцом настоящего сервера: порядок имён, type,
    is_led_silent и payload. Значения-плейсхолдеры вида "<…>" из фикстуры (decoded и
    Struct-payload) — «поле присутствует и непустое»; остальное сверяется буквально.
    Директивы со status "blocker-snooze" (snooze-перепостановка будильника) в ожидания
    не входят."""
    sample = json.loads((FIXTURES / sample_name).read_text(encoding="utf-8"))
    expected = [d for d in sample["directives"] if d.get("status") != "blocker-snooze"]
    assert _names(reply) == [d["name"] for d in expected]
    for want in expected:
        directive = _directive(reply, want["name"])
        assert directive.Type == want["type"]
        assert directive.IsLedSilent is bool(want["is_led_silent"])
        if "decoded" in want:
            got = _decoded(reply, want["name"], want["proto"])
            _like_sample(want["decoded"], got, want["name"])
        elif "payload" in want:
            got = json_format.MessageToDict(directive.Payload,
                                            preserving_proto_field_name=True)
            if "<" in json.dumps(want["payload"]):  # Struct-плейсхолдеры "<…>" — «есть и непустое»
                _like_sample(want["payload"], got, want["name"])
            else:
                assert got == want["payload"]
        else:
            assert bytes(directive.PayloadRaw) == b""


def _like_sample(expected, actual, path):
    if isinstance(expected, str) and expected.startswith("<") and expected.endswith(">"):
        assert actual is not None and actual != "" and actual != 0, path
    elif isinstance(expected, dict):
        assert set(actual) == set(expected), path
        for key, value in expected.items():
            _like_sample(value, actual[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert len(actual) == len(expected), path
        for index, (value, got) in enumerate(zip(expected, actual)):
            _like_sample(value, got, f"{path}[{index}]")
    else:
        assert actual == expected, path


async def test_timer_set_sends_sample_directives(tmp_path):
    reply = await _ask(tmp_path, "поставь таймер на 5 минут")
    assert _names(reply) == ["open_screen_timer_info_directive", "timer_set_directive",
                             "tts_play_placeholder"]
    payload = _decoded(reply, "timer_set_directive", TIMER_SET)
    assert payload["Name"] == "set_timer_capability_directive"
    timer = payload["Timer"]
    assert TIMER_ID_RE.match(timer["TimerId"])
    assert timer["Duration"] == 300 and timer["Remaining"] == 300
    assert abs(int(timer["StartTimestamp"]) - time.time()) < 5
    assert _decoded(reply, "open_screen_timer_info_directive", OPEN_INFO)["TimerId"] == timer["TimerId"]
    assert _directive(reply, "timer_set_directive").IsLedSilent is True
    assert _speech(reply) == "Хорошо. 5 минут. Отсчёт пошёл."
    _check_sample("timer-set.json", reply)


async def test_timer_set_closes_open_list_screen(tmp_path):
    reply = await _ask(tmp_path, "таймер на 1 минуту",
                       env_state=_state_raw(screens={"timer_list": True}))
    assert _speech(reply) == "Хорошо. 1 минуту. Отсчёт пошёл."
    _check_sample("timer-set-closes-open-list.json", reply)


async def test_timer_list_reports_remaining(tmp_path):
    start = time.time() - 3000
    reply = await _ask(tmp_path, "сколько осталось до таймера",
                       env_state=_state_raw(timers=[{"timer_id": TIMER_A, "start_ts": start,
                                                     "duration": 7500}]))
    assert _names(reply)[:1] == ["open_screen_timer_list_directive"]
    assert _decoded(reply, "open_screen_timer_list_directive", OPEN_LIST)["TargetTimerIds"] == [TIMER_A]
    seconds_left = 7500 - (int(time.time()) - int(start))
    assert _speech(reply) in {f"Осталось {format_duration(left)}"
                              for left in range(seconds_left - 6, seconds_left + 7)}
    _check_sample("timer-list.json", reply)


async def test_timer_cancel_uses_state_timer_id(tmp_path):
    raw = _state_raw(timers=[{"timer_id": TIMER_A, "start_ts": time.time() - 60, "duration": 600},
                             {"timer_id": TIMER_B, "start_ts": time.time() - 60, "duration": 900,
                              "disabled": True}])
    reply = await _ask(tmp_path, "отмени таймер", env_state=raw)
    assert _names(reply) == ["open_screen_timer_list_directive", "timer_cancel_directive",
                             "tts_play_placeholder"]
    assert _decoded(reply, "open_screen_timer_list_directive", OPEN_LIST) == {"ForceAutoClose": True}
    assert _decoded(reply, "timer_cancel_directive", TIMER_CANCEL) == {
        "Name": "cancel_timer_capability_directive", "TimerId": TIMER_A}
    assert _speech(reply) == "Таймер отменён."
    _check_sample("timer-cancel.json", reply)


async def test_timer_cancel_without_state_says_no_timers(tmp_path):
    reply = await _ask(tmp_path, "отмени таймер")
    assert "timer_cancel_directive" not in _names(reply)
    assert _speech(reply) == "Активных таймеров нет."


async def test_alarm_set_sends_sample_directives(tmp_path):
    reply = await _ask(tmp_path, "поставь будильник на 7:30")
    assert _names(reply) == ["open_screen_alarm_list_directive", "alarm_add_directive",
                             "tts_play_placeholder"]
    assert _decoded(reply, "open_screen_alarm_list_directive", OPEN_ALARM_LIST) == {"ForceAutoClose": True}
    payload = _decoded(reply, "alarm_add_directive", ALARM_ADD)
    assert payload["Name"] == "alarm_add_directive"
    alarm = payload["Alarm"]
    assert TIMER_ID_RE.match(alarm["AlarmId"])
    assert alarm["LocalTime"] == {"Hours": 7, "Minutes": 30}
    dates = [tuple(d["Date"][part] for part in ("Year", "Month", "Day")) for d in alarm["ActivationDays"]]
    day = datetime.date.today() + datetime.timedelta(days=1 if _speech(reply).endswith("на завтра.") else 0)
    assert dates == [day.timetuple()[:3]]
    assert alarm["SnoozeTime"] == 10
    assert "Timezone" in alarm
    assert _speech(reply).startswith("Будильник на 7:30 поставил")
    _check_sample("alarm-set.json", reply)


async def test_ring_alarm_stop_hvatit(tmp_path):
    raw = _state_raw(alarms=[{"alarm_id": ALARM_A, "hours": 7, "currently_playing": True}])
    reply = await _ask(tmp_path, "хватит", env_state=raw)
    assert _names(reply) == ["alarm_stop_directive", "close_screen_alarm_active_directive",
                             "tts_play_placeholder"]
    assert _decoded(reply, "alarm_stop_directive", ALARM_STOP) == {
        "Name": "stop_alarm_capability_directive", "StopMedia": True}
    assert _directive(reply, "close_screen_alarm_active_directive").PayloadRaw == b""
    assert _speech(reply) == "Будильник выключил."
    _check_sample("alarm-stop.json", reply)


async def test_ring_timer_stop_is_silent(tmp_path):
    raw = _state_raw(timers=[{"timer_id": TIMER_A, "start_ts": time.time() - 100, "duration": 60}])
    reply = await _ask(tmp_path, "хватит", env_state=raw)
    assert _names(reply) == ["tts_play_placeholder", "timer_stop_directive",
                             "close_screen_timer_active_directive"]
    assert _decoded(reply, "timer_stop_directive", TIMER_STOP) == {"TimerId": TIMER_A}
    assert _speech(reply) == ""
    _check_sample("timer-stop.json", reply)


async def test_hvatit_without_ringing_stays_media(tmp_path):
    """Без сигнала в состоянии «хватит» — прежний исполнитель (пауза), не сигнальные директивы."""
    raw = _state_raw(timers=[{"timer_id": TIMER_A, "start_ts": time.time() - 60, "duration": 600}])
    reply = await _ask(tmp_path, "хватит", env_state=raw)
    assert {"alarm_stop_directive", "timer_stop_directive"}.isdisjoint(_names(reply))


# --- TASK-083: громкость — тихий ответ директивой образца ----------------------------


async def test_volume_sample_form_by_ws(tmp_path):
    """WS-путь с состоянием колонки (cap 23): «громкость два» — форма обезличенного образца,
    без речи и карточек (журнал 2026-10-24 00:24:56/23:07:45, 2026-10-29 22:14:24)."""
    reply = await _ask(tmp_path, "громкость два",
                       env_state=_state_raw(volume={"level": 4, "max_level": 10}))
    _check_sample("volume-level.json", reply)
    payload = json_format.MessageToDict(_directive(reply, "sound_set_level").Payload,
                                        preserving_proto_field_name=True)
    assert payload == {"new_level": 2.0, "new_percent_level": 20.0}
    assert _speech(reply) == ""
    assert not [c.Text for c in reply.Event.AliceResponse.Response.Cards]
    assert _directive(reply, "tts_play_placeholder").AnalyticsType == "tts_play_placeholder"


async def test_volume_percent_scale_from_station_state(tmp_path):
    """Проценты и шкала — из MaxLevel колонки: «на 50» при MaxLevel 20 → level 10.0;
    шкала 0..23 — процент по её шкале (образцы: журнал cap 23)."""
    reply = await _ask(tmp_path, "громкость на 50",
                       env_state=_state_raw(volume={"level": 4, "max_level": 20}))
    payload = json_format.MessageToDict(_directive(reply, "sound_set_level").Payload,
                                        preserving_proto_field_name=True)
    assert payload == {"new_level": 10.0, "new_percent_level": 50.0}
    reply = await _ask(tmp_path, "громкость семь",
                       env_state=_state_raw(volume={"level": 4, "max_level": 23}))
    payload = json_format.MessageToDict(_directive(reply, "sound_set_level").Payload,
                                        preserving_proto_field_name=True)
    assert payload == {"new_level": 7.0, "new_percent_level": round(7 * 100.0 / 23, 1)}
