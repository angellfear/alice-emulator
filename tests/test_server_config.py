"""TASK-042: state/server.json — порты, SSL, сертификат; проверка, сохранение, откат, перезапуск."""
import datetime
import json
import socket

import pytest
from aiohttp import ClientSession, FormData

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


def test_quoted_and_padded_cert_paths_are_accepted(tmp_path):
    cert, key = self_signed(tmp_path)
    value = {"ssl": True, "port": 8443, "http_port": 0, "cert": f'  "{cert}" ', "key": f"'{key}'"}
    config = server_config.validate(tmp_path, value)
    assert config["cert"] == str(cert) and config["key"] == str(key)
    assert server_config.clean_path('"a b"') == "a b" and server_config.clean_path("  x  ") == "x"
    assert server_config.clean_path('"unbalanced') == '"unbalanced'


def test_missing_cert_message_says_path_is_on_server(tmp_path):
    with pytest.raises(ValueError, match="файл не найден на сервере") as info:
        server_config.validate(tmp_path, {"ssl": True, "port": 8443, "http_port": 0, "cert": "D:/nope/a.crt"})
    assert "загрузите файл" in str(info.value) and "Сертификат" in str(info.value)


def test_store_cert_saves_under_certs_and_validate_accepts(tmp_path):
    (tmp_path / "src").mkdir()
    cert, key = self_signed(tmp_path / "src")
    state = tmp_path / "state"
    stored = server_config.store_cert(state, "cert", "server.crt", cert.read_bytes())
    assert stored == {"path": "certs/server.crt", "has_key": False}
    assert (state / "certs" / "server.crt").read_bytes() == cert.read_bytes()
    stored_key = server_config.store_cert(state, "key", "server.key", key.read_bytes())
    assert stored_key["path"] == "certs/server.key" and stored_key["has_key"] is True
    config = server_config.validate(state, {"ssl": True, "port": 8443, "http_port": 0,
                                            "cert": stored["path"], "key": stored_key["path"]})
    assert config["cert"] == "certs/server.crt"
    assert not list((state / "certs").glob("*.tmp"))


def test_store_cert_rejects_non_pem_empty_and_oversized(tmp_path):
    with pytest.raises(ValueError, match="BEGIN"):
        server_config.store_cert(tmp_path, "cert", "a.crt", b"just text")
    with pytest.raises(ValueError, match="PEM"):
        server_config.store_cert(tmp_path, "cert", "a.crt", bytes([0x30, 0x82, 0x01, 0xFF]) * 8)
    with pytest.raises(ValueError, match="пустой"):
        server_config.store_cert(tmp_path, "cert", "a.crt", b"")
    big = b"-----BEGIN CERTIFICATE-----\n" + b"A" * server_config.CERT_LIMIT
    with pytest.raises(ValueError, match="больше"):
        server_config.store_cert(tmp_path, "cert", "a.crt", big)
    with pytest.raises(ValueError, match="закрытого ключа"):
        server_config.store_cert(tmp_path, "key", "a.key", b"-----BEGIN CERTIFICATE-----\nAAAA\n")
    with pytest.raises(ValueError, match="Тип файла"):
        server_config.store_cert(tmp_path, "other", "a.crt", b"-----BEGIN CERTIFICATE-----")
    assert not (tmp_path / "certs").exists() or not any((tmp_path / "certs").iterdir())


@pytest.mark.parametrize("name, expected", [
    ("../../evil.crt", "certs/evil.crt"),
    ("..\\..\\windows\\evil.pem", "certs/evil.pem"),
    ("C:\\certs\\my cert (1).crt", "certs/my_cert_1_.crt"),
    ("", "certs/server.crt"),
    ("..", "certs/server.crt"),
    ("noext", "certs/noext.crt"),
])
def test_store_cert_sanitizes_file_name(tmp_path, name, expected):
    state = tmp_path / "state"
    stored = server_config.store_cert(state, "cert", name, b"-----BEGIN CERTIFICATE-----\nAAAA\n")
    assert stored["path"] == expected
    assert (state / expected).is_file()
    assert [p for p in state.rglob("*") if p.is_file()] == [state / expected]


def test_combined_pem_works_with_empty_key(tmp_path):
    cert, key = self_signed(tmp_path)
    combined = cert.read_bytes() + key.read_bytes()
    state = tmp_path / "state"
    stored = server_config.store_cert(state, "cert", "both.pem", combined)
    assert stored == {"path": "certs/both.pem", "has_key": True}
    config = server_config.validate(state, {"ssl": True, "port": 8443, "http_port": 0, "cert": stored["path"], "key": ""})
    assert config["key"] == "" and server_config.ssl_context(state, config) is not None


async def test_api_uploads_cert_and_does_not_touch_settings(tmp_path):
    cert, key = self_signed(tmp_path)
    runtime = Runtime(tmp_path / "state")
    runner, url = await listen(server.create_app(runtime=runtime))
    api = url + "/emulator/api/server-cert"
    headers = {"X-Emulator": "1"}

    def form(kind, data, filename="server.crt"):
        body = FormData()
        body.add_field("kind", kind)
        body.add_field("file", data, filename=filename, content_type="application/octet-stream")
        return body
    try:
        async with ClientSession() as client:
            async with client.post(api, data=form("cert", cert.read_bytes())) as response:
                assert response.status == 403                          # без X-Emulator
            async with client.post(api, headers=headers, data=form("cert", cert.read_bytes(), "server.crt")) as response:
                assert response.status == 200
                assert await response.json() == {"path": "certs/server.crt", "has_key": False}
            async with client.post(api, headers=headers, data=form("key", key.read_bytes(), "server.key")) as response:
                assert (await response.json())["path"] == "certs/server.key"
            async with client.post(api, headers=headers, data=form("cert", b"not a pem")) as response:
                assert response.status == 400 and "BEGIN" in (await response.json())["error"]
            async with client.post(api, headers=headers, data=form("cert", b"-----BEGIN X" + b"A" * (2 * 1024 * 1024))) as response:
                assert response.status == 400
            async with client.post(api, headers=headers, json={"kind": "cert"}) as response:
                assert response.status == 400
            async with client.put(url + "/emulator/api/server", headers=headers,
                                  json={"host": "127.0.0.1", "port": 8443, "http_port": 0, "ssl": True,
                                        "cert": "certs/server.crt", "key": "certs/server.key"}) as response:
                assert response.status == 200
    finally:
        await runner.cleanup()
        await runtime.close()
    assert sorted(p.name for p in (tmp_path / "state" / "certs").iterdir()) == ["server.crt", "server.key"]
