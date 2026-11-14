"""«Подтянуть из аккаунта» (конфигуратор): клиент аккаунта Яндекса и импорт значений
в каталог «Параметры» — против поддельного сервера Яндекса и фейковой записи журнала
get_sync_info. Настоящий Яндекс не вызывается; в фикстурах фиктивные данные
(серийник-плейсхолдер, московские координаты центра, test-* значения, секретов нет).
Секреты (x_token, glagol-ключи) наружу API не выходят — проверяется по ответам.
"""
import http.cookies
import json

import pytest
import yarl
from aiohttp import web
from aiohttp.test_utils import unused_port

from server_emulator import account
from server_emulator.runtime import Runtime

SN = "C00T10001B6SAK"  # фиктивный серийник-плейсхолдер
SYNC_ANSWER = {
    "status": "ok",
    "config": {
        "account_config": {"user_wifi_config": {"wifi_hash": "test-wifi-hash"}},
        "device_config": {"name": "Дуо тест", "location": {"latitude": 55.7558, "longitude": 37.6173}},
    },
    "glagol": {"security": {"server_certificate": "test-cert", "server_private_key": "test-key"}},
}
IOT_DEVICES = {"status": "ok", "households": [{"name": "Дом", "all": [
    {"id": "d1", "name": "Дуо тест", "room": "Гостиная",
     "quasar_info": {"device_id": SN, "platform": "chiron"}}]}]}
IOT_CONFIG = {"quasar_config": {"device_config": {
    "name": "Дуо тест", "location": {"latitude": 55.7558, "longitude": 37.6173}}}}


def device_row(sn=SN, user_id="u1"):
    return {"sn": sn, "user_id": user_id, "name": sn, "profile": {"platform": "chiron"},
            "device_id": sn}


class FakeYandex:
    """Подменный Яндекс: passport (QR, cookies, x_token), iot-API. Секреты не журналируются."""

    def __init__(self):
        self.seen = []  # (path, заголовки интереса)
        self.exchanges = []  # формы /1/token (обмен x_token на токен сервиса)

    async def route(self, request):
        path = request.path
        self.seen.append((path, dict(request.headers)))
        if path == "/pwl-yandex":
            return web.Response(text='var __CSRF__ = "csrf-1";')
        if path == "/pwl-yandex/api/passport/auth/password/submit":
            return web.json_response({"track_id": "track-1"})
        if path == "/pwl-yandex/api/passport/auth/magic/code":
            return web.json_response({"link": "https://passport.example/qr"})
        if path == "/pwl-yandex/api/passport/auth/magic/code/status":
            return web.json_response({"state": "otp_auth_finished", "trackId": "track-2"})
        if path == "/pwl-yandex/api/passport/sessions/get_session":
            response = web.Response(text="")
            response.headers["Set-Cookie"] = "yandexuid=testuid; Domain=.yandex.ru; Path=/"
            return response
        if path == "/1/bundle/oauth/token_by_sessionid":
            return web.json_response({"access_token": "test-x-token"})
        if path.startswith("/1/bundle/account/short_info"):
            token = (request.headers.get("Authorization") or "").split(" ", 1)[-1]
            uid = {"test-x-token": 1130000012345678,
                   "test-x-token-1": 1130000012345001,
                   "test-x-token-2": 1130000012345002}.get(token, 1130000012345678)
            return web.json_response({"status": "ok", "display_login": "tester", "uid": uid})
        if path == "/1/token":
            form = dict(await request.post())
            self.exchanges.append(form)
            x = form.get("access_token") or ""
            if x == "dead-token":
                return web.json_response({"error": "expired token"}, status=401)
            return web.json_response({"access_token": f"music-token-{len(self.exchanges)}-of-{x}"})
        if path == "/1/bundle/auth/x_token/":
            return web.json_response({"status": "ok", "passport_host": self.base, "track_id": "track-3"})
        if path == "/auth/session/":
            raise web.HTTPFound(f"{self.base}/auth/finish")
        if path == "/iot":
            return web.Response(text='{"csrfToken2":"csrf-2"}')
        if path == "/m/v3/user/devices":
            assert request.headers.get("x-csrf-token") == "csrf-2"
            return web.json_response(IOT_DEVICES)
        if path == "/m/v2/user/devices/d1/configuration":
            return web.json_response(IOT_CONFIG)
        return web.json_response({"error": "неизвестный путь"}, status=404)


