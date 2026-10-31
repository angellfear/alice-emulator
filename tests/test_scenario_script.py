"""TASK-019: py-исполнитель сценариев — executor kind:script → handle(ctx) из <id>.py.

Ответ — построители SDK через существующий compose (новой сборки нет); None — отказ
(статус declined): реплика следующему кандидату или режим «Интеллект»; сбои — errored
и запись в runtime.recent (kind: scenario), кроме dry-run; probe не выбирает сценарий
без .py; API: заготовка/сохранение/удаление кода, dry-run пишет effects.
"""
import asyncio
import datetime
import json
import struct

import pytest
from aiohttp import ClientSession

from server_emulator.runtime import Runtime
from server_emulator.scenario_engine.executor import Executor
from server_emulator.scenario_engine.probe import dependencies_ok
from server_emulator.scenario_packages.validate import PackageError, validate_scenario
from server_emulator.server import create_app
from tests.conftest import ROOT, listen, page, types

SCHEMAS = ROOT / "server_emulator" / "schemas"

HELLO_SCRIPT = '''from server_emulator.sdk import say


async def handle(ctx):
    return say("Привет")
'''

DECLINE_SCRIPT = "async def handle(ctx):\n    return None\n"


def script_scenario(sid="user.script.hello", phrases=("скрипт привет",), priority=100):
    return {"format_version": 2, "id": sid, "version": "1.0.0", "category": "custom",
            "priority": priority,
            "triggers": [{"kind": "phrase", "phrases": list(phrases)}],
            "executor": {"kind": "script"}}


def make_runtime(tmp_path, pairs):
    """Runtime со сценариями и кодом .py во временном state (файлы — источник правды)."""
    runtime = Runtime(tmp_path)
    for scenario, code in pairs:
        (runtime.scenario_dir / (scenario["id"] + ".json")).write_text(
            json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
        if code is not None:
            (runtime.scenario_dir / (scenario["id"] + ".py")).write_text(code, encoding="utf-8")
    runtime.settings_service.load_scenario_files(runtime.settings)
    return runtime


async def say_to(runtime, text, sn="SN-S"):
    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dlg", "sn": sn, "text": text},
           "clock": {"now": datetime.datetime.now()}}
    ctx.update(runtime._scenario_context(sn=sn))
    return await runtime.dialog.handle_text(text, ctx, "dlg", None, deliver=False)


def recent_scenarios(runtime):
    return [entry for entry in runtime.recent if entry.get("kind") == "scenario"]


# --- ответ через SDK и compose ----------------------------------------------------


async def test_script_scenario_answers_by_phrase(tmp_path):
    runtime = make_runtime(tmp_path, [(script_scenario(), HELLO_SCRIPT)])
    try:
        report = await say_to(runtime, "скрипт привет")
        assert report["handled"] and report["status"] == "finished"
        assert report["speech"] == "Привет"
        assert report["messages"][0].Event.AliceResponse.Response.Cards[0].Text == "Привет"
    finally:
        await runtime.close()


async def test_script_returning_list_composes_two_messages(tmp_path):
    path = tmp_path / "multi.py"
    path.write_text(
        "from server_emulator.sdk import directive, say\n\n\n"
        "async def handle(ctx):\n"
        "    return [say('Включаю.'), directive('draw_animation', payload={'name': 'joy'})]\n",
        encoding="utf-8")
    executor = Executor(schemas_root=SCHEMAS, script_path=lambda scenario: path)
    outcome = await executor.execute(validate_scenario(script_scenario()), {"input": {}}, "dlg-multi")
    assert outcome.status == "finished"
    assert [ref for ref, _ in outcome.composed] == ["reply", "reply"]
    first, second = (message for _, message in outcome.composed)
    assert first.Event.AliceResponse.Response.Cards[0].Text == "Включаю."
    assert second.Event.AliceResponse.Response.Directives[0].Name == "draw_animation"


async def test_declined_passes_to_next_candidate(tmp_path):
    # отказавшийся py-сценарий (точное совпадение — раньше вхождения) → следующий
    # кандидат (JSON, сматченный вхождением «скрипт»: другой reason — иначе ничья)
    shy = script_scenario("user.script.shy", priority=50)
    greeter = {"format_version": 2, "id": "user.json.greeter", "version": "1.0.0",
               "category": "custom", "priority": 100,
               "triggers": [{"kind": "phrase", "phrases": ["скрипт"]}],
               "flow": [{"id": "c", "op": "compose", "reply": {"text": "Из JSON"}},
                        {"id": "done", "op": "finish"}]}
    runtime = make_runtime(tmp_path, [(shy, DECLINE_SCRIPT), (greeter, None)])
    try:
        report = await say_to(runtime, "скрипт привет")
        assert report["handled"] and report["scenario"] == "user.json.greeter"
        assert report["speech"] == "Из JSON"
    finally:
        await runtime.close()


