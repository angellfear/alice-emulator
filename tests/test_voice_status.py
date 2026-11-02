"""TASK-088: GET api/live — строка состояния в шапку панели.

Значки ASR / TTS / Модель: ok — локальный пакет отвечает на /health либо облачный адрес
настроен и последняя операция без ошибки; warn — установка/запуск; err — сбой (в hint текст
и время), серый — источник не выбран. Микросостояние агента (idle/listening/thinking/tool/
responding/error) выводится из событий SpeechTrace — единый механизм, второго нет; «вызывает
инструмент» пишет agent/tools.py (runtime.note_tool_call).
"""
import json
import re
import time

import pytest
from aiohttp import ClientSession

from server_emulator import voice_stack
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen

ASR_VALUES = {"url": "http://127.0.0.1:9/v1/audio/transcriptions", "model": "whisper", "api_key": "k1"}
PACKAGE = {"id": "locasr", "service": "asr", "title": "Локальный ASR", "adapter": "openai",
           "config": {"url": "http://127.0.0.1:{port}/v1/audio/transcriptions"},
           "fields": [{"key": "port", "label": "Порт", "type": "number", "step": 1}],
           "defaults": {"port": 59123},
           "server": {"script": "tools/x.py", "health": "/health"},
           "platforms": {voice_stack.PLATFORM: {"cpu": []}}}


async def _live(client, url):
    r = await client.get(url + "/emulator/api/live")
    assert r.status == 200
    return await r.json()


async def test_live_defaults_are_gray_and_idle(tmp_path):
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            data = await _live(client, url)
            assert data["intelligence"] == "agent"
            assert set(data["services"]) == {"asr", "tts", "llm"}
            assert all(item["status"] == "off" for item in data["services"].values())
            assert data["agent"] == {"state": "idle", "tool": None, "error": None}
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_live_cloud_source_error_and_recovery(tmp_path):
    """Облачный ASR: зелёный после применения; pipeline_error(stage asr) — красный с текстом
    и временем; asr_final следующего хода — снова зелёный."""
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession(headers={"X-Emulator": "1"}) as client:
            r = await client.post(url + "/emulator/api/voice-stack-preset",
                                  json={"service": "asr", "adapter": "openai", "title": "Облачный ASR", "values": ASR_VALUES})
            await client.post(url + "/emulator/api/voice-stack-apply",
                              json={"service": "asr", "ref": "p:" + (await r.json())["id"]})
            item = (await _live(client, url))["services"]["asr"]
            assert item["status"] == "ok" and "Облачный ASR" in item["source"]

            runtime.speech_sink({"name": "pipeline_error", "stage": "asr", "error": "ConnectError: отказ"})
            data = await _live(client, url)
            item = data["services"]["asr"]
            assert item["status"] == "err" and "ConnectError" in item["hint"]
            assert re.search(r"\d{2}:\d{2}", item["hint"])  # время последней ошибки
            assert data["agent"]["state"] == "error" and data["agent"]["error"] == "ConnectError: отказ"

            runtime.speech_sink({"name": "voice_input_start"})
            assert (await _live(client, url))["agent"]["state"] == "listening"  # ошибка хода ушла
            runtime.speech_sink({"name": "asr_final"})
            assert (await _live(client, url))["services"]["asr"]["status"] == "ok"  # сервис ответил — флаг снят
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_live_local_package_states(tmp_path, monkeypatch):
    """Локальный пакет: running — зелёный, installing — жёлтый, stopped — красный."""
    runtime = Runtime(tmp_path)
    packages = tmp_path / "pk"
    packages.mkdir()
    (packages / "locasr.json").write_text(json.dumps(PACKAGE), encoding="utf-8")
    runtime.voice_stack.packages_dir = packages

    async def start_noop(package):
        pass

    monkeypatch.setattr(runtime.voice_stack.local, "start", start_noop)

    async def health_running(package):
        return {"model": "sherpa"}

    monkeypatch.setattr(runtime.voice_stack.local, "health", health_running)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession(headers={"X-Emulator": "1"}) as client:
            package = runtime.voice_stack.package("locasr")
            folder = runtime.voice_stack.local.folder(package)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "installed.json").write_text('{"device": "cpu"}', encoding="utf-8")
            r = await client.post(url + "/emulator/api/voice-stack-apply", json={"service": "asr", "ref": "k:locasr"})
            assert r.status == 200
            item = (await _live(client, url))["services"]["asr"]
            assert item["status"] == "ok" and "Локальный ASR" in item["source"]

            runtime.voice_stack.local.jobs["locasr"] = {"stage": "Скачивание модели", "progress": 0.5, "error": None}
            assert (await _live(client, url))["services"]["asr"]["status"] == "warn"  # установка идёт
            del runtime.voice_stack.local.jobs["locasr"]

            async def health_down(package):
                return None

            monkeypatch.setattr(runtime.voice_stack.local, "health", health_down)
            item = (await _live(client, url))["services"]["asr"]
            assert item["status"] == "err" and "остановлен" in item["hint"]
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_live_agent_microstate(tmp_path):
    """Микросостояние хода: слушает → думает → вызывает инструмент → отвечает → ожидает;
    ошибка — с текстом; зависший ход гаснет по таймауту."""
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async def agent():
                return (await _live(client, url))["agent"]

            assert await agent() == {"state": "idle", "tool": None, "error": None}
            runtime.speech_sink({"name": "voice_input_start", "run": "r1"})
            assert (await agent())["state"] == "listening"
            runtime.speech_sink({"name": "asr_partial", "run": "r1"})
            assert (await agent())["state"] == "listening"
            runtime.speech_sink({"name": "asr_final", "run": "r1"})
            assert (await agent())["state"] == "thinking"

            runtime.note_tool_call("weather_current")
            assert await agent() == {"state": "tool", "tool": "weather_current", "error": None}
            runtime.note_tool_done()
            assert (await agent())["state"] == "thinking"

            runtime.speech_sink({"name": "tts_start", "run": "r1"})
            assert (await agent())["state"] == "responding"
            runtime.speech_sink({"name": "tts_done", "run": "r1"})
            assert (await agent())["state"] == "responding"
            runtime.speech_sink({"name": "alice_response", "run": "r1"})
            assert (await agent())["state"] == "idle"

            runtime.speech_sink({"name": "pipeline_error", "run": "r1", "stage": "model",
                                 "error": "TimeoutError: провайдер"})
            state = await agent()
            assert state["state"] == "error" and state["error"] == "TimeoutError: провайдер"
            item = (await _live(client, url))["services"]["llm"]
            assert item["status"] == "err" and "TimeoutError" in item["hint"]  # stage model → значок «Модель»

            runtime.agent_activity["ts"] = time.time() - 99999  # ход без терминального события
            assert (await agent())["state"] == "idle"
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_live_skips_agent_state_in_scenarios_mode(tmp_path):
    """Поле intelligence в ответе есть — UI показывает чип агента только в режиме «agent»."""
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession(headers={"X-Emulator": "1"}) as client:
            await client.put(url + "/emulator/api/intelligence", json={"intelligence": "scenarios"})
            assert (await _live(client, url))["intelligence"] == "scenarios"
    finally:
        await runner.cleanup()
        await runtime.close()
