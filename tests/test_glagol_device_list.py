"""TASK-049: /glagol/device_list — glagol.security у устройств владельца и определение
вызывающего без маркера устройства (IP клиента → единственный владелец → каталог/404)."""
from aiohttp import ClientSession

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import is_rsa_key, listen

# фиктивные значения: настоящие сертификаты в тестах не используются
FAKE_CERT = "FAKE-CERT-{sn}"
FAKE_KEY = "FAKE-KEY-{sn}"
HEADERS = {"Host": "quasar.yandex.net"}


def _define_params(runtime):
    for key, title in (("device.glagol_cert", "cert"), ("device.glagol_key", "key")):
        runtime.quasar_params.def_upsert({"key": key, "scope": "device", "category": "service",
                                          "type": "secret", "title": title, "default_value": ""})


def _set_security(runtime, sn):
    runtime.quasar_params.set_value(f"device:{sn}", "device.glagol_cert", FAKE_CERT.format(sn=sn))
    runtime.quasar_params.set_value(f"device:{sn}", "device.glagol_key", FAKE_KEY.format(sn=sn))


def _registry(runtime, second_owner=False):
    ids = runtime.identities
    ids.mutate({"operation": "user", "id": "alice", "name": "Alice"})
    ids.mutate({"operation": "device", "sn": "SN-A1", "user_id": "alice", "device_ids": ["id-a1"], "name": "Кухня"})
    ids.mutate({"operation": "device", "sn": "SN-A2", "user_id": "alice", "device_ids": ["id-a2"], "name": "Спальня"})
    if second_owner:
        ids.mutate({"operation": "user", "id": "bob", "name": "Bob"})
        ids.mutate({"operation": "device", "sn": "SN-B1", "user_id": "bob", "device_ids": ["id-b1"], "name": "Чужая"})


async def _get(url, path, headers=None):
    async with ClientSession() as client:
        async with client.get(url + path, headers={**HEADERS, **(headers or {})}) as response:
            return response.status, await response.json()