async def test_all_declined_reports_script_declined(tmp_path):
    runtime = make_runtime(tmp_path, [(script_scenario(), DECLINE_SCRIPT)])
    try:
        report = await say_to(runtime, "скрипт привет")
        assert report["handled"] is False and report["verdict"] == "unsupported"
        assert report["reason"].startswith("script_declined:")
        assert "user.script.hello" in report["reason"]
    finally:
        await runtime.close()


# --- сбои --------------------------------------------------------------------------


async def test_script_exception_errors_and_logs_recent(tmp_path):
    bad = ("from server_emulator.sdk import say\n\n\n"
           "async def handle(ctx):\n"
           "    raise RuntimeError('упало')\n")
    runtime = make_runtime(tmp_path, [(script_scenario(), bad)])
    try:
        report = await say_to(runtime, "скрипт привет")
        assert report["handled"] and report["status"] == "errored"
        assert {"step": "script", "op": "script", "status": "error"} in report["trace"]
        text = report["messages"][0].Event.AliceResponse.Response.Cards[0].Text
        assert text.startswith("Команда не выполнена") and "script_failed" in text
        entries = recent_scenarios(runtime)
        assert len(entries) == 1
        assert entries[0]["id"] == "user.script.hello" and "RuntimeError: упало" in entries[0]["error"]
        assert (await say_to(runtime, "другая фраза"))["handled"] is False  # сервер работает
    finally:
        await runtime.close()


async def test_async_handle_timeout_errors(tmp_path):
    slow = ("import asyncio\n\n\n"
            "async def handle(ctx):\n"
            "    await asyncio.sleep(5)\n")
    runtime = make_runtime(tmp_path, [(script_scenario(), slow)])
    runtime.dialog.executor.scripts.timeout = 0.05  # подмена таймаута раннера
    try:
        report = await say_to(runtime, "скрипт привет")
        assert report["handled"] and report["status"] == "errored"
        assert {"step": "script", "op": "script", "status": "error"} in report["trace"]
        assert "TimeoutError" in recent_scenarios(runtime)[0]["error"]
    finally:
        await runtime.close()


async def test_missing_py_not_selected(tmp_path):
    runtime = make_runtime(tmp_path, [(script_scenario(), None)])
    try:
        scenario = next(s for s in runtime.dialog.scenarios() if s["id"] == "user.script.hello")
        assert dependencies_ok(scenario, runtime.scenario_executor) == ("unsupported", "script_missing")
        report = await say_to(runtime, "скрипт привет")
        assert report["handled"] is False and report["reason"] == "script_missing"
    finally:
        await runtime.close()


# --- журнал recent: dry-run молчит, JSON-сценарии пишутся ---------------------------


async def test_dry_run_records_effects_without_action(tmp_path):
    code = ("from server_emulator.sdk import say\n\n\n"
            "async def handle(ctx):\n"
            "    await ctx.action('timer.start', seconds=90)\n"
            "    return say('Заведено')\n")
    runtime = make_runtime(tmp_path, [(script_scenario(), code)])
    calls = []

    def timer_start(arguments, execution):
        calls.append(dict(arguments))
        return {"id": "t"}

    runtime.scenario_executor.actions["timer.start"] = timer_start  # заглушка-счётчик
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/emulator/api/scenarios", headers={"X-Emulator": "1"},
                                   json={"text": "скрипт привет"}) as r:
                assert r.status == 200
                report = await r.json()
        assert report["handled"] and report["status"] == "finished"
        assert report["effects"] == [{"kind": "action", "name": "timer.start",
                                      "arguments": {"seconds": 90}}]
        assert calls == []  # действие не вызвано
        assert recent_scenarios(runtime) == []  # dry-run в журнал не пишет
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_json_errored_logs_recent(tmp_path):
    scenario = {"format_version": 2, "id": "user.json.boom", "version": "1.0.0",
                "category": "custom",
                "triggers": [{"kind": "phrase", "phrases": ["взорвись"]}],
                "flow": [{"id": "a", "op": "action", "ref": "test.boom"},
                         {"id": "done", "op": "finish"}]}
    runtime = make_runtime(tmp_path, [(scenario, None)])

    def boom(arguments, execution):
        raise RuntimeError("x")

    runtime.scenario_executor.actions["test.boom"] = boom
    try:
        report = await say_to(runtime, "взорвись")
        assert report["status"] == "errored"
        entries = recent_scenarios(runtime)
        assert len(entries) == 1 and entries[0]["id"] == "user.json.boom"
        assert "action_failed" in entries[0]["error"]
    finally:
        await runtime.close()


