"""TASK-052: источник конфига устройства — Генерируемый · Напрямую · Свой.

Только локальные серверы: «Яндекс» — локальная заглушка, цель подменяется алиасом
со схемой (settings.aliases[host] = http://127.0.0.1:порт), как в tests/test_upstream.py.
"""
import json

from aiohttp import ClientSession, web

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen

STUB_CONFIG = {"status": "ok", "config": {"marker": "from-yandex", "device_config": {"name": "stub"}}}


async def start_upstream(calls):
    async def handler(request):
        calls.append((request.rel_url.path_qs, dict(request.headers)))
        return web.json_response(STUB_CONFIG)

    app = web.Application()
    app.router.add_route("*", "/{p:.*}", handler)
    runner, url = await listen(app)
    return runner, int(url.rsplit(":", 1)[1])


def alias_upstream(runtime, port):
    runtime.settings["aliases"] = {"quasar.yandex.net": f"http://127.0.0.1:{port}",
                                   "quasar.local": f"http://127.0.0.1:{port}"}


def registry(runtime, sn="SN-Q", device_id="q-id"):
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    runtime.identities.mutate({"operation": "device", "sn": sn, "user_id": "u", "device_ids": [device_id]})
    runtime.quasar_platforms.ensure_common()  # платформенные файлы: без них get_sync_info отвечает 409


HEADERS = {"Host": "quasar.yandex.net", "X-YaQuasarDeviceId": "q-id", "Authorization": "Bearer secret-token"}


def state_of(runtime, sn="SN-Q"):
    return next(d for d in runtime.identities.data["devices"] if d["sn"] == sn)["state"]


