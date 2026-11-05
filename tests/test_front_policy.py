"""TASK-093: политика фронта для доменов вне сертификата — прозрачно / по листу / блокировать.

Режим и лист фронт читает живьём из server.json на каждое соединение: запись нового режима
между соединениями меняет поведение без пересоздания фронта. Приёмы — из
tests/test_front_passthrough.py (сертификат *.example.test, «настоящий» TLS на 127.0.0.2,
подменный резолвер фронта).
"""
import asyncio

import pytest
from aiohttp import ClientSession

from server_emulator import server, server_config
from server_emulator.front import Front
from server_emulator.runtime import Runtime
from tests.conftest import listen
from tests.test_front_passthrough import (REAL_BODY, free_port, make_cert, start_front,
                                          start_real_tls_server, stop_front, tls_exchange)

API = {"X-Emulator": "1"}


def set_policy(tmp_path, mode, allow=()):
    """server.json с политикой (SSL выключен — для validate сертификат не нужен)."""
    config = server_config.validate(tmp_path, {"ssl": False, "front_uncovered": mode,
                                               "front_allow": list(allow)})
    server_config.save(tmp_path, config)


async def test_block_then_tunnel_without_restart(tmp_path):
    runtime = Runtime(tmp_path)
    set_policy(tmp_path, "block")
    front, runner, port, _cert = await start_front(runtime, tmp_path, {"x.y.example.test": ["127.0.0.2"]})
    real_server = await start_real_tls_server(tmp_path, port)
    try:
        body, _local = await asyncio.to_thread(tls_exchange, port, "x.y.example.test", "/music.flac")
        assert body == b""  # блок: непокрытый домен отклонён, TLS не состоялся
        assert runtime.front_observed["x.y.example.test"]["count"] == 1
        assert list(runtime.front_sans) == ["*.example.test", "example.test"]
        # смена режима между соединениями — без пересоздания фронта
        set_policy(tmp_path, "tunnel")
        body, _local = await asyncio.to_thread(tls_exchange, port, "x.y.example.test", "/music.flac")
        assert REAL_BODY in body
        assert runtime.front_observed["x.y.example.test"]["count"] == 2
        # покрытое имя в обоих режимах — эмулятор
        body, _local = await asyncio.to_thread(tls_exchange, port, "a.example.test", "/whoami")
        assert b'"peer"' in body
    finally:
        real_server.close()
        await stop_front(front, runner)
        runtime.store.close()


async def test_allowlist_tunnels_only_list(tmp_path):
    runtime = Runtime(tmp_path)
    set_policy(tmp_path, "allowlist", ["*.strm.example.test", "exact.strm.example.test"])
    front, runner, port, _cert = await start_front(runtime, tmp_path,
                                                   {"x.strm.example.test": ["127.0.0.2"],
                                                    "exact.strm.example.test": ["127.0.0.2"]})
    real_server = await start_real_tls_server(tmp_path, port)
    try:
        for name in ("x.strm.example.test", "exact.strm.example.test"):
            body, _local = await asyncio.to_thread(tls_exchange, port, name, "/music.flac")
            assert REAL_BODY in body  # из листа: wildcard и точное имя — туннель
        body, _local = await asyncio.to_thread(tls_exchange, port, "other.y.example.test", "/x")
        assert body == b""  # вне листа — отклонено
        assert set(runtime.front_observed) == {"x.strm.example.test", "exact.strm.example.test",
                                               "other.y.example.test"}
    finally:
        real_server.close()
        await stop_front(front, runner)
        runtime.store.close()


