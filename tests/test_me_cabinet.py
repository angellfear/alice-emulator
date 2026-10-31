"""TASK-062: кабинет /me — API /api/me/*, страница, ссылка-токен для админа (X-Me-Token)."""
import pytest
from aiohttp import ClientSession, CookieJar

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import run_app

WRITE = {"X-Emulator": "1"}


def seed(runtime):
    """Два пользователя, у каждого по колонке; параметры user.* / device.* — из сида каталога."""
    registry = runtime.identities
    registry.mutate({"operation": "user", "id": "u1", "name": "Алиса"})
    registry.mutate({"operation": "user", "id": "u2", "name": "Борис"})
    registry.mutate({"operation": "device", "sn": "SN-A", "name": "Кухня", "user_id": "u1", "device_ids": ["ID-A"]})
    registry.mutate({"operation": "device", "sn": "SN-B", "name": "Гостиная", "user_id": "u2", "device_ids": ["ID-B"]})
    runtime.quasar_params.seed(registry.data)


@pytest.fixture
async def open_panel(tmp_path):
    runtime = Runtime(tmp_path)
    seed(runtime)
    runner, port = await run_app(create_app(runtime=runtime))
    yield runtime, f"http://127.0.0.1:{port}"
    await runner.cleanup()
    await runtime.close()


@pytest.fixture
async def closed_panel(tmp_path):
    """Вход включён: админ Admin и пользователь bob → u1."""
    runtime = Runtime(tmp_path)
    seed(runtime)
    runtime.auth.bootstrap("Admin", password="pw-123456")
    runtime.auth.set_user_login("u1", "bob", "bob-pass1")
    runner, port = await run_app(create_app(runtime=runtime))
    yield runtime, f"http://127.0.0.1:{port}"
    await runner.cleanup()
    await runtime.close()


async def login(client, base, name, password):
    async with client.post(base + "/api/auth/login", json={"name": name, "password": password}) as response:
        assert response.status == 200


async def test_open_mode_home_of_selected_user(open_panel):
    _runtime, base = open_panel
    async with ClientSession() as client:
        async with client.get(base + "/api/me/params?user=u2") as response:
            assert response.status == 200
            body = await response.json()
        assert body["user"] == "u2" and body["user_name"] == "Борис"
        assert [d["sn"] for d in body["devices"]] == ["SN-B"]
        assert {u["id"] for u in body["users"]} == {"u1", "u2"}
        device = body["devices"][0]
        assert device["status"] == "offline" and device["name"] == "Гостиная"
        assert {p["key"] for p in device["params"]} >= {"device.name", "device.room"}
        assert any(p["key"] == "user.name" for p in body["params"])
        async with client.get(base + "/api/me/params?user=ghost") as response:  # нет такого — первый
            assert response.status == 200
            assert (await response.json())["user"] in ("u1", "u2")


async def test_login_user_sees_only_own_home(closed_panel):
    _runtime, base = closed_panel
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
        async with client.get(base + "/api/me/params") as response:
            assert response.status == 401  # без сессии
        await login(client, base, "bob", "bob-pass1")
        async with client.get(base + "/api/me/params?user=u2") as response:  # чужой дом игнорируется
            body = await response.json()
        assert body["user"] == "u1" and "users" not in body
        assert [d["sn"] for d in body["devices"]] == ["SN-A"]
        async with client.get(base + "/emulator/api/settings") as response:
            assert response.status == 403  # admin-маршруты закрыты


async def test_user_without_link_gets_409(tmp_path):
    runtime = Runtime(tmp_path)
    seed(runtime)
    runtime.auth.bootstrap("Admin", password="pw-123456")
    runtime.auth.add_principal("lonely", "lonely-pw1", role="user", identities_user=None)
    runner, port = await run_app(create_app(runtime=runtime))
    try:
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            base = f"http://127.0.0.1:{port}"
            await login(client, base, "lonely", "lonely-pw1")
            async with client.get(base + "/api/me/params") as response:
                assert response.status == 409
                assert (await response.json())["error"] == "principal_not_linked"
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_put_params_ownership_and_editable(closed_panel):
    runtime, base = closed_panel
    runtime.quasar_params.def_upsert({"key": "device.service_note", "scope": "device", "category": "service",
                                      "type": "string", "title": "Служебный", "user_editable": False})
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
        await login(client, base, "bob", "bob-pass1")

        async def put(body):
            async with client.put(base + "/api/me/params", json=body, headers=WRITE) as response:
                return response.status, await response.json()

        status, body = await put({"key": "device.timezone", "value": "Europe/Moscow", "owner": "device:SN-B"})
        assert status == 403 and body["error"] == "not_your_device"
        status, body = await put({"key": "device.service_note", "value": "x", "owner": "device:SN-A"})
        assert status == 403 and body["error"] == "not_editable"
        status, body = await put({"key": "user.timezone", "value": "Europe/Moscow", "owner": "user:u2"})
        assert status == 403 and body["error"] == "bad_owner"
        status, body = await put({"key": "user.timezone", "value": 5})
        assert status == 400  # ошибка значения
        status, body = await put({"key": "device.timezone", "value": "Asia/Tokyo", "owner": "device:SN-A"})
        assert status == 200
        status, body = await put({"key": "user.timezone", "value": "Europe/Moscow"})  # owner по умолчанию — свой
        assert status == 200
        assert runtime.quasar_params.raw_value("device:SN-A", "device.timezone") == "Asia/Tokyo"
        assert runtime.quasar_params.raw_value("user:u1", "user.timezone") == "Europe/Moscow"
        async with client.put(base + "/api/me/params", json={"key": "user.timezone", "value": "x"}) as response:
            assert response.status == 403  # без маркера записи


