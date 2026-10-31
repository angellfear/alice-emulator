"""M08 будильники: parse_time_ru, next_occurrence (DST), планировщик
(dedupe/snooze/rollover/missed-after-restart), сценарии через WS."""
import asyncio
import datetime

import pytest
from aiohttp import ClientSession

from server_emulator.runtime import Runtime
from server_emulator.scenario_engine.library import LEXICONS
from server_emulator.scenario_engine.parsers import parse_time
from server_emulator.scenario_engine.scheduler import TimerScheduler, next_occurrence
from server_emulator.server import create_app
from server_emulator.state_store import StateStore
from tests.conftest import listen
from tests.conftest import _open_session
from tests.conftest import types

# --- парсер времени ---------------------------------------------------------------


def parse_time_ru(text):  # разбор времени теперь — парсер extract со словарём из lexicons.json
    return parse_time(text, LEXICONS["time"])


@pytest.mark.parametrize("phrase,hhmm", [
    ("будильник на 8:30", "08:30"),
    ("разбуди меня в 7 тридцать", "07:30"),
    ("на девять", "09:00"),
    ("на 09.15", "09:15"),
    ("будильник на восемь часов", "08:00"),
    ("разбуди в 21:05", "21:05"),
])
def test_parse_time_ru(phrase, hhmm):
    assert parse_time_ru(phrase) == hhmm


@pytest.mark.parametrize("phrase", ["поставь будильник", "будильник на 25:00", "", "сколько времени"])
def test_parse_time_ru_unparsable(phrase):
    assert parse_time_ru(phrase) is None


# --- next_occurrence: таймзона и DST ----------------------------------------------


def test_next_occurrence_basic_and_timezone():
    # 12:00 UTC-эквивалент: локальное 15:00 Москвы
    ts = datetime.datetime(2026, 10, 23, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
    due = next_occurrence("15:00", "Europe/Moscow", ts)
    local = datetime.datetime.fromtimestamp(due, datetime.timezone.utc)
    assert (local - datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)).total_seconds() == 24 * 3600
    # уже наставшее сегодня → завтра
    due2 = next_occurrence("11:00", "UTC", ts)
    assert due2 > ts and datetime.datetime.fromtimestamp(due2, datetime.timezone.utc).day == 24


