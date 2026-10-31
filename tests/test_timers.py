"""M08 таймеры: parse_duration_ru, TimerScheduler (persist/dedupe/cancel/missed/poll),
сценарий «поставь таймер» через WS с уточнением и доставкой срабатывания."""
import asyncio
import time

import pytest
from aiohttp import ClientSession

from server_emulator.runtime import Runtime
from server_emulator.scenario_engine.library import LEXICONS
from server_emulator.scenario_engine.parsers import parse_duration
from server_emulator.scenario_engine.scheduler import TimerScheduler
from server_emulator.server import create_app
from server_emulator.state_store import StateStore
from tests.conftest import _open_session, listen, types

# --- парсер длительностей --------------------------------------------------------


def parse_duration_ru(text):  # разбор длительности — парсер extract со словарём из lexicons.json
    return parse_duration(text, LEXICONS["duration"])


@pytest.mark.parametrize("phrase,seconds", [
    ("поставь таймер на 5 минут", 300),
    ("таймер на десять секунд", 10),
    ("установи таймер на полтора часа", 5400),
    ("таймер на полчаса", 1800),
    ("таймер на двадцать пять минут", 1500),
    ("таймер на 90 секунд", 90),
    ("таймер 300", 300),
    ("таймер на 1.5 минуты", 90),
])
def test_parse_duration_ru(phrase, seconds):
    assert parse_duration_ru(phrase) == seconds


@pytest.mark.parametrize("phrase", ["поставь таймер", "таймер на вчера", "", "сколько времени"])
def test_parse_duration_ru_unparsable(phrase):
    assert parse_duration_ru(phrase) is None


# --- планировщик -----------------------------------------------------------------


def test_schedule_persist_dedupe_and_cancel(tmp_path):
    store = StateStore(tmp_path / "e.sqlite3")
    scheduler = TimerScheduler(store, clock=lambda: 1000.0)
    timer, created = scheduler.schedule("SN-1", 300, label="Таймер 300 с")
    assert created and timer["status"] == "active" and timer["due_ts"] == 1300.0
    same, created2 = scheduler.schedule("SN-1", 300, label="Таймер 300 с")  # dedupe-окно
    assert not created2 and same["id"] == timer["id"]
    other, created3 = scheduler.schedule("SN-1", 600, label="Таймер 600 с")
    assert created3 and other["id"] != timer["id"]
    assert scheduler.cancel(sn="SN-1", label="Таймер 300 с") == 1
    assert [t["status"] for t in scheduler.list_timers()] == ["cancelled", "active"]
    store.close()
    store2 = StateStore(tmp_path / "e.sqlite3")  # персистентность через рестарт
    restored = TimerScheduler(store2, clock=lambda: 1001.0)
    assert len(restored.list_timers(status="active")) == 1
    store2.close()


def test_poll_fires_due_and_records(tmp_path):
    fired = []
    store = StateStore(tmp_path / "e.sqlite3")
    clock = {"now": 1000.0}
    scheduler = TimerScheduler(store, fire=fired.append, clock=lambda: clock["now"])
    scheduler.schedule("SN-1", 10)
    assert scheduler.poll() == []  # ещё не настав
    clock["now"] = 1011.0
    result = scheduler.poll()
    assert len(result) == 1 and result[0]["status"] == "fired"
    assert fired and fired[0]["fired_ts"] == 1011.0
    assert scheduler.poll() == []  # повторно не срабатывает
    store.close()


def test_missed_after_restart_not_refired(tmp_path):
    store = StateStore(tmp_path / "e.sqlite3")
    fired = []
    TimerScheduler(store, clock=lambda: 1000.0).schedule("SN-1", 5)
    store.close()
    # рестарт «позже срока»: просроченный активный → missed, без огня
    store2 = StateStore(tmp_path / "e.sqlite3")
    scheduler = TimerScheduler(store2, fire=fired.append, clock=lambda: 2000.0)
    missed = scheduler.recover_missed()
    assert len(missed) == 1 and missed[0]["status"] == "missed"
    assert scheduler.poll() == [] and fired == []
    store2.close()


