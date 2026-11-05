"""TASK-095: точечные исправления дефектов из разбора журнала рабочего экземпляра (TASK-094).

1) aiohttp не отбивает заголовки колонки 16–19 КиБ (max_field_size/max_line_size на AppRunner);
2) GET api/identities не падает от capabilities.raw (bytes протокольного кадра) в сессии;
3) сбой пересылки в «Прозрачно» — 502, а не 500 от aiohttp (и без дубля в журнале);
4) ошибка плагина после таймаута refresh_dataset не теряется, а журнал сбоев сценариев
   получает тип/сообщение исключения (последняя строка), а не хвост трейсбека.
"""
import asyncio
import logging
import socket
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession

from server_emulator import control, server
from server_emulator.runtime import Runtime
from server_emulator.scenario_engine.executor import Execution, Executor
from server_emulator.scripting import failure
from server_emulator.server import create_app
from tests.conftest import listen
from tests.test_device_route_modes import DEVICE, registry, set_route
from tests.test_front_passthrough import free_port, wait_status


async def test_big_station_header_is_parsed(tmp_path):
    """Колонка шлёт значение заголовка 16–19 КиБ: до исправления парсер отдавал 400 (LineTooLong)."""
    config = {"host": "127.0.0.1", "port": free_port(), "ssl": False, "cert": "", "key": "",
              "http_port": 0, "front": False, "front_dns": ["8.8.8.8"]}
    runtime = Runtime(tmp_path)
    token = control.write_token(tmp_path)
    app = server.create_app(runtime=runtime, control_token=token)
    app[control.INFO_KEY] = {"state": str(tmp_path), "config": config, "overridden": []}
    task = asyncio.ensure_future(server.serve(app, tmp_path, config))
    try:
        await wait_status(config["port"], token)

        def raw_probe():
            request = ("GET /emulator/api/status HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                       f"X-Big-Cookie: {'x' * 16000}\r\nConnection: close\r\n\r\n").encode()
            with socket.create_connection(("127.0.0.1", config["port"]), timeout=10) as raw:
                raw.sendall(request)
                data = b""
                while b"\r\n\r\n" not in data:  # заголовки ответа; тело не обязательно ждать
                    chunk = raw.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                if b"\r\n\r\n" in data:  # добираем тело статуса (Content-Length маленький)
                    data += raw.recv(65536)
                return data

        data = await asyncio.to_thread(raw_probe)  # блокирующий обмен — вне цикла сервера
        assert b'"standalone"' in data          # запрос дошёл до обработчика
        assert b" 400 " not in data.split(b"\r\n", 1)[0]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


async def test_identities_survives_capability_raw_bytes(tmp_path):
    """TASK-069 кладёт raw (bytes) в сессию; до исправления GET api/identities отвечал 500."""
    runtime = Runtime(tmp_path)
    runtime.sessions["s1"] = {"connection_id": "s1", "host": "quasar.yandex.net", "ip": "192.0.2.10",
                              "device_sn": "SN-X", "user_id": None, "runtime": None, "send": None,
                              "ws": None, "transport": None,
                              "capabilities": {"player_state": {"ts": 1.0, "bytes": 12,
                                                                "preview": "ab", "raw": b"\x00\x01"}}}
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/identities") as response:
                assert response.status == 200
                data = await response.json()
    finally:
        await runner.cleanup()
        await runtime.close()
    cap = data["sessions"][0]["capabilities"]["player_state"]
    assert cap["preview"] == "ab" and cap["bytes"] == 12 and "raw" not in cap


async def test_proxy_upstream_failure_answers_502(tmp_path):
    """Сбой пересылки в «Прозрачно»: колонка получает 502, а не 500 от aiohttp."""
    runtime = Runtime(tmp_path)
    registry(runtime)
    set_route(runtime, "SN-UP", "proxy")
    runtime.settings["aliases"] = {"quasar.yandex.net": "http://127.0.0.1:1"}  # закрытый порт
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/push_subscribe", headers=DEVICE) as response:
                assert response.status == 502
                body = await response.json()
    finally:
        await runner.cleanup()
        await runtime.close()
    assert body["error"] == "upstream_unavailable"


async def test_refresh_dataset_late_plugin_error_retrieved(tmp_path, caplog):
    """Плагин упал после таймаута: задача не ждётся — ошибку забирает done-callback."""
    async def broken(key, args):
        await asyncio.sleep(0.05)
        raise RuntimeError("плагин умер")

    executor = Executor(plugins={"weather": broken})
    with pytest.raises(asyncio.TimeoutError):
        await executor.refresh_dataset("weather", "k", timeout=0.01)
    with caplog.at_level(logging.DEBUG, logger="server_emulator.scenario_engine.executor"):
        await asyncio.sleep(0.2)
    assert executor._refreshing == {}  # noqa: SLF001 — задача доработала и вычищена
    assert any("плагин" in record.getMessage() for record in caplog.records)


async def test_scenario_error_journals_exception_line():
    """Журнал сбоев сценариев — последняя строка (тип/сообщение), не хвост трейсбека."""
    captured = []
    executor = Executor(on_error=captured.append)
    execution = Execution("d1", {"id": "weather.py"})
    execution.error = "script_failed:" + failure(ValueError("нет источника"))
    outcome = await executor._finish_with(execution, "errored")
    assert outcome.status == "errored"
    # одна строка: тип и сообщение исключения; кадры трейсбека в журнал не идут
    assert "\n" not in captured[0]["error"]
    assert captured[0]["error"].endswith("ValueError: нет источника")
