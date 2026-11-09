"""Директивы по прото-спецификации (без capture — решение владельца 2026-11-07):
Raw-payload roundtrip по схеме станции (proto_build encode → ParseFromString),
new_percent_level у sound_set_level, сценарии паузы/продолжения таймера, отмены
будильника и перемотки плеера, Push-действия звука включения/выключения
(local_audio_file_play, TLocalAudioFilePlayDirective)."""
import time

import pytest
from google.protobuf import json_format

from server_emulator import clock_directives
from server_emulator.actions import push_message
from server_emulator.response_compose import add_volume_directive
from server_emulator.schema import load_type, schemas_dir, station_file
from server_emulator.scenario_engine.proto_build import encode
from server_emulator.server import create_app
from tests.conftest import _open_session, listen
from tests.test_time_scenarios import (
    ALARM_A, OPEN_ALARM_LIST, OPEN_LIST, TIMER_A, TIMER_B, _state_raw, _speech)

ALARM_B = "22222222-33333333-44444444-55555555"

ALARM_CANCEL = "NAlice.TAlarmCapability.TAlarmCancelDirective"
TIMER_PAUSE = "NAlice.TAlarmCapability.TTimerPauseDirective"
TIMER_RESUME = "NAlice.TAlarmCapability.TTimerResumeDirective"
AUDIO_FILE_PLAY = "NAlice.TAudioFilePlayerCapability.TLocalAudioFilePlayDirective"
REWIND = "audio_player_rewind"


def _server_cls():
    return load_type(schemas_dir(), "alice-api.pb", "NAlice.NAliceApi.TServerMessage")


def _station(proto):
    return load_type(schemas_dir(), station_file(), proto)


def _names(reply):
    return [d.Name for d in reply.Event.AliceResponse.Response.Directives]


def _directive(reply, name):
    return next(d for d in reply.Event.AliceResponse.Response.Directives if d.Name == name)


def _payload(reply, name, proto):
    message = _station(proto)()
    message.ParseFromString(bytes(_directive(reply, name).PayloadRaw))
    return json_format.MessageToDict(message, preserving_proto_field_name=True)


def _payload_struct(reply, name):
    directive = _directive(reply, name)
    fields = {}
    for key, value in directive.Payload.fields.items():
        fields[key] = value.string_value             if value.WhichOneof("kind") == "string_value" else value.number_value
    return fields


async def _ask(tmp_path, phrase, follow_up=None, env_state=None):
    """Реплика по WS (SN-T): harness tests/test_time_scenarios._ask с продолжением."""
    from server_emulator.runtime import Runtime
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    runner, url = await listen(create_app(runtime=runtime))
    try:
        first, second, ws = await _open_session(runtime, url, phrase, follow_up=follow_up,
                                                env_state=env_state)
        await ws.close()
        return first, second
    finally:
        await runner.cleanup()
        await runtime.close()


# --- Raw-payload roundtrip -----------------------------------------------------------

def test_raw_payload_roundtrip():
    """alarm_cancel / timer_pause / timer_resume: encode → ParseFromString, поля по protos."""
    for proto, items, expected in (
            (ALARM_CANCEL, clock_directives.alarm_cancel_items("AL-1"),
             {"Name": "cancel_alarm_capability_directive", "AlarmId": "AL-1"}),
            (TIMER_PAUSE, clock_directives.timer_pause_items("T-1"),
             {"Name": "pause_timer_capability_directive", "TimerId": "T-1"}),
            (TIMER_RESUME, clock_directives.timer_resume_items("T-2"),
             {"Name": "resume_timer_capability_directive", "TimerId": "T-2"})):
        name, spec = items[-1]["name"], items[-1]
        raw = encode(schemas_dir(), spec["proto"], spec["fields"], "centaur")
        message = _station(proto)()
        message.ParseFromString(raw)
        decoded = json_format.MessageToDict(message, preserving_proto_field_name=True)
        assert decoded == expected, name


def test_local_audio_file_play_roundtrip():
    """Push-действия звука: TLocalAudioFilePlayDirective{PlayTimes: 1, SoundToPlay: enum}."""
    cls = _server_cls()
    for action, sound in (("sound_turn_on", "AliceIoTSoundsTurnOn"),
                          ("sound_turn_off", "AliceIoTSoundsTurnOff")):
        message = push_message(cls, action, {})
        directive = message.Event.Push.Directives[0]
        assert directive.Name == "local_audio_file_play"
        parsed = _station(AUDIO_FILE_PLAY)()
        parsed.ParseFromString(directive.PayloadRaw)
        fields = json_format.MessageToDict(parsed, preserving_proto_field_name=True)
        assert fields == {"PlayTimes": 1, "SoundToPlay": sound}


def test_local_audio_file_play_rejects_arguments():
    with pytest.raises(ValueError):
        push_message(_server_cls(), "sound_turn_on", {"x": 1})


# --- sound_set_level: new_percent_level ----------------------------------------------

def test_volume_directive_has_percent_level():
    """TSoundSetLevelDirective NewPercentLevel=6: проценты = уровень × 10 (шкала 0..10)."""
    message = _server_cls()()
    add_volume_directive(message.Event.AliceResponse, 5)
    directive = message.Event.AliceResponse.Response.Directives[0]
    payload = {key: value.number_value for key, value in directive.Payload.fields.items()}
    assert payload == {"new_level": 5.0, "new_percent_level": 50.0}


# --- таймеры: пауза и продолжение ----------------------------------------------------