def test_decision_table(tmp_path):
    front = Front(Runtime(tmp_path))
    front.sans = ("*.example.test",)
    allow = ("*.strm.example.test",)
    assert front._decision("a.example.test", "block", allow) == "splice"      # покрыт сертификатом
    assert front._decision(None, "block", allow) == "splice"                  # SNI нет
    assert front._decision("192.0.2.9", "block", allow) == "splice"           # литеральный IP
    assert front._decision("box.local", "block", allow) == "splice"           # локальная сеть
    front.sans = ()
    assert front._decision("x.y.example.test", "block", allow) == "splice"    # SAN не читаются — всё сплайсом
    front.sans = ("*.example.test",)
    assert front._decision("a.b.example.test", "allowlist", ("*.example.test",)) == "block"  # wildcard один уровень
    assert front._decision("x.strm.example.test", "allowlist", allow) == "tunnel"
    assert front._decision("other.y.example.test", "allowlist", allow) == "block"
    assert front._decision("x.y.example.test", "tunnel", ()) == "tunnel"
    assert front._decision("x.y.example.test", "block", ()) == "block"


def test_front_policy_validation(tmp_path):
    assert server_config.DEFAULTS["front_uncovered"] == "tunnel"
    mode, allow = server_config.front_policy({"front_uncovered": "allowlist",
                                              "front_allow": "*.A.Example.TEST.\n\n x.strm.example.test\n"
                                                             "*.a.example.test\n*.A.Example.TEST."})
    assert mode == "allowlist"
    assert allow == ["*.a.example.test", "x.strm.example.test"]  # нормализация, повторы и пустые — прочь
    config = server_config.validate(tmp_path, {"ssl": False, "front_uncovered": "allowlist",
                                               "front_allow": "a.example.test\nb.example.test"})
    assert config["front_allow"] == ["a.example.test", "b.example.test"]  # из textarea — по домену на строку
    with pytest.raises(ValueError, match="режим"):
        server_config.front_policy({"front_uncovered": "pass"})
    with pytest.raises(ValueError, match="не домен"):
        server_config.front_policy({"front_allow": ["a b.example.test"]})
    with pytest.raises(ValueError, match="не домен"):
        server_config.front_policy({"front_allow": ["**.example.test"]})
    with pytest.raises(ValueError, match="длиннее 253"):
        server_config.front_policy({"front_allow": ["a" * 250 + ".example.test"]})
    with pytest.raises(ValueError, match="100 записей"):
        server_config.front_policy({"front_allow": [f"n{i}.example.test" for i in range(101)]})


async def test_api_server_front(tmp_path):
    runtime = Runtime(tmp_path)
    app = server.create_app(runtime=runtime)
    runner, url = await listen(app)
    try:
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/server-front", headers=API) as response:
                assert response.status == 200
                data = await response.json()
            assert data["front_uncovered"] == "tunnel"  # значения по умолчанию, фронта нет
            assert data["front_allow"] == [] and data["observed"] == [] and data["sans"] == []
            async with client.put(url + "/emulator/api/server-front", headers=API,
                                  json={"front_uncovered": "pass"}) as response:
                assert response.status == 400  # неверный режим — файл не меняется
            async with client.put(url + "/emulator/api/server-front", headers=API,
                                  json={"front_uncovered": "allowlist",
                                        "front_allow": "*.strm.yandex.net\nx.y.example.test"}) as response:
                assert response.status == 200
                saved = await response.json()
            assert saved["front_uncovered"] == "allowlist"
            assert saved["front_allow"] == ["*.strm.yandex.net", "x.y.example.test"]
            assert server_config.load(tmp_path)["front_uncovered"] == "allowlist"
            # основная форма ключей не отправляет — политика из файла сохраняется
            body = {"host": "127.0.0.1", "port": free_port(), "ssl": False, "cert": "", "key": "",
                    "http_port": 0, "front": False, "front_dns": ["8.8.8.8"]}
            async with client.put(url + "/emulator/api/server", headers=API, json=body) as response:
                assert response.status == 200
            assert server_config.load(tmp_path)["front_uncovered"] == "allowlist"
            assert server_config.load(tmp_path)["front_allow"] == ["*.strm.yandex.net", "x.y.example.test"]
    finally:
        await runner.cleanup()
        await runtime.close()