@pytest.fixture()
async def fake(monkeypatch):
    fy = FakeYandex()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fy.route)
    runner = web.AppRunner(app)
    await runner.setup()
    port = unused_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    fy.base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(account, "PASSPORT", fy.base)
    monkeypatch.setattr(account, "MOBILEPROXY", fy.base + "/1/bundle")
    monkeypatch.setattr(account, "MOBILE_OAUTH", fy.base + "/1/token")
    monkeypatch.setattr(account, "IOT", fy.base)
    monkeypatch.setattr(account, "IOT_PAGE", fy.base + "/iot")
    yield fy
    await runner.cleanup()


def make_runtime(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({"operation": "user", "id": "u1", "name": "Тест"})
    runtime.identities.data["devices"] = [device_row()]
    return runtime


def headers_write():
    return {"X-Emulator": "1", "Content-Type": "application/json"}


async def test_account_status_and_token_login(tmp_path, fake):
    runtime = make_runtime(tmp_path)
    app_runner_url = None  # API поднимается через create_app — как в surface-тесте
    from server_emulator.server import create_app
    from tests.conftest import listen
    app = create_app(runtime=runtime)
    runner, url = await listen(app)
    try:
        async with runtime.lifecycle.client_session().get(
                f"{url}/emulator/api/account?user=u1") as response:
            assert await response.json() == {"login": False, "login_name": None}
        async with runtime.lifecycle.client_session().post(
                f"{url}/emulator/api/account", headers=headers_write(),
                json={"op": "login_token", "token": "test-x-token", "user": "u1"}) as response:
            assert await response.json() == {"login": True, "login_name": "tester"}
        stored = (runtime.directory / "account" / "sessions" / "u1.json").read_text(encoding="utf-8")
        assert json.loads(stored)["x_token"] == "test-x-token"  # секрет в state/, не в ответе
        assert json.loads(stored)["user_id"] == "u1"  # пользователь внутри файла — коллизии имён видны
        async with runtime.lifecycle.client_session().post(
                f"{url}/emulator/api/account", headers=headers_write(),
                json={"op": "logout", "user": "u1"}) as response:
            assert (await response.json())["login"] is False
        assert not (runtime.directory / "account" / "sessions" / "u1.json").exists()
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_account_api_requires_user(tmp_path, fake):
    """Без user или с несуществующим id — 400 с понятным текстом (GET и POST)."""
    runtime = make_runtime(tmp_path)
    from server_emulator.server import create_app
    from tests.conftest import listen
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with runtime.lifecycle.client_session().get(
                f"{url}/emulator/api/account") as response:
            assert response.status == 400
            assert "user" in (await response.json())["error"]
        async with runtime.lifecycle.client_session().get(
                f"{url}/emulator/api/account?user=ghost") as response:
            assert response.status == 400
            assert "ghost" in (await response.json())["error"]
        async with runtime.lifecycle.client_session().post(
                f"{url}/emulator/api/account", headers=headers_write(),
                json={"op": "logout"}) as response:
            assert response.status == 400
            assert "user" in (await response.json())["error"]
    finally:
        await runner.cleanup()  # закрывает и runtime (create_app.on_cleanup)


async def test_two_users_separate_sessions(tmp_path, fake):
    """Два пользователя — два входа одновременно: разные x_token в разных файлах
    sessions/, сессии не путаются; забытый вход первого не трогает второй."""
    runtime = make_runtime(tmp_path)
    runtime.identities.mutate({"operation": "user", "id": "u2", "name": "Тест 2"})
    try:
        first = account.AccountSession(runtime, "u1")
        second = account.AccountSession(runtime, "u2")
        await first.login_token("test-x-token-1")
        await second.login_token("test-x-token-2")
        stored1 = json.loads((runtime.directory / "account" / "sessions" / "u1.json").read_text("utf-8"))
        stored2 = json.loads((runtime.directory / "account" / "sessions" / "u2.json").read_text("utf-8"))
        assert stored1["x_token"] == "test-x-token-1" and stored2["x_token"] == "test-x-token-2"
        linked = account.linked_sessions(runtime)
        assert [s.user_id for s in linked] == ["u1", "u2"]  # обе сессии, в порядке реестра
        first.forget()
        assert first.status()["login"] is False and second.status()["login"] is True
        assert [s.user_id for s in account.linked_sessions(runtime)] == ["u2"]
    finally:
        await runtime.close()


async def test_login_cookies_raw_and_qr(tmp_path, fake):
    runtime = make_runtime(tmp_path)
    session = account.AccountSession(runtime, "u1")
    try:
        assert await session.login_cookies("yandexuid=testuid; Session_id=test") == \
            {"login": True, "login_name": "tester"}
        assert fake.seen[-1][0] == "/1/bundle/account/short_info/"
        session.forget()
        link = await session.qr_start()
        assert link["link"] == "https://passport.example/qr"
        assert any(path == "/pwl-yandex/api/passport/auth/magic/code" for path, _ in fake.seen)
        # get_session настоящего потока кладёт cookies паспорта (Domain=.yandex.ru) в jar;
        # фейк с другим доменом — подкладываем тот же cookie с доменом паспорта:
        passport_cookies = http.cookies.SimpleCookie()
        passport_cookies["yandexuid"] = "testuid"
        passport_cookies["yandexuid"]["domain"] = ".yandex.ru"
        passport_cookies["Session_id"] = "test"
        passport_cookies["Session_id"]["domain"] = ".yandex.ru"
        session._session().cookie_jar.update_cookies(
            passport_cookies, response_url=yarl.URL("https://passport.yandex.ru/"))
        result = await session.qr_poll()
        assert result["state"] == "done" and result["login"] is True  # cookies сессии → токен
    finally:
        await runtime.close()


async def test_required_unfilled_list(tmp_path):
    runtime = make_runtime(tmp_path)
    try:
        required = account.required_unfilled(runtime)
        assert [item["key"] for item in required] == list(account.REQUIRED_KEYS)
        assert all(item["missing"] == ["user:u1"] for item in required)
        runtime.quasar_params.set_value("user:u1", "user.wifi_hash", "test-wifi-hash")
        required = account.required_unfilled(runtime)
        assert "user.wifi_hash" not in [item["key"] for item in required]
    finally:
        await runtime.close()


async def test_import_from_get_sync_info_journal(tmp_path, fake):
    runtime = make_runtime(tmp_path)
    try:
        journal = runtime.upstream.journal
        journal.write(kind="http", host="quasar.yandex.net",
                      path=f"/get_sync_info?device_id={SN}&platform=chiron",
                      source="upstream", response_body=json.dumps(SYNC_ANSWER).encode())
        await journal.flush()
        from server_emulator.server import create_app
        from tests.conftest import listen
        runner, url = await listen(create_app(runtime=runtime))
        try:
            async with runtime.lifecycle.client_session().post(
                    f"{url}/emulator/api/params_import", headers=headers_write(),
                    json={"source": "get_sync_info"}) as response:
                report = await response.json()
            assert report["answers"] == 1
            filled = {item["key"]: item for item in report["items"]}
            assert set(filled) == {"user.wifi_hash", "user.location.latitude",
                                   "user.location.longitude", "device.name",
                                   "device.glagol_cert", "device.glagol_key"}
            assert filled["user.wifi_hash"]["owner"] == "user:u1"
            assert filled["device.glagol_key"]["secret"] is True
            blob = json.dumps(report)  # секреты и wifi_hash в отчёте не путешествуют
            for secret in ("test-wifi-hash", "test-cert", "test-key", "test-x-token"):
                assert secret not in blob
            params = runtime.quasar_params
            assert params.resolve("user.wifi_hash", user_id="u1") == "test-wifi-hash"
            assert params.resolve("user.location.latitude", user_id="u1") == 55.7558
            assert params.resolve("device.name", sn=SN) == "Дуо тест"
            assert params.resolve("device.glagol_key", sn=SN) == "test-key"
            assert params.def_get("device.glagol_key")["type"] == "secret"
        finally:
            await runner.cleanup()  # закрывает и runtime (create_app.on_cleanup)
    finally:
        await runtime.close()


async def test_import_iot_after_login(tmp_path, fake):
    runtime = make_runtime(tmp_path)
    try:
        session = account.AccountSession(runtime, "u1")
        await session.login_token("test-x-token")
        from server_emulator.server import create_app
        from tests.conftest import listen
        runner, url = await listen(create_app(runtime=runtime))
        try:
            async with runtime.lifecycle.client_session().post(
                    f"{url}/emulator/api/params_import", headers=headers_write(),
                    json={"source": "iot"}) as response:
                report = await response.json()
            assert report["devices"] == 1
            filled = {item["key"]: item for item in report["items"]}
            assert filled["device.room"]["owner"] == f"device:{SN}"
            assert filled["device.room"]["source"] == "iot"
            assert filled["user.location.latitude"]["owner"] == "user:u1"
            params = runtime.quasar_params
            assert params.resolve("device.room", sn=SN) == "Гостиная"
            assert params.resolve("user.location.longitude", user_id="u1") == 37.6173
        finally:
            await runner.cleanup()  # закрывает и runtime (create_app.on_cleanup)
    finally:
        await runtime.close()


async def test_import_iot_without_login_is_400(tmp_path, fake):
    runtime = make_runtime(tmp_path)
    try:
        from server_emulator.server import create_app
        from tests.conftest import listen
        runner, url = await listen(create_app(runtime=runtime))
        try:
            async with runtime.lifecycle.client_session().post(
                    f"{url}/emulator/api/params_import", headers=headers_write(),
                    json={"source": "iot"}) as response:
                assert response.status == 400
                assert "Вход в аккаунт не выполнен" in (await response.json())["error"]
        finally:
            await runner.cleanup()
    finally:
        await runtime.close()


# --- токены сервисов из привязанного аккаунта -----------------------------------

async def _logged_session(runtime, user_id="u1", x_token="test-x-token"):
    """Сессия с выполненным входом (short_info по test-* токенам известен фикстуре)."""
    session = account.AccountSession(runtime, user_id)
    await session.login_token(x_token)
    return session


async def test_service_token_exchange_and_cache(tmp_path, fake):
    runtime = make_runtime(tmp_path)
    try:
        session = await _logged_session(runtime)
        token = await session.service_token("music")
        assert token == f"music-token-1-of-test-x-token"
        assert len(fake.exchanges) == 1
        form = fake.exchanges[0]
        assert form["grant_type"] == "x-token" and form["access_token"] == "test-x-token"
        assert form["client_id"] == account.SERVICE_CLIENTS["music"][0]
        assert form["client_secret"] == account.SERVICE_CLIENTS["music"][1]
        # повторный вызов — из кеша в файле сессии, обмена нет
        assert await session.service_token("music") == token
        assert len(fake.exchanges) == 1
        stored = json.loads(session.path.read_text(encoding="utf-8"))
        assert stored["service_tokens"]["music"]["token"] == token
        # сервис ответил 401 — вызывающий просит refresh=True: обмен заново и кеш свежий
        assert await session.service_token("music", refresh=True) == "music-token-2-of-test-x-token"
        assert len(fake.exchanges) == 2
        stored = json.loads(session.path.read_text(encoding="utf-8"))
        assert stored["service_tokens"]["music"]["token"] == "music-token-2-of-test-x-token"
    finally:
        await runtime.close()


async def test_service_token_without_login_and_unknown_service(tmp_path, fake):
    runtime = make_runtime(tmp_path)
    try:
        session = account.AccountSession(runtime, "u1")
        with pytest.raises(account.AccountError):
            await session.service_token("music")  # вход не выполнен
        assert fake.exchanges == []
        logged = await _logged_session(runtime)
        with pytest.raises(account.AccountError):
            await logged.service_token("video")  # сервис не заведён
        assert fake.exchanges == []
    finally:
        await runtime.close()


async def test_service_token_exchange_failure_keeps_cache(tmp_path, fake):
    runtime = make_runtime(tmp_path)
    try:
        session = await _logged_session(runtime)
        token = await session.service_token("music")
        # сессия с мёртвым x_token не обменяется: честная ошибка, кеш не тронут
        # (другой файл сессии — свой у каждого пользователя)
        dead = account.AccountSession(runtime, "u2")
        dead.path.write_text(json.dumps({"user_id": "u2", "x_token": "dead-token",
                                         "display_login": "tester"}), encoding="utf-8")
        with pytest.raises(account.AccountError):
            await dead.service_token("music")
        assert len(fake.exchanges) == 2  # первая — удачный, вторая — 401 от Яндекса
        assert json.loads(session.path.read_text(encoding="utf-8"))["service_tokens"]["music"]["token"] == token
    finally:
        await runtime.close()


async def test_service_token_forget_drops_cache(tmp_path, fake):
    runtime = make_runtime(tmp_path)
    try:
        session = await _logged_session(runtime)
        await session.service_token("music")
        session.forget()
        assert not session.path.exists()
        with pytest.raises(account.AccountError):
            await session.service_token("music")
        assert len(fake.exchanges) == 1  # после forget — только первый обмен
    finally:
        await runtime.close()


async def test_service_token_per_user(tmp_path, fake):
    runtime = make_runtime(tmp_path)
    try:
        runtime.identities.mutate({"operation": "user", "id": "u2", "name": "Вторая"})
        first = await _logged_session(runtime, "u1", "test-x-token-1")
        second = await _logged_session(runtime, "u2", "test-x-token-2")
        token1 = await first.service_token("music")
        token2 = await second.service_token("music")
        assert token1.endswith("of-test-x-token-1") and token2.endswith("of-test-x-token-2")
        assert token1 != token2
        # кеш у каждого пользователя свой
        assert await first.service_token("music") == token1
        assert await second.service_token("music") == token2
        assert len(fake.exchanges) == 2
    finally:
        await runtime.close()
