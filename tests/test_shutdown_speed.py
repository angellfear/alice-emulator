"""Мгновенная остановка/перезапуск сервера: открытый WS колонки и живой туннель фронта
(upstream принял соединение, но молчит) не удерживают процесс.

front.stop() отменяет обработчики соединений (wait_closed ждал бы закрытия туннеля вечно),
aiohttp закрывает соединения сразу (shutdown_timeout=0), закрытия WS не ждут клиента,
дочерние процессы голосового стека убиваются без ожидания выхода. Порты освобождены сразу.
"""
import asyncio
import ssl
import subprocess
import sys
import time

from aiohttp import ClientSession

from server_emulator import control, server, server_config
from server_emulator.front import Front
from server_emulator.runtime import Runtime
from server_emulator.voice_stack import LocalSpeech
from tests.conftest import types
from tests.test_front_passthrough import free_port, make_cert, start_front

QUIET = "quiet.deep.example.test"  # домен вне сертификата (wildcard ровно на один уровень) — туннель


async def start_silent_upstream(port):
    """«Настоящий» сервер на 127.0.0.2 (вне local_addresses — иначе петля): принял и молчит."""
    accepted = asyncio.Event()
    handlers = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            accepted.set()
            await asyncio.sleep(3600)  # ответа нет и не будет — как настоящий хост без ответа
        finally:
            handlers.discard(task)

    return await asyncio.start_server(handle, "127.0.0.2", port), accepted, handlers


async def stop_silent_upstream(upstream, handlers):
    upstream.close()
    for task in list(handlers):  # обработчик молчит вечно — отменяем, не ждём
        task.cancel()
    await asyncio.gather(*list(handlers), return_exceptions=True)


async def open_tunnel(port):
    """Клиент с непокрытым SNI: задача висит в TLS-handshake, пока туннель не разорвут."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return asyncio.ensure_future(asyncio.open_connection(
        "127.0.0.1", port, ssl=context, server_hostname=QUIET))


async def assert_tunnel_torn_down(client_task, timeout=5):
    done, _pending = await asyncio.wait({client_task}, timeout=timeout)
    assert done, "соединение туннеля не разорвано остановкой"
    assert client_task.exception() is not None  # handshake не состоялся — upstream молчал


async def test_front_stop_closes_tunnel_without_waiting(tmp_path):
    runtime = Runtime(tmp_path)
    front, runner, port, _cert = await start_front(runtime, tmp_path, {QUIET: ["127.0.0.2"]})
    upstream, accepted, handlers = await start_silent_upstream(port)
    tunnel = await open_tunnel(port)
    try:
        await asyncio.wait_for(accepted.wait(), 10)  # upstream принял туннель и молчит
        await asyncio.wait_for(front.stop(), 2)  # раньше ждал бы закрытия туннеля бесконечно
        await assert_tunnel_torn_down(tunnel)
    finally:
        tunnel.cancel()
        await stop_silent_upstream(upstream, handlers)
        await runner.cleanup()
        runtime.store.close()


async def test_restart_with_open_ws_and_tunnel_is_instant(tmp_path, monkeypatch):
    cert, key = make_cert(tmp_path, "s", "example.test", ("*.example.test", "example.test"))
    main_port, http_port = free_port(), free_port()
    config = {"host": "127.0.0.1", "port": main_port, "ssl": True, "cert": str(cert), "key": str(key),
              "http_port": http_port, "front": True, "front_dns": ["8.8.8.8", "1.1.1.1"]}

    async def fake_resolve(self, name):  # DNS не трогаем: адрес молчащего upstream задан
        return {QUIET: ["127.0.0.2"]}.get(name, [])
    monkeypatch.setattr(Front, "_resolve", fake_resolve)

    upstream, accepted, handlers = await start_silent_upstream(main_port)
    runtime = Runtime(tmp_path)
    token = control.write_token(tmp_path)
    app = server.create_app(runtime=runtime, control_token=token)
    app[control.INFO_KEY] = {"state": str(tmp_path), "config": config, "overridden": []}
    task = asyncio.ensure_future(server.serve(app, tmp_path, config))
    client_type, _server_type = types()
    runtime.identities.mutate({"operation": "user", "id": "owner", "name": "O"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-T", "user_id": "owner",
                               "device_ids": ["test-device"]})
    client, ws, tunnel = ClientSession(), None, None
    try:
        for _ in range(50):
            try:
                await client.get(f"http://127.0.0.1:{http_port}/__control/status",
                                 headers={control.HEADER: token})
                break
            except OSError:
                await asyncio.sleep(0.1)
        ws = await client.ws_connect(f"http://127.0.0.1:{http_port}/uni.ws")  # открытый WS колонки
        sync = client_type()
        sync.Event.Header.MessageId = "sync"
        sync.Event.SynchronizeStateRequest.Application.DeviceId = "test-device"
        await ws.send_bytes(b"AAPI" + sync.SerializeToString())
        packet = await asyncio.wait_for(ws.receive(), 5)
        assert packet.data.startswith(b"AAPI")  # WS жив: сервер отвечает по открытому соединению
        tunnel = await open_tunnel(main_port)
        await asyncio.wait_for(accepted.wait(), 10)  # туннель открыт, upstream молчит
        started = time.monotonic()
        async with asyncio.timeout(10):
            async with client.post(f"http://127.0.0.1:{http_port}/__control/restart",
                                   headers={control.HEADER: token}) as response:
                assert response.status == 200
        code = await asyncio.wait_for(task, 10)
        elapsed = time.monotonic() - started
        assert code == server_config.RESTART_CODE
        assert elapsed < 3, f"перезапуск с открытыми соединениями занял {elapsed:.1f} с"
        await assert_tunnel_torn_down(tunnel)
        for port in (main_port, http_port):  # порты освобождены — новый процесс может подняться
            try:
                await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 1)
                raise AssertionError(f"порт {port} ещё открыт после остановки")
            except OSError:
                pass
    finally:
        if tunnel is not None:
            tunnel.cancel()
        if ws is not None and not ws.closed:
            await ws.close()
        await client.close()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
        await stop_silent_upstream(upstream, handlers)


async def test_stop_key_without_wait_kills_without_reaping(tmp_path):
    speech = LocalSpeech(tmp_path / "speech", tmp_path / "packages")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    speech.processes["pkg"] = process
    await asyncio.sleep(0.2)  # дочерний процесс дошёл до sleep
    try:
        started = time.monotonic()
        speech.stop_key("pkg", wait=False)  # kill сразу: выхода процесса не ждём
        assert time.monotonic() - started < 2
        for _ in range(50):  # сам процесс умирает — но уже вне stop_key
            if process.poll() is not None:
                break
            await asyncio.sleep(0.1)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(5)
