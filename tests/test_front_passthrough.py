"""TASK-067: фронт TLS-порта — SNI покрыт сертификатом → эмулятор, не покрыт → туннель.

Самоподписанный сертификат на *.example.test; «настоящий» TLS-сервер — на 127.0.0.2
(вне local_addresses — иначе защита от петли его бы отсекла); резолвер фронта подменён.
"""
import asyncio
import datetime
import json
import ssl
import socket

import pytest
from aiohttp import web

from server_emulator import control, server, server_config
from server_emulator.front import Front, cert_covers, dns_names_in_cert
from server_emulator.runtime import Runtime
from tests.conftest import listen

REAL_BODY = b"real-server-answer"


def make_cert(tmp_path, name, cn, sans):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1)))
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in sans]), critical=False)
    cert = builder.sign(key, hashes.SHA256())
    cert_path, key_path = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def tls_exchange(port, server_hostname, path, timeout=15):
    """Сырой TLS-запрос без проверки сертификата → (байты ответа или b"", локальный адрес клиента)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as raw:
            local = raw.getsockname()
            with context.wrap_socket(raw, server_hostname=server_hostname) as sock:
                sock.settimeout(timeout)
                sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {server_hostname or '127.0.0.1'}\r\n"
                             f"Connection: close\r\n\r\n".encode())
                chunks = []
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
                return b"".join(chunks), local
    except (OSError, ssl.SSLError, ConnectionError):
        return b"", None


async def start_real_tls_server(tmp_path, port):
    """«Настоящий» сервер на 127.0.0.2 (публично резолвимый адрес в тесте): свой сертификат."""
    cert, key = make_cert(tmp_path, "real", "x.y.example.test", ("x.y.example.test",))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    async def handle(reader, writer):
        await reader.read(65536)
        head = f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: {len(REAL_BODY)}\r\n\r\n".encode()
        writer.write(head + REAL_BODY)
        await writer.drain()
        await asyncio.sleep(0.3)
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.2", port, ssl=context)


async def start_front(runtime, tmp_path, resolve_map):
    """Фронт + внутренний aiohttp на эфемерных портах; SNI-разбор и сплайс — как в бою."""
    cert, key = make_cert(tmp_path, "server", "example.test", ("*.example.test", "example.test"))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    front = Front(runtime)

    async def whoami(request):
        peer = front.client_peer(request)
        return web.json_response({"remote": request.remote, "peer": list(peer) if peer else None})

    app = web.Application()
    app.router.add_get("/whoami", whoami)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    before = set(runner.addresses)
    await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context).start()
    internal_port = next(a[1] for a in runner.addresses if a not in before)
    port = free_port()
    await front.start("127.0.0.1", port, internal_port, cert_path=cert)

    async def fake_resolve(name):
        return resolve_map.get(name, [])

    front._resolve = fake_resolve  # noqa: SLF001 — подмена резолвера (в бою — внешний DNS)
    return front, runner, port, cert


async def stop_front(front, runner):
    await front.stop()
    await runner.cleanup()


def test_cert_covers_and_sans(tmp_path):
    assert cert_covers("a.example.test", ("*.example.test",))
    assert cert_covers("EXAMPLE.test.", ("example.test",))
    assert not cert_covers("x.y.example.test", ("*.example.test",))  # wildcard ровно на один уровень
    assert not cert_covers("other.test", ("*.example.test",))
    cert, _key = make_cert(tmp_path, "s", "example.test", ("*.example.test", "example.test"))
    assert dns_names_in_cert(cert) == ("*.example.test", "example.test")
    assert dns_names_in_cert(tmp_path / "нет.crt") == ()  # нечитаемый — пусто, всё сплайсом


async def test_covered_sni_splices_with_client_ip(tmp_path):
    runtime = Runtime(tmp_path)
    front, runner, port, _cert = await start_front(runtime, tmp_path, {"x.y.example.test": ["127.0.0.2"]})
    try:
        body, local = await asyncio.to_thread(tls_exchange, port, "a.example.test", "/whoami")
        payload = json.loads(body.split(b"\r\n\r\n", 1)[1])
        assert payload["remote"] == "127.0.0.1"          # сплайс: локальный пир aiohttp
        assert payload["peer"] == [local[0], local[1]]   # реальный адрес колонки — из таблицы фронта
    finally:
        await stop_front(front, runner)
        runtime.store.close()


async def test_no_sni_splices(tmp_path):
    runtime = Runtime(tmp_path)
    front, runner, port, _cert = await start_front(runtime, tmp_path, {})
    try:
        body, local = await asyncio.to_thread(tls_exchange, port, None, "/whoami")
        payload = json.loads(body.split(b"\r\n\r\n", 1)[1])
        assert payload["peer"] == [local[0], local[1]]   # без SNI — тоже таблица
    finally:
        await stop_front(front, runner)
        runtime.store.close()


async def test_uncovered_sni_tunnels_to_real_server(tmp_path):
    runtime = Runtime(tmp_path)
    front, runner, port, _cert = await start_front(runtime, tmp_path, {"x.y.example.test": ["127.0.0.2"]})
    real_server = await start_real_tls_server(tmp_path, port)
    try:
        body, _local = await asyncio.to_thread(tls_exchange, port, "x.y.example.test", "/music.flac")
        assert REAL_BODY in body  # клиент видит ответ настоящего сервера (его TLS, его сертификат)
        assert front.tunnels.get("x.y.example.test") == 1
    finally:
        real_server.close()
        await stop_front(front, runner)
        runtime.store.close()


async def test_loop_address_never_tunnels(tmp_path):
    runtime = Runtime(tmp_path)
    front, runner, port, _cert = await start_front(runtime, tmp_path, {"loop.y.example.test": ["127.0.0.1"]})
    try:
        body, _local = await asyncio.to_thread(tls_exchange, port, "loop.y.example.test", "/x")
        assert body == b""  # адрес самого сервера отфильтрован — туннель не открылся, соединение закрыто
        assert front.tunnels.get("loop.y.example.test") is None
    finally:
        await stop_front(front, runner)
        runtime.store.close()


def test_front_settings_roundtrip(tmp_path):
    cert, key = make_cert(tmp_path, "s", "localhost", ("localhost",))
    assert server_config.DEFAULTS["front"] is True  # по умолчанию включён
    value = server_config.validate(tmp_path, {"ssl": True, "cert": str(cert), "key": str(key),
                                              "front": False, "front_dns": "9.9.9.9, 1.0.0.1"})
    assert value["front"] is False and value["front_dns"] == ["9.9.9.9", "1.0.0.1"]  # из формы: через запятую
    with pytest.raises(ValueError, match="DNS"):
        server_config.validate(tmp_path, {"ssl": False, "front_dns": []})


async def test_serve_starts_front_and_keeps_client_peer(tmp_path):
    cert, key = make_cert(tmp_path, "s", "localhost", ("localhost",))
    main_port, http_port = free_port(), free_port()
    config = {"host": "127.0.0.1", "port": main_port, "ssl": True, "cert": str(cert), "key": str(key),
              "http_port": http_port, "front": True, "front_dns": ["8.8.8.8", "1.1.1.1"]}
    runtime = Runtime(tmp_path)
    token = control.write_token(tmp_path)
    app = server.create_app(runtime=runtime, control_token=token)
    app[control.INFO_KEY] = {"state": str(tmp_path), "config": config, "overridden": []}
    task = asyncio.ensure_future(server.serve(app, tmp_path, config))
    try:
        status = await wait_status(http_port, token)
        assert [entry["port"] for entry in status["listeners"]] == [main_port, http_port]
        assert status["listeners"][0]["front"] is True
        assert runtime.client_peer is not None  # обработчики получают реальный адрес колонки
        body, _local = await asyncio.to_thread(tls_exchange, main_port, "localhost", "/emulator/api/status")
        assert b'"standalone": true' in body  # покрытый SNI — ответ эмулятора через фронт
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


async def test_serve_without_front_keeps_plain_tls(tmp_path):
    cert, key = make_cert(tmp_path, "s", "localhost", ("localhost",))
    config = {"host": "127.0.0.1", "port": free_port(), "ssl": True, "cert": str(cert), "key": str(key),
              "http_port": 0, "front": False, "front_dns": ["8.8.8.8"]}
    runtime = Runtime(tmp_path)
    token = control.write_token(tmp_path)
    app = server.create_app(runtime=runtime, control_token=token)
    app[control.INFO_KEY] = {"state": str(tmp_path), "config": config, "overridden": []}
    task = asyncio.ensure_future(server.serve(app, tmp_path, config))
    try:
        status = await wait_status(config["port"], token, tls=True)
        assert status["listeners"][0]["front"] is False  # фронт выключен — прямой TLS-слушатель
        assert runtime.client_peer is None
        body, _local = await asyncio.to_thread(tls_exchange, config["port"], "localhost", "/emulator/api/status")
        assert b'"standalone": true' in body
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


async def wait_status(port, token, tls=False):
    from aiohttp import ClientSession
    scheme = "https" if tls else "http"
    async with ClientSession() as client:
        for _ in range(50):
            try:
                async with client.get(f"{scheme}://127.0.0.1:{port}/__control/status",
                                      headers={control.HEADER: token}, ssl=False) as response:
                    return await response.json()
            except OSError:
                await asyncio.sleep(0.1)
    raise AssertionError("сервер не поднялся")
