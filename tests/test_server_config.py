"""TASK-042: state/server.json — порты, SSL, сертификат; проверка, сохранение, откат, перезапуск."""
import datetime
import json
import socket

import pytest
from aiohttp import ClientSession

from server_emulator import control, server, server_config
from server_emulator.runtime import Runtime
from tests.conftest import listen


def self_signed(tmp_path, name="server"):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_defaults_and_file_roundtrip(tmp_path):
    assert server_config.load(tmp_path) == server_config.DEFAULTS
    assert server_config.DEFAULTS["port"] == 443 and server_config.DEFAULTS["http_port"] == 80
    config = {**server_config.DEFAULTS, "ssl": False, "port": 8443}
    server_config.save(tmp_path, config)
    assert server_config.load(tmp_path) == config
    (tmp_path / "server.json").write_text("{битый", encoding="utf-8")
    assert server_config.load(tmp_path) == server_config.DEFAULTS


def test_command_line_overrides_file(tmp_path):
    config, overridden = server_config.effective({**server_config.DEFAULTS, "port": 8443},
                                                 {"port": 9000, "host": None, "ssl": False})
    assert config["port"] == 9000 and config["ssl"] is False and config["host"] == "0.0.0.0"
    assert overridden == ["port", "ssl"]


def test_validation(tmp_path):
    with pytest.raises(ValueError, match="различаться"):
        server_config.validate(tmp_path, {"port": 8080, "http_port": 8080, "ssl": False})
    with pytest.raises(ValueError, match="65535"):
        server_config.validate(tmp_path, {"port": 70000, "ssl": False})
    with pytest.raises(ValueError, match="не указан сертификат"):
        server_config.validate(tmp_path, {"port": 443, "ssl": True})
    with pytest.raises(ValueError, match="не найден"):
        server_config.validate(tmp_path, {"port": 443, "ssl": True, "cert": "missing.crt"})
    broken = tmp_path / "broken.crt"
    broken.write_text("not a certificate", encoding="utf-8")
    with pytest.raises(ValueError, match="не загружается"):
        server_config.validate(tmp_path, {"port": 443, "ssl": True, "cert": str(broken)})
    cert, key = self_signed(tmp_path)
    value = server_config.validate(tmp_path, {"port": 443, "ssl": True, "cert": str(cert), "key": key.name})
    assert value["key"] == key.name   # относительный путь — от папки данных
    assert server_config.ssl_context(tmp_path, value) is not None


def test_rollback_restores_previous_file(tmp_path):
    first = {**server_config.DEFAULTS, "ssl": False, "port": 8001, "http_port": 0}
    server_config.save(tmp_path, first)
    server_config.confirm(tmp_path)
    assert server_config.rollback(tmp_path) is False          # без метки — нечего откатывать
    server_config.save(tmp_path, {**first, "port": 8002})
    assert server_config.rollback(tmp_path) is True
    assert server_config.load(tmp_path)["port"] == 8001


def test_local_url():
    assert server_config.local_url(server_config.DEFAULTS) == "http://127.0.0.1:80"
    assert server_config.local_url({**server_config.DEFAULTS, "http_port": 0}) == "https://127.0.0.1:443"
    assert server_config.local_url({"host": "::1", "port": 8000, "ssl": False, "http_port": 0}) == "http://[::1]:8000"


