"""OAuth/Passport: режим-зависимые заглушки (settings["oauth"]["mode"]) и туннель наверх.

Образец ответов — код quasar-server; токены заглушек детерминированные (VAT./VRT.) и
note_token музыки их не захватывает. Фейковый upstream — как в test_upstream.py:
алиас хоста на локальный сервер, настоящие хосты не трогаются.
"""
import base64
import json

import pytest
from aiohttp import ClientSession, web

from server_emulator import music, oauth_stubs
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen

OAUTH = {"Host": "oauth.yandex.ru"}
PASSPORT = {"Host": "mobileproxy.passport.yandex.net"}
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def registry(runtime, route_mode=None):
    runtime.identities.mutate({"operation": "user", "id": "u1", "name": "Аня"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-1", "user_id": "u1",
                               "device_ids": ["id-1"]})
    if route_mode:
        runtime.identities.mutate({"operation": "route_mode", "sn": "SN-1", "mode": route_mode})


def alias_upstream(runtime, port, hosts=oauth_stubs.HOSTS):
    runtime.settings["aliases"] = {host: f"http://127.0.0.1:{port}" for host in hosts}


async def post_form(url, path, headers, body):
    async with ClientSession() as client:
        async with client.post(url + path, headers={**headers, **FORM}, data=body) as response:
            return response.status, await response.json()


async def get_json(url, path, headers):
    async with ClientSession() as client:
        async with client.get(url + path, headers=headers) as response:
            return response.status, await response.json()