# --- валидатор ----------------------------------------------------------------------


def test_validator_rejects_script_with_flow():
    scenario = script_scenario()
    scenario["flow"] = [{"id": "done", "op": "finish"}]
    with pytest.raises(PackageError, match="flow не нужен"):
        validate_scenario(scenario)


def test_validator_rejects_unknown_executor_kind():
    scenario = script_scenario()
    scenario["executor"] = {"kind": "weird"}
    with pytest.raises(PackageError, match="executor"):
        validate_scenario(scenario)


def test_validator_accepts_flow_kind_and_plain_script():
    flow = script_scenario()
    flow["executor"] = {"kind": "flow"}
    flow["flow"] = [{"id": "done", "op": "finish"}]
    assert validate_scenario(flow)["executor"] == {"kind": "flow"}  # как без поля
    assert validate_scenario(script_scenario())["executor"] == {"kind": "script"}


# --- API ----------------------------------------------------------------------------


async def test_api_get_put_delete_script(tmp_path):
    runtime = make_runtime(tmp_path, [])
    runner, url = await listen(create_app(runtime=runtime))
    headers = {"X-Emulator": "1"}
    try:
        async with ClientSession() as client:
            # PUT сценария с кодом: файл .py записан, сценарий сохранён
            async with client.put(url + "/emulator/api/scenarios", headers=headers,
                                  json={"scenario": script_scenario(), "script": HELLO_SCRIPT}) as r:
                assert r.status == 200 and (await r.json())["saved"] == "user.script.hello"
            assert (runtime.scenario_dir / "user.script.hello.py").is_file()
            async with client.get(url + "/emulator/api/scenarios?script=user.script.hello") as r:
                assert (await r.json())["code"] == HELLO_SCRIPT
            # GET без файла → заготовка
            empty = runtime.scenario_dir / "user.script.empty.json"
            empty.write_text(json.dumps(script_scenario("user.script.empty")), encoding="utf-8")
            runtime.settings_service.load_scenario_files(runtime.settings)
            async with client.get(url + "/emulator/api/scenarios?script=user.script.empty") as r:
                body = await r.json()
                assert body["code"] is None
                assert "from server_emulator.sdk import say" in body["stub"]
                assert "async def handle(ctx)" in body["stub"]
            # PUT с битым кодом → 400, файл не создан, сценарий не сохранён
            async with client.put(url + "/emulator/api/scenarios", headers=headers,
                                  json={"scenario": script_scenario("user.script.bad"),
                                        "script": "def no("}) as r:
                assert r.status == 400 and "Скрипт" in (await r.json())["error"]
            assert not (runtime.scenario_dir / "user.script.bad.py").exists()
            assert all(s["id"] != "user.script.bad" for s in runtime.settings["scenarios"])
            # DELETE удаляет и .json, и .py
            async with client.delete(url + "/emulator/api/scenarios?id=user.script.hello",
                                     headers=headers) as r:
                assert r.status == 200
            assert not (runtime.scenario_dir / "user.script.hello.json").exists()
            assert not (runtime.scenario_dir / "user.script.hello.py").exists()
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_settings_save_keeps_py_files(tmp_path):
    runtime = make_runtime(tmp_path, [(script_scenario(), HELLO_SCRIPT)])
    try:
        await runtime.save_settings(runtime.settings)  # sync_scenario_files трогает только *.json
        assert (runtime.scenario_dir / "user.script.hello.py").is_file()
        assert (runtime.scenario_dir / "user.script.hello.json").is_file()
    finally:
        await runtime.close()


# --- встроенный py-сценарий ---------------------------------------------------------