async def test_timer_pause_single(tmp_path):
    raw = _state_raw(timers=[{"timer_id": TIMER_A, "start_ts": _now() - 60, "duration": 600},
                             {"timer_id": TIMER_B, "start_ts": _now(), "duration": 60,
                              "paused": True}])
    reply, _ = await _ask(tmp_path, "поставь таймер на паузу", env_state=raw)
    assert _names(reply) == ["open_screen_timer_list_directive", "timer_pause_directive",
                             "tts_play_placeholder"]
    assert _payload(reply, "open_screen_timer_list_directive", OPEN_LIST) == {"ForceAutoClose": True}
    assert _payload(reply, "timer_pause_directive", TIMER_PAUSE) == {
        "Name": "pause_timer_capability_directive", "TimerId": TIMER_A}


async def test_timer_pause_none(tmp_path):
    reply, _ = await _ask(tmp_path, "поставь таймер на паузу")
    assert "timer_pause_directive" not in _names(reply)
    assert _speech(reply) == "Активных таймеров нет."


async def test_timer_resume_paused(tmp_path):
    raw = _state_raw(timers=[{"timer_id": TIMER_B, "start_ts": _now() - 60, "duration": 600,
                              "paused": True}])
    reply, _ = await _ask(tmp_path, "продолжи таймер", env_state=raw)
    assert _payload(reply, "timer_resume_directive", TIMER_RESUME) == {
        "Name": "resume_timer_capability_directive", "TimerId": TIMER_B}
    assert _speech(reply) == "Таймер запустила."


async def test_timer_resume_none(tmp_path):
    reply, _ = await _ask(tmp_path, "продолжи таймер")
    assert "timer_resume_directive" not in _names(reply)
    assert _speech(reply) == "Таймеров на паузе нет."


async def test_timer_pause_several_asks_and_continues(tmp_path):
    """Несколько активных таймеров — уточняющий вопрос; ответ «второй» ставит на паузу."""
    raw = _state_raw(timers=[{"timer_id": TIMER_A, "start_ts": _now() - 60, "duration": 600},
                             {"timer_id": TIMER_B, "start_ts": _now() - 30, "duration": 300}])
    question, reply = await _ask(tmp_path, "поставь таймер на паузу", follow_up="второй",
                                 env_state=raw)
    assert "timer_pause_directive" not in _names(question)  # сначала вопрос
    # порядок списка — по остатку (TIMER_B ближе): «второй» — TIMER_A
    assert _payload(reply, "timer_pause_directive", TIMER_PAUSE) == {
        "Name": "pause_timer_capability_directive", "TimerId": TIMER_A}


# --- будильник: отмена ---------------------------------------------------------------

async def test_alarm_cancel_by_time(tmp_path):
    raw = _state_raw(alarms=[{"alarm_id": ALARM_A, "hours": 7, "minutes": 30},
                             {"alarm_id": ALARM_B, "hours": 9, "minutes": 0}])
    reply, _ = await _ask(tmp_path, "отмени будильник на 7:30", env_state=raw)
    assert _names(reply) == ["open_screen_alarm_list_directive", "alarm_cancel_directive",
                             "tts_play_placeholder"]
    assert _payload(reply, "alarm_cancel_directive", ALARM_CANCEL) == {
        "Name": "cancel_alarm_capability_directive", "AlarmId": ALARM_A}


async def test_alarm_cancel_no_match_says_missing(tmp_path):
    raw = _state_raw(alarms=[{"alarm_id": ALARM_A, "hours": 9, "minutes": 0}])
    reply, _ = await _ask(tmp_path, "отмени будильник на 7:30", env_state=raw)
    assert "alarm_cancel_directive" not in _names(reply)
    assert _speech(reply) == "Будильника на 7:30 нет."


async def test_alarm_cancel_several_asks_time_answer(tmp_path):
    """Два будильника без времени — вопрос; ответ «семь утра» отменяет нужный."""
    raw = _state_raw(alarms=[{"alarm_id": ALARM_A, "hours": 7, "minutes": 0},
                             {"alarm_id": ALARM_B, "hours": 9, "minutes": 0}])
    question, reply = await _ask(tmp_path, "отмени будильник", follow_up="семь утра",
                                 env_state=raw)
    assert "alarm_cancel_directive" not in _names(question)
    assert _payload(reply, "alarm_cancel_directive", ALARM_CANCEL) == {
        "Name": "cancel_alarm_capability_directive", "AlarmId": ALARM_A}


async def test_alarm_cancel_ringing_alarm_is_not_candidate(tmp_path):
    """Звонящий будильник не отменяется — его останавливает timers.ringing.stop."""
    raw = _state_raw(alarms=[{"alarm_id": ALARM_A, "hours": 7, "currently_playing": True}])
    reply, _ = await _ask(tmp_path, "отмени будильник", env_state=raw)
    assert "alarm_cancel_directive" not in _names(reply)
    assert _speech(reply) == "Будильников нет."


# --- перемотка плеера ----------------------------------------------------------------

async def test_rewind_with_duration(tmp_path):
    reply, _ = await _ask(tmp_path, "перемотай на минуту вперед")
    assert _names(reply) == ["tts_play_placeholder", "audio_player_rewind"]
    assert _payload_struct(reply, REWIND) == {"amount_ms": 60000, "type": "Forward"}


async def test_rewind_back_asks_and_continues(tmp_path):
    """«перемотай назад» без длительности — вопрос; ответ «тридцать секунд» — Backward."""
    question, reply = await _ask(tmp_path, "перемотай назад", follow_up="тридцать секунд")
    assert REWIND not in _names(question)
    assert _payload_struct(reply, REWIND) == {"amount_ms": 30000, "type": "Backward"}


def _now():
    return time.time()