async def test_put_devices_renames_own_only(closed_panel):
    runtime, base = closed_panel
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
        await login(client, base, "bob", "bob-pass1")
        async with client.put(base + "/api/me/devices", headers=WRITE,
                              json={"sn": "SN-B", "name": "Чужая"}) as response:
            assert response.status == 403
        async with client.put(base + "/api/me/devices", headers=WRITE,
                              json={"sn": "SN-A", "name": "Спальня", "room": "Комната"}) as response:
            assert response.status == 200
            assert await response.json() == {"sn": "SN-A", "name": "Спальня", "room": "Комната"}
    device = next(d for d in runtime.identities.data["devices"] if d["sn"] == "SN-A")
    assert device["name"] == "Спальня" and device["room"] == "Комната" and device["device_ids"] == ["ID-A"]
    assert runtime.quasar_params.raw_value("device:SN-A", "device.name") == "Спальня"
    assert runtime.quasar_params.raw_value("device:SN-A", "device.room") == "Комната"


async def test_me_link_token_scope_and_revocation(closed_panel):
    runtime, base = closed_panel
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as user_client:
        await login(user_client, base, "bob", "bob-pass1")
        async with user_client.post(base + "/emulator/api/me_link", json={"user": "u2"}, headers=WRITE) as response:
            assert response.status == 403  # не админ
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as admin:
        await login(admin, base, "Admin", "pw-123456")
        async with admin.post(base + "/emulator/api/me_link", json={"user": "ghost"}, headers=WRITE) as response:
            assert response.status == 400
        async with admin.post(base + "/emulator/api/me_link", json={"user": "u2"}, headers=WRITE) as response:
            assert response.status == 200
            url = (await response.json())["url"]
        assert url.startswith("/me#t=")
        token = url.split("#t=", 1)[1]
        header = {"X-Me-Token": token}
        async with ClientSession() as anonymous:  # без cookie — только токен
            async with anonymous.get(base + "/api/me/params", headers=header) as response:
                assert response.status == 200
                body = await response.json()
            assert body["user"] == "u2" and [d["sn"] for d in body["devices"]] == ["SN-B"]
            async with anonymous.get(base + "/api/me/params?user=u1", headers=header) as response:
                assert (await response.json())["user"] == "u2"  # токен привязан к пользователю
            async with anonymous.get(base + "/api/auth/whoami", headers=header) as response:
                assert await response.json() == {"authenticated": True, "name": "Борис", "role": "user", "via": "link"}
            async with anonymous.get(base + "/emulator/api/settings", headers=header) as response:
                assert response.status == 401  # на другие пути токен не действует
            async with anonymous.put(base + "/api/me/params", headers={**header, **WRITE},
                                     json={"key": "user.timezone", "value": "Asia/Tokyo"}) as response:
                assert response.status == 200
            async with anonymous.get(base + "/api/me/params", headers={"X-Me-Token": "wrong"}) as response:
                assert response.status == 401
        # cookie админа токеном не перезаписывается: панель остаётся залогиненной
        async with admin.get(base + "/api/me/params?user=u1", headers=header) as response:
            assert (await response.json())["user"] == "u2"
        async with admin.get(base + "/emulator/api/settings") as response:
            assert response.status == 200
        # revoke_all гасит токен
        runtime.auth.revoke_all()
        async with ClientSession() as anonymous:
            async with anonymous.get(base + "/api/me/params", headers=header) as response:
                assert response.status == 401


async def test_admin_logout_revokes_links(closed_panel):
    runtime, base = closed_panel
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as admin:
        await login(admin, base, "Admin", "pw-123456")
        async with admin.post(base + "/emulator/api/me_link", json={"user": "u1"}, headers=WRITE) as response:
            token = (await response.json())["url"].split("#t=", 1)[1]
        assert runtime.auth.link_session(token) is not None
        async with admin.post(base + "/api/auth/logout") as response:
            assert response.status == 200
        assert runtime.auth.link_session(token) is None


async def test_me_link_open_mode_and_page(open_panel):
    _runtime, base = open_panel
    async with ClientSession() as client:
        async with client.post(base + "/emulator/api/me_link", json={"user": "u2"}, headers=WRITE) as response:
            assert (await response.json()) == {"url": "/me?user=u2"}
        async with client.get(base + "/me") as response:
            assert response.status == 200
            assert response.content_type == "text/html"
            assert "X-Me-Token" in await response.text()


async def test_me_page_is_public_when_login_enabled(closed_panel):
    _runtime, base = closed_panel
    async with ClientSession() as client:
        async with client.get(base + "/me") as response:
            assert response.status == 200  # страница отдаётся, API защищён входом
