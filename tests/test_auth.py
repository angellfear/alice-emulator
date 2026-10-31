"""B04: принципалы/сессии панели. Открытый режим (нет auth.json) — всё как
прежде; после локального bootstrap панель и API требуют входа; user не видит
admin-маршруты; устройства входа не требуют; секреты — env-ссылки/маски."""
import asyncio

import pytest
from aiohttp import ClientSession, CookieJar

from server_emulator.auth import COOKIE, AuthStore, secret
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import run_app


def test_auth_store_bootstrap_verify_sessions(tmp_path):
    store = AuthStore(tmp_path)
    assert not store.enabled  # файла нет — открытый режим
    principal, password = store.bootstrap("Admin", password="pw-123456")
    assert store.enabled and principal["role"] == "admin"
    assert "hash" not in store.public()["principals"][0]  # хэши наружу не отдаются
    assert store.login("Admin", "pw-123456") is not None
    assert store.login("Admin", "wrong") is None
    assert store.login("ghost", "pw-123456") is None
    with pytest.raises(ValueError):
        store.bootstrap("Other", password="x")  # повторный bootstrap запрещён
    _new, new_password = store.bootstrap("Admin2", reset=True)  # принудительная замена
    assert store.login("Admin2", new_password) is not None
    assert store.login("Admin", "pw-123456") is None
    # сессии: отзыв и TTL
    token, session = store.login("Admin2", new_password)
    assert store.session(token)["role"] == "admin"
    store.logout(token)
    assert store.session(token) is None
    token, session = store.login("Admin2", new_password)
    session["expires"] = 0
    assert store.session(token) is None  # истекла
    # user-принципал
    store.add_principal("bob", "bob-pw", role="user", identities_user="u1")
    with pytest.raises(ValueError):
        store.add_principal("bob", "x")  # имя занято
    _token, user_session = store.login("bob", "bob-pw")
    assert user_session["role"] == "user"
    # персистентность: рестарт читает тот же файл
    restarted = AuthStore(tmp_path)
    assert restarted.enabled and restarted.login("bob", "bob-pw") is not None


def test_secret_env_references(monkeypatch):
    monkeypatch.setenv("ALICE_TEST_KEY", "env-value")
    assert secret("env:ALICE_TEST_KEY") == "env-value"
    assert secret("env:MISSING_VAR") == ""
    assert secret("plain") == "plain"


def test_public_masks_raw_keys_keeps_env_refs(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings["model"]["api_key"] = "raw-secret"
    runtime.settings["stt"]["api_key"] = "env:STT_KEY"
    public = runtime.public()
    assert public["model"]["api_key"] == "__KEEP__"  # секрет замаскирован
    assert public["stt"]["api_key"] == "env:STT_KEY"  # ссылка — не секрет
    assert runtime.settings["model"]["api_key"] == "raw-secret"  # оригинал не тронут
    asyncio.run(runtime.close())


def test_pairing_pending_to_assigned_with_confirm(tmp_path):
    runtime = Runtime(tmp_path)
    registry = runtime.identities
    # pairing работает из очереди unassigned; наблюдения её больше не пополняют
    # (get_sync создаёт устройства сразу) — сеем запись напрямую
    value = registry.public()
    value['unassigned'].append({'device_id': 'pending-id', 'source': 'header', 'ip': '192.0.2.19'})
    registry.commit(value)
    registry.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    with pytest.raises(ValueError):
        registry.mutate({'operation': 'pair', 'device_id': 'pending-id',
                         'sn': 'SN-P', 'user_id': 'u1'})  # без confirm
    with pytest.raises(ValueError):
        registry.mutate({'operation': 'pair', 'device_id': 'unknown-id', 'confirm': True,
                         'sn': 'SN-P', 'user_id': 'u1'})  # не в очереди
    registry.mutate({'operation': 'pair', 'device_id': 'pending-id', 'confirm': True,
                     'sn': 'SN-P', 'user_id': 'u1', 'name': 'Колонка'})
    device = registry.resolve('pending-id')
    assert device and device['sn'] == 'SN-P' and device['user_id'] == 'u1'
    assert all(d['device_id'] != 'pending-id' for d in registry.public()['unassigned'])
    with pytest.raises(ValueError):
        registry.mutate({'operation': 'pair', 'device_id': 'pending-id', 'confirm': True,
                         'sn': 'SN-P', 'user_id': 'u1'})  # второй раз — нечего привязывать
    asyncio.run(runtime.close())


async def test_standalone_panel_guarded_device_traffic_free(tmp_path):
    runtime = Runtime(tmp_path)
    _principal, password = runtime.auth.bootstrap("Admin", password="pw-123456")
    runner, port = await run_app(create_app(runtime=runtime))
    try:
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            base = f"http://127.0.0.1:{port}"
            async with client.get(base + "/emulator/api/settings") as response:
                assert response.status == 401  # панель standalone требует входа
            async with client.post(base + "/api/auth/login",
                                   json={"name": "Admin", "password": password}) as response:
                assert response.status == 200
            async with client.get(base + "/emulator/api/settings") as response:
                assert response.status == 200
            # трафик устройств не охраняется: builtin-маршрут отвечает без сессии
            async with client.get(base + "/check_updates",
                                  headers={"Host": "quasar.yandex.net"}) as response:
                assert response.status == 200
                assert (await response.json()) == {"hasUpdate": False}
    finally:
        await runner.cleanup()
    await runtime.close()


def test_set_user_login_creates_updates_and_links_session(tmp_path):
    store = AuthStore(tmp_path)
    store.bootstrap("Admin", password="pw-123456")
    assert store.user_login("u1") is None
    with pytest.raises(ValueError):
        store.set_user_login("u1", "bob")  # новому входу нужен пароль
    with pytest.raises(ValueError):
        store.set_user_login("u1", "bob", "short")
    store.set_user_login("u1", "bob", "bob-pass1")
    token, session = store.login("bob", "bob-pass1")
    assert session["role"] == "user" and session["identities_user"] == "u1"
    with pytest.raises(ValueError):
        store.set_user_login("u1", "Admin")  # имя занято
    store.set_user_login("u1", "bobby", "new-pass2")
    assert store.session(token) is None  # смена входа отзывает сессии
    assert store.login("bob", "bob-pass1") is None
    assert store.login("bobby", "new-pass2") is not None
    store.set_user_login("u1", "bobby")  # без пароля — пароль прежний
    assert store.login("bobby", "new-pass2") is not None
    assert store.user_login("u1")["name"] == "bobby"
    assert AuthStore(tmp_path).login("bobby", "new-pass2") is not None