async def test_security_per_device_owner_only(tmp_path):
    """glagol.security у каждого устройства владельца (свои параметры), чужих в списке нет."""
    runtime = Runtime(tmp_path)
    _registry(runtime, second_owner=True)
    _define_params(runtime)
    for sn in ("SN-A1", "SN-A2", "SN-B1"):
        _set_security(runtime, sn)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        status, body = await _get(url, "/glagol/device_list?device_id=id-a1")
        assert status == 200 and body["status"] == "ok"
        by_id = {d["id"]: d for d in body["devices"]}
        assert set(by_id) == {"id-a1", "id-a2"}  # id-b1 — чужой владелец
        assert by_id["id-a1"]["glagol"]["security"] == {"server_certificate": "FAKE-CERT-SN-A1",
                                                        "server_private_key": "FAKE-KEY-SN-A1"}
        assert by_id["id-a2"]["glagol"]["security"]["server_certificate"] == "FAKE-CERT-SN-A2"
        # поля элемента из реального ответа
        assert by_id["id-a1"]["activation_code"] == "" and by_id["id-a1"]["promocode_activated"] is False
        assert "activation_region" in by_id["id-a1"]
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_no_params_autogenerates_security(tmp_path):
    """Параметров нет: пара генерируется и запоминается в state.glagol_security; повторный
    вызов не перегенерирует (стабильный PEM); устройство с параметрами получает их."""
    runtime = Runtime(tmp_path)
    _registry(runtime)
    _define_params(runtime)  # определения есть, значений нет (пустой дефолт)
    _set_security(runtime, "SN-A2")  # параметры пользователя — только у второго
    runner, url = await listen(create_app(runtime=runtime))
    try:
        _, body = await _get(url, "/glagol/device_list?device_id=id-a1")
        by_id = {d["id"]: d for d in body["devices"]}
        generated = by_id["id-a1"]["glagol"]["security"]
        assert generated["server_certificate"].startswith("-----BEGIN CERTIFICATE-----")
        assert is_rsa_key(generated["server_private_key"])
        assert by_id["id-a2"]["glagol"]["security"] == {"server_certificate": "FAKE-CERT-SN-A2",
                                                        "server_private_key": "FAKE-KEY-SN-A2"}
        stored = runtime.identities.resolve("id-a1")["state"]["glagol_security"]
        assert stored["certificate"] == generated["server_certificate"]
        _, body = await _get(url, "/glagol/device_list?device_id=id-a1")
        again = {d["id"]: d for d in body["devices"]}["id-a1"]["glagol"]["security"]
        assert again == generated  # повторный вызов: тот же PEM
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_user_params_override_autogen(tmp_path):
    """Параметры пользователя сильнее автогена: появившись позже, перекрывают пару из state."""
    runtime = Runtime(tmp_path)
    _registry(runtime)
    _define_params(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        _, body = await _get(url, "/glagol/device_list?device_id=id-a1")
        generated = {d["id"]: d for d in body["devices"]}["id-a1"]["glagol"]["security"]
        assert generated["server_certificate"].startswith("-----BEGIN CERTIFICATE-----")
        _set_security(runtime, "SN-A1")
        _, body = await _get(url, "/glagol/device_list?device_id=id-a1")
        security = {d["id"]: d for d in body["devices"]}["id-a1"]["glagol"]["security"]
        assert security == {"server_certificate": "FAKE-CERT-SN-A1",
                            "server_private_key": "FAKE-KEY-SN-A1"}
        # сгенерированная пара осталась в state, но не выдаётся
        assert runtime.identities.resolve("id-a1")["state"]["glagol_security"]["certificate"] \
            == generated["server_certificate"]
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_private_key_masked_in_ui_api(tmp_path):
    """В API реестра приватный ключ glagol.security — маска (как секреты расширений),
    сертификат виден как есть."""
    runtime = Runtime(tmp_path)
    _registry(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        await _get(url, "/glagol/device_list?device_id=id-a1")  # ленивый автоген пары
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/identities") as response:
                devices = (await response.json())["devices"]
        security = devices[0]["state"]["glagol_security"]
        assert security["private_key"] == "__KEEP__"
        assert security["certificate"].startswith("-----BEGIN CERTIFICATE-----")  # в state — настоящий

    finally:
        await runner.cleanup()
        await runtime.close()


def test_registration_generates_security(tmp_path):
    """Регистрация устройства (observe register=True) сразу несёт сгенерированную пару."""
    from server_emulator.identity import IdentityRegistry
    from server_emulator.state_store import StateStore

    registry = IdentityRegistry(tmp_path, StateStore(tmp_path / "emulator.sqlite3"))
    device = registry.observe("id-new", register=True)
    security = device["state"]["glagol_security"]
    assert security["certificate"].startswith("-----BEGIN CERTIFICATE-----")
    assert is_rsa_key(security["private_key"])


async def test_no_marker_resolved_by_client_ip(tmp_path):
    """Запрос без device_id с IP, который реестр знает по активности устройства."""
    runtime = Runtime(tmp_path)
    _registry(runtime, second_owner=True)
    runtime.client_peer = lambda request: ("192.0.2.84", 54321)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        status, _ = await _get(url, "/check_updates?device_id=id-b1")  # активность: id-b1 с этого IP
        assert status == 200
        status, body = await _get(url, "/glagol/device_list")  # без маркеров
        assert status == 200
        assert [d["id"] for d in body["devices"]] == ["id-b1"]
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_no_marker_single_owner_fallback(tmp_path):
    """IP неизвестен, владелец всех известных устройств один — отдаём его список."""
    runtime = Runtime(tmp_path)
    _registry(runtime)
    runtime.client_peer = lambda request: ("192.0.2.99", 1)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        status, body = await _get(url, "/glagol/device_list")
        assert status == 200
        assert {d["id"] for d in body["devices"]} == {"id-a1", "id-a2"}
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_no_marker_several_owners_keeps_catalog_or_404(tmp_path):
    """IP неизвестен, владельцев несколько — builtin уступает (каталог/404)."""
    runtime = Runtime(tmp_path)
    _registry(runtime, second_owner=True)
    runtime.client_peer = lambda request: ("192.0.2.99", 1)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        status, body = await _get(url, "/glagol/device_list")
        assert status == 404 and body == {"error": "emulator_handler_not_found"}
    finally:
        await runner.cleanup()
        await runtime.close()