async def test_builtin_script_scenario(tmp_path, monkeypatch):
    from server_emulator.scenario_engine import library

    lib = tmp_path / "lib"
    section = lib / "custom"
    section.mkdir(parents=True)
    (lib / "categories.json").write_text(
        json.dumps([{"id": "custom", "title": {"ru": "Пользовательские"}}]), encoding="utf-8")
    (section / "builtin.hello.json").write_text(
        json.dumps(script_scenario("builtin.hello"), ensure_ascii=False), encoding="utf-8")
    (section / "builtin.hello.py").write_text(
        "from server_emulator.sdk import say\n\n\n"
        "async def handle(ctx):\n"
        "    return say('Привет из библиотеки')\n",
        encoding="utf-8")
    monkeypatch.setattr(library, "ROOT", lib)
    scenarios, _categories, _lexicons, errors = library._load()
    assert errors == []
    monkeypatch.setattr(library, "SCENARIOS", scenarios)
    runtime = Runtime(tmp_path / "state")
    try:
        report = await say_to(runtime, "скрипт привет")
        assert report["handled"] and report["scenario"] == "builtin.hello"
        assert report["speech"] == "Привет из библиотеки"
    finally:
        await runtime.close()


# --- e2e: настоящий WS ---------------------------------------------------------------


def _tts_ogg():
    """Минимальный корректный Ogg Opus (BoS + EOS) для заглушки TTS."""
    return (page(2, b'OpusHead' + bytes([1, 1]) + struct.pack('<HIhB', 312, 16000, 0, 0), 0) +
            page(4, b'\xf8\xff\xfe', 1))


async def _voice_roundtrip(runtime, spoken_text, tts_ogg):
    """Полный голосовой проход по WS (образец tests/test_voice_e2e.py): аудио → ASR stub →
    сценарий → TTS stub. Возвращает (events, audio_frames, provider_calls)."""
    calls = []

    async def provider(kind, payload=None, audio=None, filename=None):
        calls.append(kind)
        if kind == 'stt':
            return f'{{"text":"{spoken_text}"}}'.encode(), 'application/json'
        assert kind == 'tts' and payload['input'], payload
        return tts_ogg, 'audio/ogg'
    runtime.provider = provider

    client_type, server_type = types()
    incoming = client_type()
    incoming.Event.Header.MessageId = 'voice-message'
    incoming.Event.Header.StreamId = 3
    incoming.Event.VoiceInput.Header.RequestId = 'request'
    incoming.Event.VoiceInput.Header.DialogId = 'dialog'
    incoming.Event.VoiceInput.Format = 'audio/x-pcm;bit=16;rate=16000;channels=1'
    close = client_type()
    close.StreamControl.StreamId = 3
    close.StreamControl.Close.Reason = 0

    runner, url = await listen(create_app(runtime=runtime))
    events, audio_frames = [], []
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url + '/uni.ws') as ws:
                runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
                runtime.identities.mutate({'operation': 'device', 'sn': 'SN-T', 'user_id': 'owner',
                                           'device_ids': ['test-device']})
                sync = client_type()
                sync.Event.Header.MessageId = 'sync'
                sync.Event.SynchronizeStateRequest.Application.DeviceId = 'test-device'
                await ws.send_bytes(b'AAPI' + sync.SerializeToString())
                await asyncio.wait_for(ws.receive(), 3)  # SynchronizeStateResponse

                await ws.send_bytes(b'AAPI' + incoming.SerializeToString())
                await ws.send_bytes(struct.pack('>I', 3) + b'\0' * 320)
                await ws.send_bytes(b'AAPI' + close.SerializeToString())

                for _ in range(24):
                    packet = await asyncio.wait_for(ws.receive(), 5)
                    if not packet.data.startswith(b'AAPI'):
                        audio_frames.append(packet.data)
                        continue
                    message = server_type.FromString(packet.data[4:])
                    if message.WhichOneof('Message') == 'StreamControl':
                        break
                    events.append((message.Event.WhichOneof('Event'), message))
    finally:
        await runner.cleanup()
    return events, audio_frames, calls


async def test_script_scenario_answers_over_ws(tmp_path):
    runtime = make_runtime(tmp_path, [(script_scenario(), HELLO_SCRIPT)])
    runtime.settings.update(aapi_voice=True, aapi_tts=True)
    try:
        events, audio_frames, calls = await _voice_roundtrip(runtime, 'скрипт привет', _tts_ogg())
        assert 'model' not in calls, f'агент вызван вместо py-сценария: {calls}'
        names = [name for name, _ in events]
        assert 'AliceResponse' in names and 'TtsSpeak' in names, names
        response = next(m for name, m in events if name == 'AliceResponse').Event.AliceResponse
        assert response.Response.Cards[0].Text == 'Привет'
        assert response.VoiceResponse.HasVoiceResponse
        assert audio_frames, 'озвучка не пришла'
    finally:
        await runtime.close()
