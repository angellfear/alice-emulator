"""TASK-049: /glagol/device_list — glagol.security у устройств владельца и определение
вызывающего без маркера устройства (IP клиента → единственный владелец → каталог/404)."""
from aiohttp import ClientSession

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen

# фиктивные значения: настоящие сертификаты в тестах не используются
FAKE_CERT = "FAKE-CERT-{sn}"
FAKE_KEY = "FAKE-KEY-{sn}"
HEADERS = {"Host": "quasar.local"}


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


async def test_no_params_no_glagol_field(tmp_path):
    runtime = Runtime(tmp_path)
    _registry(runtime)
    _define_params(runtime)  # определения есть, значений нет (пустой дефолт)
    _set_security(runtime, "SN-A2")  # только у второго устройства
    runner, url = await listen(create_app(runtime=runtime))
    try:
        _, body = await _get(url, "/glagol/device_list?device_id=id-a1")
        by_id = {d["id"]: d for d in body["devices"]}
        assert "glagol" not in by_id["id-a1"]
        assert "glagol" in by_id["id-a2"]
    finally:
        await runner.cleanup()
        await runtime.close()


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