async def test_generated_default_matches_compose(tmp_path):
    """Без поля и с generated ответ — прежний compose (config совпадает с quasar_composed)."""
    runtime = Runtime(tmp_path)
    registry(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            bodies = []
            for _mode in (None, "generated"):
                if _mode:
                    async with client.put(url + "/emulator/api/quasar_source",
                                          json={"sn": "SN-Q", "mode": _mode}, headers={"X-Emulator": "1"}) as r:
                        assert r.status == 200
                async with client.get(url + "/get_sync_info?device_id=q-id", headers=HEADERS) as response:
                    assert response.status == 200
                    bodies.append(await response.json())
            assert bodies[0] == bodies[1] and bodies[0]["status"] == "ok"
            async with client.get(url + "/emulator/api/quasar_composed?device=SN-Q") as response:
                composed = (await response.json())["config"]
            assert bodies[0]["config"] == composed["config"]  # у composed 'config' — тело compose
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_direct_forwards_and_saves_last(tmp_path):
    """direct: ответ заглушки доходит до колонки без изменений, quasar_direct_last
    сохранён, запись журнала с source=upstream — по опции «Запись» устройства (TASK-054)."""
    runtime = Runtime(tmp_path)
    registry(runtime)
    runtime.identities.mutate({"operation": "record", "sn": "SN-Q", "mode": "on"})
    state = dict(state_of(runtime))
    state["quasar_source"] = "direct"
    runtime.identities.mutate({"operation": "state", "sn": "SN-Q", "state": state})
    calls = []
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/get_sync_info?device_id=q-id", headers=HEADERS) as response:
                assert response.status == 200
                assert await response.json() == STUB_CONFIG  # ответ Яндекса без изменений
        assert calls and calls[0][0] == "/get_sync_info?device_id=q-id"
        last = state_of(runtime).get("quasar_direct_last")
        assert last and last["status"] == 200 and last["body"] == STUB_CONFIG
        await runtime.upstream.journal.flush()
        items = runtime.upstream.journal.list()["items"]
        assert items and items[0]["source"] == "upstream" and items[0]["path"] == "/get_sync_info?device_id=q-id"
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()


async def test_direct_upstream_down_keeps_last(tmp_path):
    """direct при недоступном upstream — 502, прежний quasar_direct_last цел."""
    runtime = Runtime(tmp_path)
    registry(runtime)
    saved_last = {"ts": 1.0, "status": 200, "body": {"config": {"old": True}}}
    state = dict(state_of(runtime))
    state.update(quasar_source="direct", quasar_direct_last=saved_last)
    runtime.identities.mutate({"operation": "state", "sn": "SN-Q", "state": state})
    runtime.settings["aliases"] = {"quasar.yandex.net": "http://127.0.0.1:1"}  # порт закрыт
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/get_sync_info?device_id=q-id", headers=HEADERS) as response:
                assert response.status == 502
                assert (await response.json())["error"] == "upstream_unavailable"
        assert state_of(runtime)["quasar_direct_last"] == saved_last  # не затёрт
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_custom_served_as_is(tmp_path):
    """custom: сохранённый ответ уходит как есть; без сохранённого — 409."""
    runtime = Runtime(tmp_path)
    registry(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    custom = {"status": "ok", "config": {"custom": True}}
    try:
        async with ClientSession() as client:
            async with client.get(url + "/get_sync_info?device_id=q-id", headers=HEADERS) as response:
                assert response.status == 200  # без поля — generated, работает как раньше
            async with client.put(url + "/emulator/api/quasar_source",
                                  json={"sn": "SN-Q", "mode": "custom"}, headers={"X-Emulator": "1"}) as r:
                assert r.status == 200
            async with client.get(url + "/get_sync_info?device_id=q-id", headers=HEADERS) as response:
                assert response.status == 409
                assert (await response.json())["error"] == "quasar_custom_not_set"
            async with client.put(url + "/emulator/api/quasar_source",
                                  json={"sn": "SN-Q", "custom": custom}, headers={"X-Emulator": "1"}) as r:
                assert r.status == 200
            async with client.get(url + "/get_sync_info?device_id=q-id", headers=HEADERS) as response:
                assert response.status == 200
                assert await response.json() == custom  # как есть, без слоёв и group
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_put_bad_custom_keeps_state(tmp_path):
    """PUT с битым custom — 400, state не изменён."""
    runtime = Runtime(tmp_path)
    registry(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.put(url + "/emulator/api/quasar_source",
                                  json={"sn": "SN-Q", "custom": {"no": "config"}}, headers={"X-Emulator": "1"}) as r:
                assert r.status == 400
            assert state_of(runtime).get("quasar_custom") is None
            async with client.put(url + "/emulator/api/quasar_source",
                                  json={"sn": "SN-Q", "mode": "weird"}, headers={"X-Emulator": "1"}) as r:
                assert r.status == 400
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_generate_matches_composed_and_fetch_uses_headers(tmp_path):
    """generate совпадает с quasar_composed; fetch ходит с заголовками последнего
    опроса; без опроса — 409; Authorization остаётся только в памяти."""
    runtime = Runtime(tmp_path)
    registry(runtime)
    calls = []
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/emulator/api/quasar_source",
                                   json={"sn": "SN-Q", "action": "fetch"}, headers={"X-Emulator": "1"}) as r:
                assert r.status == 409  # опроса ещё не было
            async with client.get(url + "/get_sync_info?device_id=q-id", headers=HEADERS) as response:
                assert response.status == 200
            async with client.get(url + "/get_sync_info?device_id=q-id", headers=HEADERS) as response:
                sync_response = await response.json()
            async with client.post(url + "/emulator/api/quasar_source",
                                   json={"sn": "SN-Q", "action": "generate"}, headers={"X-Emulator": "1"}) as r:
                assert r.status == 200
                generated = await r.json()
            assert generated == sync_response  # generate — ровно то, что собрал бы compose
            async with client.get(url + "/emulator/api/quasar_composed?device=SN-Q") as r:
                composed = (await r.json())["config"]
            assert generated["config"] == composed["config"]
            async with client.post(url + "/emulator/api/quasar_source",
                                   json={"sn": "SN-Q", "action": "fetch"}, headers={"X-Emulator": "1"}) as r:
                assert r.status == 200
                fetched = await r.json()
            assert fetched["status"] == 200 and fetched["body"] == STUB_CONFIG
            path, headers = calls[-1]
            assert path.startswith("/get_sync_info")
            assert dict(headers).get("Authorization") == "Bearer secret-token"  # запрос от имени колонки
            blob = (tmp_path / "emulator.sqlite3").read_bytes()
            assert b"secret-token" not in blob  # Authorization только в памяти
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()