def test_invalid_duration_rejected(tmp_path):
    scheduler = TimerScheduler(StateStore(tmp_path / "e.sqlite3"))
    for bad in (0, -5, 100000):
        with pytest.raises(ValueError):
            scheduler.schedule("SN", bad)


# --- интеграция: сценарий через WS -------------------------------------------------


async def test_timer_phrase_schedules_and_confirms(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        first, _, ws = await _open_session(runtime, url, "поставь таймер на 5 минут")
        card = first.Event.AliceResponse.Response.Cards[0].Text
        assert card == "Таймер на 5 минут."
        timers = runtime.timer_scheduler.list_timers(status="active")
        assert len(timers) == 1 and timers[0]["sn"] == "SN-T" and timers[0]["duration_s"] == 300
        await ws.close()
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_timer_without_duration_asks_then_schedules(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        first, second, ws = await _open_session(runtime, url, "поставь таймер",
                                                follow_up="на две минуты")
        assert first.Event.AliceResponse.Response.Cards[0].Text == "На сколько поставить таймер?"
        assert second.Event.AliceResponse.Response.Cards[0].Text == "Таймер на 2 минуты."
        assert len(runtime.timer_scheduler.list_timers(status="active")) == 1
        await ws.close()
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_timer_fire_delivered_to_device_session(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)
    runtime.start_background()
    runner, url = await listen(create_app(runtime=runtime))
    try:
        # короткий таймер через API-слой сценария, доставка — в живую сессию
        runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
        runtime.identities.mutate({'operation': 'device', 'sn': 'SN-T', 'user_id': 'owner',
                                   'device_ids': ['dev-t'], 'profile': {'aapi_model': True}})
        timer, _ = runtime.timer_scheduler.schedule("SN-T", 1, label="Таймер 1 с")
        client_type, server_type = types()
        sync = client_type()
        sync.Event.Header.MessageId = "sync"
        sync.Event.SynchronizeStateRequest.Application.DeviceId = "dev-t"
        delivered = []
        async with ClientSession() as client:
            async with client.ws_connect(url + "/uni.ws") as ws:
                await ws.send_bytes(b"AAPI" + sync.SerializeToString())
                await asyncio.wait_for(ws.receive(), 3)  # SynchronizeStateResponse
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    try:
                        packet = await asyncio.wait_for(ws.receive(), 2)
                    except asyncio.TimeoutError:
                        continue
                    if packet.data.startswith(b"AAPI"):
                        message = server_type.FromString(packet.data[4:])
                        if message.Event.WhichOneof("Event") == "AliceResponse":
                            delivered.append(message.Event.AliceResponse.Response.Cards[0].Text)
                            break
                await ws.close()
        assert delivered == ["Таймер 1 с сработал."]
        assert runtime.timer_scheduler.list_timers(status="active") == []
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_timer_cancel_voice(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        first, _, ws = await _open_session(runtime, url, "поставь таймер на 10 минут")
        runtime.timer_scheduler.schedule("SN-T", 20, label="Таймер 20 с")
        assert len(runtime.timer_scheduler.list_timers(status="active")) == 2
        reply, _, ws = await _open_session(runtime, url, "отмени таймер")
        assert reply.Event.AliceResponse.Response.Cards[0].Text == "Таймер отменён."
        assert runtime.timer_scheduler.list_timers(status="active") == []
        await ws.close()
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_timer_cancel_without_active(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        reply, _, ws = await _open_session(runtime, url, "отмени таймер")
        assert reply.Event.AliceResponse.Response.Cards[0].Text == "Активных таймеров нет."
        await ws.close()
    finally:
        await runner.cleanup()
        await runtime.close()