async def test_api_rejects_bad_certificate_and_keeps_file(tmp_path):
    runtime = Runtime(tmp_path)
    runner, url = await listen(server.create_app(runtime=runtime))
    api = url + "/emulator/api/server"
    headers = {"X-Emulator": "1"}
    try:
        async with ClientSession() as client:
            async with client.get(api, headers=headers) as response:
                body = await response.json()
            assert body["config"] == server_config.DEFAULTS
            broken = tmp_path / "broken.crt"
            broken.write_text("nope", encoding="utf-8")
            async with client.put(api, headers=headers, json={"port": 443, "ssl": True, "cert": str(broken)}) as response:
                assert response.status == 400
                assert "не загружается" in (await response.json())["error"]
            assert not (tmp_path / "server.json").exists()
            async with client.put(api, headers=headers, json={"port": 8080, "http_port": 8080, "ssl": False}) as response:
                assert response.status == 400
            port = free_port()
            async with client.put(api, headers=headers, json={"host": "127.0.0.1", "port": port, "ssl": False,
                                                              "http_port": 0}) as response:
                assert response.status == 200
                saved = await response.json()
            assert saved["url"] == f"http://127.0.0.1:{port}"
            assert json.loads((tmp_path / "server.json").read_text(encoding="utf-8"))["port"] == port
            async with client.post(api, headers=headers, json={"action": "restart"}) as response:
                assert response.status == 400                         # тестовый экземпляр без serve — без перезапуска
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_serve_listens_on_both_ports_with_tls_and_restarts(tmp_path):
    cert, key = self_signed(tmp_path)
    main_port, http_port = free_port(), free_port()
    config = {"host": "127.0.0.1", "port": main_port, "ssl": True, "cert": str(cert), "key": str(key),
              "http_port": http_port}
    runtime = Runtime(tmp_path)
    token = control.write_token(tmp_path)
    app = server.create_app(runtime=runtime, control_token=token)
    app[control.INFO_KEY] = {"state": str(tmp_path), "config": config, "overridden": []}
    import asyncio
    task = asyncio.ensure_future(server.serve(app, tmp_path, config))
    try:
        async with ClientSession() as client:
            for _ in range(50):
                try:
                    async with client.get(f"http://127.0.0.1:{http_port}/__control/status",
                                          headers={control.HEADER: token}) as response:
                        status = await response.json()
                        break
                except OSError:
                    await asyncio.sleep(0.1)
            assert [entry["port"] for entry in status["listeners"]] == [main_port, http_port]
            assert all(entry["ok"] for entry in status["listeners"])
            async with client.get(f"https://127.0.0.1:{main_port}/__control/status",
                                  headers={control.HEADER: token}, ssl=False) as response:
                assert response.status == 200                          # TLS на основном порту
            async with client.get(f"http://127.0.0.1:{http_port}/__control/status") as response:
                assert response.status == 403                          # без ключа
            async with client.post(f"http://127.0.0.1:{http_port}/__control/restart",
                                   headers={control.HEADER: token}) as response:
                assert response.status == 200
        assert await asyncio.wait_for(task, 10) == server_config.RESTART_CODE
    finally:
        if not task.done():
            task.cancel()
        await runtime.close()


async def test_serve_rolls_back_settings_when_port_is_busy(tmp_path):
    good = {"host": "127.0.0.1", "port": free_port(), "ssl": False, "http_port": 0, "cert": "", "key": ""}
    server_config.save(tmp_path, good)
    server_config.confirm(tmp_path)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    try:
        bad = {**good, "port": blocker.getsockname()[1]}
        server_config.save(tmp_path, bad)
        runtime = Runtime(tmp_path)
        app = server.create_app(runtime=runtime)
        app[control.INFO_KEY] = {}
        assert await server.serve(app, tmp_path, bad) == server_config.RESTART_CODE
        assert server_config.load(tmp_path)["port"] == good["port"]
        await runtime.close()
    finally:
        blocker.close()


def test_main_restarts_itself_with_same_arguments(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.delenv("ALICA_LAUNCHER", raising=False)

    async def fake_serve(app, state, config):
        calls["config"] = config
        return server_config.RESTART_CODE

    monkeypatch.setattr(server, "serve", fake_serve)
    monkeypatch.setattr(server.os, "execv", lambda exe, args: calls.setdefault("execv", (exe, args)))
    monkeypatch.setattr(server.sys, "orig_argv", ["python", "-m", "server_emulator.server", "--port", "9001"])
    with pytest.raises(SystemExit):
        server.main(["--port", "9001", "--no-ssl", "--state", str(tmp_path)])
    assert calls["config"]["port"] == 9001 and calls["config"]["ssl"] is False
    assert calls["execv"][1][1:] == ["-m", "server_emulator.server", "--port", "9001"]