def test_next_occurrence_dst_transition():
    # переход ЕС на зимнее время 25.10.2026 03:00→02:00 (UTC+3→UTC+2 у Калининграда)
    before = datetime.datetime(2026, 10, 24, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
    due = next_occurrence("04:30", "Europe/Kaliningrad", before)
    local = datetime.datetime.fromtimestamp(due, datetime.timezone(datetime.timedelta(hours=2)))
    assert local.hour == 4 and local.minute == 30  # срабатывание по локальному времени после сдвига


def test_next_occurrence_weekdays():
    ts = datetime.datetime(2026, 10, 23, 12, 0, tzinfo=datetime.timezone.utc).timestamp()  # пятница
    due = next_occurrence("09:00", "UTC", ts, weekdays={1})  # только понедельник
    local = datetime.datetime.fromtimestamp(due, datetime.timezone.utc)
    assert local.isoweekday() == 1 and local.day == 26


# --- планировщик -----------------------------------------------------------------


def test_alarm_schedule_dedupe_persist_cancel(tmp_path):
    store = StateStore(tmp_path / "e.sqlite3")
    scheduler = TimerScheduler(store, clock=lambda: 1000.0)
    alarm, created = scheduler.schedule_alarm("SN-1", "07:30")
    assert created and alarm["kind"] == "alarm" and alarm["time_hhmm"] == "07:30"
    same, created2 = scheduler.schedule_alarm("SN-1", "7:30")  # нормализация + dedupe
    assert not created2 and same["id"] == alarm["id"]
    assert scheduler.cancel(sn="SN-1", kind="alarm") == 1
    assert scheduler.list_timers(status="active") == []
    store.close()


def test_alarm_fire_rolls_over_to_next_day(tmp_path):
    fired = []
    store = StateStore(tmp_path / "e.sqlite3")
    clock = {"now": datetime.datetime(2026, 10, 23, 6, 0, tzinfo=datetime.timezone.utc).timestamp()}
    scheduler = TimerScheduler(store, fire=fired.append, clock=lambda: clock["now"])
    scheduler.schedule_alarm("SN-1", "07:00", tz="UTC")
    clock["now"] += 3601  # 07:00 прошло
    result = scheduler.poll()
    assert len(result) == 1 and fired[0]["label"] == "Будильник 07:00"
    active = scheduler.list_timers(status="active")
    assert len(active) == 1  # будильник перекатился на завтра
    next_local = datetime.datetime.fromtimestamp(active[0]["due_ts"], datetime.timezone.utc)
    assert next_local.day == 24 and (next_local.hour, next_local.minute) == (7, 0)
    assert scheduler.poll() == []  # повторно не срабатывает
    store.close()


def test_alarm_snooze_refires_in_window(tmp_path):
    store = StateStore(tmp_path / "e.sqlite3")
    clock = {"now": datetime.datetime(2026, 10, 23, 6, 0, tzinfo=datetime.timezone.utc).timestamp()}
    scheduler = TimerScheduler(store, clock=lambda: clock["now"])
    scheduler.schedule_alarm("SN-1", "07:00", tz="UTC")
    clock["now"] = datetime.datetime(2026, 10, 23, 7, 1, tzinfo=datetime.timezone.utc).timestamp()
    scheduler.poll()
    snoozed = scheduler.snooze("SN-1", minutes=9)
    assert snoozed and snoozed["status"] == "active"
    assert snoozed["due_ts"] == clock["now"] + 9 * 60
    clock["now"] += 9 * 60 + 1
    assert len(scheduler.poll()) == 1  # повторный огонь после snooze
    store.close()


def test_alarm_snooze_outside_window_declined(tmp_path):
    store = StateStore(tmp_path / "e.sqlite3")
    clock = {"now": datetime.datetime(2026, 10, 23, 6, 0, tzinfo=datetime.timezone.utc).timestamp()}
    scheduler = TimerScheduler(store, clock=lambda: clock["now"])
    scheduler.schedule_alarm("SN-1", "07:00", tz="UTC")
    clock["now"] = datetime.datetime(2026, 10, 23, 7, 1, tzinfo=datetime.timezone.utc).timestamp()
    scheduler.poll()
    clock["now"] += 601  # окно 10 минут истекло
    assert scheduler.snooze("SN-1") is None
    store.close()


def test_alarm_missed_after_restart_continues(tmp_path):
    store = StateStore(tmp_path / "e.sqlite3")
    TimerScheduler(store, clock=lambda: datetime.datetime(2026, 10, 23, 6, 0,
                                                          tzinfo=datetime.timezone.utc).timestamp()
                   ).schedule_alarm("SN-1", "07:00", tz="UTC")
    store.close()
    # рестарт «в 10:00» — срабатывание пропущено
    later = datetime.datetime(2026, 10, 23, 10, 0, tzinfo=datetime.timezone.utc).timestamp()
    store2 = StateStore(tmp_path / "e.sqlite3")
    fired = []
    scheduler = TimerScheduler(store2, fire=fired.append, clock=lambda: later)
    missed = scheduler.recover_missed()
    assert len(missed) == 1 and missed[0]["status"] == "missed"
    assert scheduler.poll() == [] and fired == []  # без догоняющего огня
    active = scheduler.list_timers(status="active")
    assert len(active) == 1  # будильник продолжает: завтра 07:00
    assert datetime.datetime.fromtimestamp(active[0]["due_ts"], datetime.timezone.utc).day == 24
    store2.close()


def test_alarm_bad_time_rejected(tmp_path):
    scheduler = TimerScheduler(StateStore(tmp_path / "e.sqlite3"))
    with pytest.raises(ValueError):
        scheduler.schedule_alarm("SN", "99:99")


# --- сценарии через WS --------------------------------------------------------------


async def test_alarm_phrase_schedules_and_confirms(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        first, _, ws = await _open_session(runtime, url, "поставь будильник на 8:30")
        assert first.Event.AliceResponse.Response.Cards[0].Text == "Будильник на 8:30."
        alarms = [t for t in runtime.timer_scheduler.list_timers(status="active")
                  if t.get("kind") == "alarm"]
        assert len(alarms) == 1 and alarms[0]["time_hhmm"] == "08:30" and alarms[0]["sn"] == "SN-T"
        await ws.close()
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_alarm_without_time_asks_then_schedules(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        first, second, ws = await _open_session(runtime, url, "поставь будильник",
                                                follow_up="на семь тридцать")
        assert first.Event.AliceResponse.Response.Cards[0].Text == "На какое время поставить будильник?"
        assert second.Event.AliceResponse.Response.Cards[0].Text == "Будильник на 7:30."
        await ws.close()
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_alarm_cancel_and_timer_cancel_are_separate(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        await _open_session(runtime, url, "поставь будильник на 6:00")
        await _open_session(runtime, url, "поставь таймер на 10 минут")
        timers = runtime.timer_scheduler.list_timers(status="active")
        assert {t["kind"] for t in timers} == {"alarm", "timer"}
        reply, _, ws = await _open_session(runtime, url, "отмени таймер")
        assert reply.Event.AliceResponse.Response.Cards[0].Text == "Таймер отменён."
        remaining = runtime.timer_scheduler.list_timers(status="active")
        assert [t["kind"] for t in remaining] == ["alarm"]  # будильник не тронут
        reply2, _, ws2 = await _open_session(runtime, url, "отмени будильник")
        assert reply2.Event.AliceResponse.Response.Cards[0].Text == "Будильник отменён."
        assert runtime.timer_scheduler.list_timers(status="active") == []
        await ws.close(); await ws2.close()
    finally:
        await runner.cleanup()
        await runtime.close()
