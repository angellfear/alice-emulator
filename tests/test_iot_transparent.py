"""Перехват iot.quasar.yandex.ru в маршруте «Прозрачно»: домен в сертификате фронта,
пересылка на настоящий iot и запись в журнал по опции «Запись» устройства
(по образцу quasar.yandex.net — tests/test_device_route_modes.py).

Только локальные серверы: цель подменяется алиасом со схемой.
"""
from aiohttp import ClientSession, web

from server_emulator import server_config
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen


def test_iot_host_in_certificate():
    """Домен в списке сертификата — фронт расшифровывает TLS и видит запросы iot."""
    assert "iot.quasar.yandex.ru" in server_config.CERT_DOMAINS


async def start_upstream(calls):
    async def http(request):
        calls.append(request.rel_url.path_qs)
        return web.json_response({"from": "upstream", "path": request.rel_url.path_qs})

    app = web.Application()
    app.router.add_route("*", "/{p:.*}", http)
    return await listen(app)


async def test_iot_transparent_forwarded_and_journaled(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-IOT", "user_id": "u",
                               "device_ids": ["iot-id"]})
    runtime.identities.mutate({"operation": "route_mode", "sn": "SN-IOT", "mode": "proxy"})
    runtime.identities.mutate({"operation": "record", "sn": "SN-IOT", "mode": "on"})

    calls = []
    upstream_runner, upstream_url = await start_upstream(calls)
    runtime.settings["aliases"] = {"iot.quasar.yandex.ru": upstream_url}
    runner, url = await listen(create_app(runtime=runtime))
    headers = {"Host": "iot.quasar.yandex.ru", "X-YaQuasarDeviceId": "iot-id"}
    try:
        async with ClientSession() as client:
            async with client.get(url + "/iot/devices", headers=headers) as response:
                assert response.status == 200
                assert (await response.json())["from"] == "upstream"
        assert calls == ["/iot/devices"]
        await runtime.upstream.journal.flush()
        items = (await runtime.upstream.journal.list(device="SN-IOT"))["items"]
        assert len(items) == 1
        assert items[0]["source"] == "upstream" and items[0]["status"] == 200
        assert items[0]["path"] == "/iot/devices"
        assert items[0]["host"] == "iot.quasar.yandex.ru"
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()