@pytest.mark.asyncio
async def test_stubs_emulate_all_routes(tmp_path):
    """Эмуляция: все grant_type, token_by_sessionid, short_info по users, статика образца."""
    runtime = Runtime(tmp_path)
    registry(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        status, body = await post_form(url, "/token", OAUTH,
                                       "grant_type=authorization_code&code=abc&device_id=id-1")
        assert status == 200
        assert body["access_token"].startswith("VAT.") and len(body["access_token"]) == 68
        assert body["refresh_token"].startswith("VRT.") and body["expires_in"] == 315360000
        assert body["token_type"] == "bearer"
        status, body = await post_form(url, "/token", OAUTH,
                                       "grant_type=x-token&access_token=test-token")
        # uid — число: SDK паспорта колонки разбирает его как long, строка роняет разбор
        assert status == 200 and body == {"access_token": "test-token", "expires_in": 315360000,
                                          "token_type": "bearer", "uid": oauth_stubs.numeric_uid("u1")}
        assert isinstance(body["uid"], int) and body["uid"] > 0
        status, body = await post_form(url, "/token", OAUTH, "grant_type=bogus")
        assert status == 400 and body == {"error": "unsupported_grant_type"}
        status, body = await post_form(url, "/1/token", PASSPORT,
                                       "grant_type=authorization_code&code=abc&device_id=id-1")
        assert status == 200 and body["access_token"].startswith("VAT.")
        status, body = await post_form(url, "/1/bundle/oauth/token_by_sessionid", PASSPORT, "")
        assert status == 200 and body["access_token"].startswith("VAT.")
        assert body["status"] == "ok" and len(body["access_token"]) == 68
        status, body = await get_json(url, "/1/bundle/account/short_info", PASSPORT)
        assert status == 200 and body["status"] == "ok"
        # uid/master_uid — число (SDK паспорта разбирает long), детерминированное из реестра
        assert body["uid"] == oauth_stubs.numeric_uid("u1") == body["master_uid"] == \
            body["master_members"][0]["uid"]
        assert body["display_name"] == "Аня" and body["display_login"] == "Аня"
        assert body["x_token_client_id"] and body["partitions"] == ["default"]  # каркас образца
        for path, key in (("/1/am/sso/config.json", "allowed_sso_apps"),
                          ("/1/am/config.json", "am_config"),
                          ("/1/badges-specification.json", "badges_specification")):
            status, body = await get_json(url, path, PASSPORT)
            assert status == 200 and key in body
        status, body = await get_json(url, "/1/unknown", PASSPORT)
        assert status == 404 and body == {"error": "emulator_handler_not_found"}
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_short_info_fallback_first_owner(tmp_path):
    """Устройства без владельца — профиль первого владельца users."""
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({"operation": "user", "id": "u1", "name": "Аня"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-1", "device_ids": ["id-1"]})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        status, body = await get_json(url, "/1/bundle/account/short_info", PASSPORT)
        assert status == 200 and body["uid"] == oauth_stubs.numeric_uid("u1") \
            and body["display_name"] == "Аня"
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_numeric_uid_deterministic_per_user(tmp_path):
    """uid заглушек — число, детерминированное по id пользователя: SDK паспорта колонки
    разбирает его как long, строка роняет разбор ответа — сессия не восстанавливается."""
    runtime = Runtime(tmp_path)
    registry(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        status, first = await post_form(url, "/token", OAUTH,
                                        "grant_type=x-token&access_token=test-token")
        status, second = await post_form(url, "/token", OAUTH,
                                         "grant_type=x-token&access_token=test-token")
        assert status == 200
        assert first["uid"] == second["uid"] == oauth_stubs.numeric_uid("u1")
        assert oauth_stubs.numeric_uid("u1") != oauth_stubs.numeric_uid("u2")  # пользователи различимы
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_linked_real_account_uid_wins_over_synthetic(tmp_path):
    """Привязанный через «Подтянуть из аккаунта» настоящий Яндекс-аккаунт — его uid
    приоритетнее синтетического: колонка в «Эмуляции» видит тот же uid, что от
    настоящего Яндекса, не придуманный по id реестра."""
    from server_emulator import account
    runtime = Runtime(tmp_path)
    registry(runtime)
    account.AccountSession(runtime)._save("real-x-token", "tester", 1130000012345678)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        status, body = await post_form(url, "/token", OAUTH,
                                       "grant_type=x-token&access_token=test-token")
        assert status == 200 and body["uid"] == 1130000012345678
        assert body["uid"] != oauth_stubs.numeric_uid("u1")
        status, info = await get_json(url, "/1/bundle/account/short_info", PASSPORT)
        assert status == 200 and info["uid"] == info["master_uid"] == \
            info["master_members"][0]["uid"] == 1130000012345678
    finally:
        await runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_auto_mode_emulate_device_gets_stubs(tmp_path):
    """auto (по умолчанию) + устройство в эмуляции — заглушки, наверх не ходим."""
    calls = []
    runtime = Runtime(tmp_path)
    registry(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    try:
        status, body = await post_form(url, "/token", OAUTH,
                                       "grant_type=authorization_code&code=abc&device_id=id-1")
        assert status == 200 and body["access_token"].startswith("VAT.")
        assert calls == []  # заглушка ответила локально
    finally:
        await runner.cleanup()
        await upstream_runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_auto_mode_proxy_device_tunnels(tmp_path):
    """auto + устройство «Прозрачно» — туннель наверх, ответ релеится."""
    calls = []
    runtime = Runtime(tmp_path)
    registry(runtime, route_mode="proxy")
    runner, url = await listen(create_app(runtime=runtime))
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    try:
        status, body = await post_form(url, "/token?device_id=id-1", OAUTH,
                                       "grant_type=x-token&access_token=real-token-1234567890")
        assert status == 200 and body["access_token"] == "real-token-1234567890"
        assert [c[0] for c in calls] == ["/token?device_id=id-1"]  # дошло до настоящего сервера
    finally:
        await runner.cleanup()
        await upstream_runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_upstream_mode_always_tunnels(tmp_path):
    """Режим «напрямую»: туннель при любом маршруте устройства."""
    calls = []
    runtime = Runtime(tmp_path)
    registry(runtime)  # emulate
    runtime.settings["oauth"] = {"mode": "upstream"}
    runner, url = await listen(create_app(runtime=runtime))
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    try:
        status, body = await post_form(url, "/token", OAUTH,
                                       "grant_type=x-token&access_token=real-token-1234567890")
        assert status == 200 and [c[0] for c in calls] == ["/token"]
    finally:
        await runner.cleanup()
        await upstream_runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_emulate_mode_stubs_even_for_proxy(tmp_path):
    """Режим «эмуляция»: заглушки даже устройству «Прозрачно», наверх не ходим."""
    calls = []
    runtime = Runtime(tmp_path)
    registry(runtime, route_mode="proxy")
    runtime.settings["oauth"] = {"mode": "emulate"}
    runner, url = await listen(create_app(runtime=runtime))
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    try:
        status, body = await post_form(url, "/token", OAUTH,
                                       "grant_type=authorization_code&code=abc&device_id=id-1")
        assert status == 200 and body["access_token"].startswith("VAT.")
        assert calls == []
    finally:
        await runner.cleanup()
        await upstream_runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_broken_json_upstream_is_honest_error(tmp_path):
    """Наверх вернулся не-JSON при 200 — колонке честная ошибка, мусор не уходит."""
    calls = []
    runtime = Runtime(tmp_path)
    registry(runtime)  # emulate + режим upstream
    runtime.settings["oauth"] = {"mode": "upstream"}
    runner, url = await listen(create_app(runtime=runtime))
    async def broken(request):
        return web.Response(status=200, text="<html>мусор</html>", content_type="text/html")
    app = web.Application()
    app.router.add_route("*", "/{p:.*}", broken)
    upstream_runner, uurl = await listen(app)
    alias_upstream(runtime, int(uurl.rsplit(":", 1)[1]))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/token", headers={**OAUTH, **FORM},
                                   data="grant_type=x-token&access_token=test-token") as response:
                assert response.status == 502
                body = await response.json()
                assert body["error"] == "upstream_bad_body"
    finally:
        await runner.cleanup()
        await upstream_runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_mode_switch_applies_live(tmp_path):
    """Переключатель читается живьём: PUT settings — поведение меняется без перезапуска."""
    calls = []
    runtime = Runtime(tmp_path)
    registry(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    try:
        status, body = await post_form(url, "/token", OAUTH,
                                       "grant_type=x-token&access_token=test-token")
        assert status == 200 and body["uid"] == oauth_stubs.numeric_uid("u1")  # auto + emulate — заглушка
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/settings",
                                  headers={"X-Emulator": "1"}) as response:
                settings = await response.json()
            settings["oauth"] = {"mode": "upstream"}
            async with client.put(url + "/emulator/api/settings", headers={"X-Emulator": "1"},
                                  json=settings) as response:
                assert response.status == 200
        status, body = await post_form(url, "/token", OAUTH,
                                       "grant_type=x-token&access_token=real-token-1234567890")
        assert status == 200 and body["access_token"] == "real-token-1234567890"
        assert len(calls) == 1  # тот же процесс — туннель без перезапуска
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/settings",
                                  headers={"X-Emulator": "1"}) as response:
                settings = await response.json()  # свежая ревизия после первого PUT
            settings["oauth"] = {"mode": "emulate"}
            async with client.put(url + "/emulator/api/settings", headers={"X-Emulator": "1"},
                                  json=settings) as response:
                assert response.status == 200
        status, body = await post_form(url, "/token", OAUTH,
                                       "grant_type=x-token&access_token=test-token")
        assert status == 200 and body["uid"] == oauth_stubs.numeric_uid("u1") and len(calls) == 1
        async with ClientSession() as client:
            async with client.put(url + "/emulator/api/settings", headers={"X-Emulator": "1"},
                                  json={"oauth": {"mode": "bogus"}}) as response:
                assert response.status == 400  # validation: auto | emulate | upstream
    finally:
        await runner.cleanup()
        await upstream_runner.cleanup()
        await runtime.close()


@pytest.mark.asyncio
async def test_stub_tokens_are_not_captured(tmp_path):
    """Заглушечный токен (VAT.) note_token не захватывает; настоящий не затирается."""
    runtime = Runtime(tmp_path)
    registry(runtime)
    music._CAPTURED.pop("oauth", None)
    try:
        assert music.note_token(runtime, "VAT." + "0" * 64) is False
        assert music.captured_oauth() == ""
        assert music.note_token(runtime, "yandex-real-oauth-token-123456") is True
        assert music.captured_oauth() == "yandex-real-oauth-token-123456"
        # колонка в эмуляции получила токен заглушки и пришла с ним — настоящий прежний
        assert music.note_token(runtime, "VAT." + "1" * 64) is False
        assert music.captured_oauth() == "yandex-real-oauth-token-123456"
        assert runtime.store.meta("yandex_oauth") is not None  # персистентность настоящего
    finally:
        music._CAPTURED.pop("oauth", None)
        await runtime.close()


@pytest.mark.asyncio
async def test_stub_token_via_http_does_not_touch_capture(tmp_path):
    """Полный путь: заглушка выдала токен — колонка пришла с ним — сохранённый настоящий прежний."""
    runtime = Runtime(tmp_path)
    registry(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    music._CAPTURED.pop("oauth", None)
    try:
        assert music.note_token(runtime, "yandex-real-oauth-token-123456") is True
        async with ClientSession() as client:
            async with client.post(url + "/token", headers={**OAUTH, **FORM},
                                   data="grant_type=authorization_code&code=abc&device_id=id-1") as response:
                body = await response.json()
            stub_token = body["access_token"]
            async with client.get(url + "/check_updates", headers={"Host": "quasar.yandex.net",
                                  "Authorization": "OAuth " + stub_token}) as response:
                assert response.status == 200
        assert music.captured_oauth() == "yandex-real-oauth-token-123456"
    finally:
        music._CAPTURED.pop("oauth", None)
        await runner.cleanup()
        await runtime.close()


async def start_upstream(calls):
    """Локальный upstream-сервер: фиксирует пути, отвечает эхом grant_type."""
    async def handler(request):
        calls.append((request.rel_url.path_qs, dict(request.headers)))
        form = await request.post()
        if form.get("grant_type") == "x-token":
            return web.json_response({"access_token": str(form.get("access_token")),
                                      "expires_in": 315360000, "token_type": "bearer", "uid": 42})
        return web.json_response({"access_token": "VAT.upstream", "expires_in": 315360000,
                                  "refresh_token": "VRT.upstream", "token_type": "bearer"})

    app = web.Application()
    app.router.add_route("*", "/{p:.*}", handler)
    runner, url = await listen(app)
    return runner, int(url.rsplit(":", 1)[1])
