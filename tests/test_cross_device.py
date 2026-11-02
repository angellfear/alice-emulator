"""Команды между колонками: «поставь будильник на дуо 32» с колонки «дуо 24» —
адресат по имени/псевдониму (params device.aliases) из home.devices владельца."""
import datetime

from server_emulator.runtime import Runtime
from server_emulator.schema import load_type


def _home(runtime):
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    for sn in ("SN-24", "SN-32"):
        runtime.identities.mutate({'operation': 'device', 'sn': sn, 'user_id': 'owner', 'device_ids': []})
    runtime.quasar_params.set_value("device:SN-24", "device.aliases", ["дуо 24"])
    runtime.quasar_params.set_value("device:SN-32", "device.name", "Дуо в спальне")
    runtime.quasar_params.set_value("device:SN-32", "device.aliases", ["дуо 32"])


async def _say(runtime, text, sn):
    replies = []

    async def send(data):
        cls = load_type("server_emulator/schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
        message = cls.FromString(data[4:])
        if message.Event.WhichOneof("Event") == "AliceResponse":
            replies.append(message.Event.AliceResponse.Response.Cards[0].Text)

    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg", "sn": sn, "text": text},
           "clock": {"now": datetime.datetime.now()}}
    ctx.update(runtime._scenario_context(sn=sn))
    report = await runtime.dialog.handle_text(text, ctx, "dg", send)
    return report, replies[0]


async def test_home_devices_in_context(tmp_path):
    runtime = Runtime(tmp_path)
    _home(runtime)
    try:
        home = runtime._scenario_context(sn="SN-24")["home"]["devices"]
        assert {d["sn"]: (d["name"], d["aliases"], d["current"]) for d in home} == {
            "SN-24": ("SN-24", ["дуо 24"], True), "SN-32": ("Дуо в спальне", ["дуо 32"], False)}
    finally:
        await runtime.close()


async def test_alarm_on_other_speaker(tmp_path):
    runtime = Runtime(tmp_path)
    _home(runtime)
    try:
        report, text = await _say(runtime, "поставь будильник на дуо 32 на 7:30", "SN-24")
        assert report["scenario"] == "timers.alarm.set"
        # TASK-079: директивы ответа уходят колонке-автору запроса; адресация другой
        # колонке — БЛОКЕР (образца нет), ответ честный
        assert text == "Пока не ставлю будильник на другой колонке — только на этой."
    finally:
        await runtime.close()


async def test_timer_on_self_by_own_alias_and_without_target(tmp_path):
    runtime = Runtime(tmp_path)
    _home(runtime)
    try:
        _, text = await _say(runtime, "поставь таймер на 5 минут", "SN-24")
        assert text == "Хорошо. 5 минут. Отсчёт пошёл."
        _, text = await _say(runtime, "поставь таймер на дуо 24 на 2 минуты", "SN-24")  # сама себя
        assert text == "Хорошо. 2 минуты. Отсчёт пошёл."
        assert not hasattr(runtime, "timer_scheduler")  # TASK-079: серверного расписания нет
    finally:
        await runtime.close()
