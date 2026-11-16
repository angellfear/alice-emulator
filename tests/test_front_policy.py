"""Обслуживаемые домены и политика для необслуживаемых — прозрачно / блокировать.

Списки (обязательные, дополнительные, пользовательские) и режим фронт читает живьём из
server.json на каждое соединение: запись нового режима между соединениями меняет поведение
без пересоздания фронта. Приёмы — из tests/test_front_passthrough.py (сертификат
*.example.test, «настоящий» TLS на 127.0.0.2, подменный резолвер фронта).
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


def set_policy(tmp_path, mode, off=(), extra=()):
    """server.json с политикой и списками обслуживаемых (SSL выключен — сертификат не нужен)."""
    config = server_config.validate(tmp_path, {"ssl": False, "front_uncovered": mode,
                                               "serve_off": list(off), "serve_extra": list(extra)})
    server_config.save(tmp_path, config)


async def test_block_then_tunnel_without_restart(tmp_path):
    runtime = Runtime(tmp_path)
    set_policy(tmp_path, "block", extra=["a.example.test"])
    front, runner, port, _cert = await start_front(runtime, tmp_path, {"x.y.example.test": ["127.0.0.2"]})
    real_server = await start_real_tls_server(tmp_path, port)
    try:
        body, _local = await asyncio.to_thread(tls_exchange, port, "x.y.example.test", "/music.flac")
        assert body == b""  # блок: необслуживаемый домен отклонён, TLS не состоялся
        assert runtime.front_observed["x.y.example.test"]["count"] == 1
        assert list(runtime.front_sans) == ["*.example.test", "example.test"]
        # смена режима между соединениями — без пересоздания фронта
        set_policy(tmp_path, "tunnel", extra=["a.example.test"])
        body, _local = await asyncio.to_thread(tls_exchange, port, "x.y.example.test", "/music.flac")
        assert REAL_BODY in body
        assert runtime.front_observed["x.y.example.test"]["count"] == 2
        # обслуживаемый (пользовательский список) в обоих режимах — эмулятор
        body, _local = await asyncio.to_thread(tls_exchange, port, "a.example.test", "/whoami")
        assert b'"peer"' in body
    finally:
        real_server.close()
        await stop_front(front, runner)
        await runtime.store.close()


async def test_optional_off_and_extra_decide_splice(tmp_path):
    """Выключенный дополнительный домен ведёт себя как необслуживаемый (туннель/блок),
    включённый и пользовательский список — эмулятор; смена списков — без перезапуска."""
    runtime = Runtime(tmp_path)
    set_policy(tmp_path, "tunnel", off=["clck.yandex.net"], extra=["a.example.test"])
    front, runner, port, _cert = await start_front(runtime, tmp_path,
                                                   {"x.y.example.test": ["127.0.0.2"],
                                                    "b.example.test": ["127.0.0.2"],
                                                    "clck.yandex.net": ["127.0.0.2"]},
                                                   extra_sans=("clck.yandex.net",))
    real_server = await start_real_tls_server(tmp_path, port)
    try:
        body, _local = await asyncio.to_thread(tls_exchange, port, "clck.yandex.net", "/music.flac")
        assert REAL_BODY in body  # покрыт сертификатом, но выключен — туннель
        assert runtime.front_observed["clck.yandex.net"]["count"] == 1
        body, _local = await asyncio.to_thread(tls_exchange, port, "a.example.test", "/whoami")
        assert b'"peer"' in body  # пользовательский список — эмулятор
        body, _local = await asyncio.to_thread(tls_exchange, port, "b.example.test", "/whoami")
        assert REAL_BODY in body  # покрыт, но не в списках — туннель по политике
        # включили дополнительный — соединение стало эмулятором без перезапуска
        set_policy(tmp_path, "tunnel", extra=["a.example.test"])
        body, _local = await asyncio.to_thread(tls_exchange, port, "clck.yandex.net", "/whoami")
        assert b'"peer"' in body
        # блок: выключенный и не в списках отклоняются, пользовательский список — нет
        set_policy(tmp_path, "block", off=["clck.yandex.net"], extra=["a.example.test"])
        body, _local = await asyncio.to_thread(tls_exchange, port, "x.y.example.test", "/x")
        assert body == b""
        body, _local = await asyncio.to_thread(tls_exchange, port, "clck.yandex.net", "/x")
        assert body == b""
        body, _local = await asyncio.to_thread(tls_exchange, port, "a.example.test", "/whoami")
        assert b'"peer"' in body
    finally:
        real_server.close()
        await stop_front(front, runner)
        await runtime.store.close()


def test_decision_table(tmp_path):
    front = Front(Runtime(tmp_path))
    front.sans = ("*.example.test",)
    assert front._decision("a.example.test", "block", (), ("a.example.test",)) == "splice"  # в списке
    assert front._decision("a.example.test", "block", (), ()) == "block"     # покрыт, но не обслуживаем
    assert front._decision("a.example.test", "tunnel", (), ()) == "tunnel"
    assert front._decision(None, "block", (), ()) == "splice"                 # SNI нет
    assert front._decision("192.0.2.9", "block", (), ()) == "splice"          # литеральный IP
    assert front._decision("box.local", "block", (), ()) == "splice"          # локальная сеть
    assert front._decision("alica.lan", "block", (), ()) == "splice"          # локальный домен сервера
    front.sans = ()
    assert front._decision("x.y.example.test", "block", (), ()) == "splice"   # SAN не читаются — всё сплайсом
    # обязательные: покрытый — сплайс, непокрытый — всегда блок (эмуляцию молчей не подменяем)
    front.sans = ("quasar.yandex.net",)
    assert front._decision("quasar.yandex.net", "block", (), ()) == "splice"
    assert front._decision("rpc.alice.yandex.net", "tunnel", (), ()) == "block"
    # дополнительные: включённый покрытый — сплайс, выключенный — по политике
    front.sans = ("clck.yandex.net",)
    assert front._decision("clck.yandex.net", "tunnel", (), ()) == "splice"
    assert front._decision("clck.yandex.net", "tunnel", ("clck.yandex.net",), ()) == "tunnel"
    assert front._decision("clck.yandex.net", "block", ("clck.yandex.net",), ()) == "block"
    assert front._decision("clck.yandex.net", "block", (), ()) == "splice"
    # пользовательский список непокрытого домена — по политике (терминировать нечем)
    front.sans = ("*.example.test",)
    assert front._decision("other.test", "tunnel", (), ("other.test",)) == "tunnel"
    # хосты авторизации — всегда туннель в Яндекс, независимо от режима и списков
    assert front._decision("oauth.yandex.ru", "block", (), ()) == "tunnel"
    assert front._decision("mobileproxy.passport.yandex.net", "block", (), ()) == "tunnel"
    front.sans = ("oauth.yandex.ru", "*.passport.yandex.net")
    assert front._decision("oauth.yandex.ru", "block", (), ("oauth.yandex.ru",)) == "tunnel"  # даже из списка
    assert front._decision("mobileproxy.passport.yandex.net", "block", (), ()) == "tunnel"


async def test_auth_hosts_tunnel_regardless_of_policy(tmp_path):
    """Хосты авторизации — всегда туннель настоящему Яндексу: режим «block» их не
    отклоняет, покрытие сертификатом и пользовательский список не делают их эмулятором
    (вход на колонке — WebView паспорта, Chromium отвергает наш сертификат). Политика
    их не отмечает в замеченных — в пользовательский список их добавлять не нужно."""
    runtime = Runtime(tmp_path)
    set_policy(tmp_path, "block", extra=["passport.yandex.ru"])
    front, runner, port, _cert = await start_front(runtime, tmp_path,
                                                   {"passport.yandex.ru": ["127.0.0.2"],
                                                    "oauth.yandex.ru": ["127.0.0.2"]})
    real_server = await start_real_tls_server(tmp_path, port)
    try:
        for name in ("passport.yandex.ru", "oauth.yandex.ru"):
            body, _local = await asyncio.to_thread(tls_exchange, port, name, "/music.flac")
            assert REAL_BODY in body  # настоящий сервер ответил, несмотря на block и список
        assert "passport.yandex.ru" not in runtime.front_observed
        assert "oauth.yandex.ru" not in runtime.front_observed
    finally:
        real_server.close()
        await stop_front(front, runner)
        await runtime.store.close()


def test_serve_policy_validation(tmp_path):
    assert server_config.DEFAULTS["front_uncovered"] == "tunnel"
    assert server_config.DEFAULTS["serve_off"] == [] and server_config.DEFAULTS["serve_extra"] == []
    off, extra = server_config.serve_policy({"serve_off": "clck.yandex.net\nunknown.host\nCLCK.yandex.net.",
                                             "serve_extra": "A.Example.TEST.\n\n x.s3.example.test\n"
                                                            "A.Example.TEST."})
    assert off == ["clck.yandex.net"]  # нормализация, повторы и не-дополнительные — прочь
    assert extra == ["a.example.test", "x.s3.example.test"]  # нормализация, повторы и пустые — прочь
    config = server_config.validate(tmp_path, {"ssl": False, "serve_off": ["push.yandex.ru"],
                                               "serve_extra": "a.example.test\nb.example.test"})
    assert config["serve_off"] == ["push.yandex.ru"] and config["serve_extra"] == ["a.example.test", "b.example.test"]
    with pytest.raises(ValueError, match="режим"):
        server_config.front_mode({"front_uncovered": "pass"})
    with pytest.raises(ValueError, match="режим"):
        server_config.front_mode({"front_uncovered": "allowlist"})  # режим удалён
    with pytest.raises(ValueError, match="без шаблонов"):
        server_config.serve_policy({"serve_extra": ["*.example.test"]})  # пользовательский список без `*`
    with pytest.raises(ValueError, match="без шаблонов"):
        server_config.serve_policy({"serve_extra": ["a b.example.test"]})
    with pytest.raises(ValueError, match="длиннее 253"):
        server_config.serve_policy({"serve_extra": ["a" * 250 + ".example.test"]})
    with pytest.raises(ValueError, match="100 записей"):
        server_config.serve_policy({"serve_extra": [f"n{i}.example.test" for i in range(101)]})
    # режим allowlist в файле — разовый перенос в tunnel
    import json
    from pathlib import Path
    (tmp_path / "server.json").write_text(json.dumps({"front_uncovered": "allowlist",
                                                      "front_allow": ["a.example.test"]}), encoding="utf-8")
    loaded = server_config.load(tmp_path)
    assert loaded["front_uncovered"] == "tunnel" and "front_allow" not in loaded


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
            assert data["serve_off"] == [] and data["serve_extra"] == [] and data["extra"] == []
            assert data["observed"] == [] and data["sans"] == []
            async with client.put(url + "/emulator/api/server-front", headers=API,
                                  json={"front_uncovered": "pass"}) as response:
                assert response.status == 400  # неверный режим — файл не меняется
            async with client.put(url + "/emulator/api/server-front", headers=API,
                                  json={"serve_extra": "*.strm.yandex.net"}) as response:
                assert response.status == 400  # пользовательский список без шаблонов
            async with client.put(url + "/emulator/api/server-front", headers=API,
                                  json={"front_uncovered": "block"}) as response:
                assert response.status == 200
                saved = await response.json()
            assert saved["front_uncovered"] == "block"  # частичная правка: списки не тронуты
            assert saved["serve_off"] == [] and saved["serve_extra"] == []
            async with client.put(url + "/emulator/api/server-front", headers=API,
                                  json={"serve_off": ["clck.yandex.net", "unknown.host"],
                                        "serve_extra": "x.y.example.test"}) as response:
                assert response.status == 200
                saved = await response.json()
            assert saved["serve_off"] == ["clck.yandex.net"]  # не-дополнительные отбрасываются
            assert saved["serve_extra"] == ["x.y.example.test"]
            assert server_config.load(tmp_path)["front_uncovered"] == "block"
            # основная форма ключей не отправляет — списки и режим из файла сохраняются
            body = {"host": "127.0.0.1", "port": free_port(), "ssl": False, "cert": "", "key": "",
                    "http_port": 0, "front": False, "front_dns": ["8.8.8.8"]}
            async with client.put(url + "/emulator/api/server", headers=API, json=body) as response:
                assert response.status == 200
            loaded = server_config.load(tmp_path)
            assert loaded["front_uncovered"] == "block" and loaded["serve_off"] == ["clck.yandex.net"]
            assert loaded["serve_extra"] == ["x.y.example.test"]
    finally:
        await runner.cleanup()
        await runtime.close()
