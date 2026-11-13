"""Плагин Home Assistant: точки умного дома (SDK 2.8.0 extension_runtime) и сущности HA в реестре.

Поддельный HA — локальные WS/REST серверы (настоящий из тестов не дёргается):
WS /api/websocket — auth_required → auth (test-token) → auth_ok → команды config-реестров
(комнаты) → подписка subscribe_entities в проводном формате HA (ack result; снимок
event "a" {s, a, c, lc}; дельты event "c" — "+" частичен, "-" удаляет ключи атрибутов;
удаление "r"); REST /api/services/<домен>/<сервис> пишет вызовы в список; REST-реестры
`/api/config/*` не обслуживаются — настоящий HA с версии 2026.5 отвечает на них 404,
комнаты берутся WS-командами. Настройка url плагина — адрес локальной заглушки;
фикстуры обезличены: токен test-token, документные адреса — 192.0.2.0/24.
"""
import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

from server_emulator import sdk
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen

API = {"X-Emulator": "1"}
TOKEN = "test-token"
ENTITY = "light.kitchen"
EXT_ID = "builtin.home-assistant"
AREA = "kitchen-area"


def load_smart_home():
    """Модуль aapi/smart_home.py плагина — изолированная загрузка (как у реестра)."""
    import importlib.util
    path = Path(sdk.__file__).parent / "plugins" / "home-assistant" / "aapi" / "smart_home.py"
    spec = importlib.util.spec_from_file_location(f"ha_smart_home_{id(object())}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def fake_ha(requests, events, release=None, registries_ok=True):
    """Поддельный HA: WS (auth → команды config-реестров → подписка → ack result +
    снимок "a" → события теста), REST сервисов. events — полезные нагрузки event
    ({"c": …}, {"r": …}); release — сигнал теста: держим снимок, пока не выставлен
    (первая проверка видит снимок); registries_ok=False — config-команды отклонены
    (degradation: комнаты недоступны)."""
    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        auth = json.loads((await ws.receive()).data)
        if auth.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid"})
            return ws
        await ws.send_json({"type": "auth_ok"})
        registries = {
            "config/area_registry/list": [{"area_id": AREA, "name": "Кухня"}],
            "config/entity_registry/list": [{"entity_id": ENTITY, "area_id": AREA}],
        }
        snapshot = {ENTITY: {"s": "on", "a": {"friendly_name": "Кухня свет", "brightness": 127},
                             "c": "ctx-1", "lc": 1000.0}}
        async for message in ws:  # команды клиента по порядку: реестры, подписка
            command = json.loads(message.data)
            kind = command.get("type")
            if kind == "subscribe_entities":
                identifier = command.get("id")
                # как у настоящего HA: подписка подтверждается result (null), данные — событиями
                await ws.send_json({"id": identifier, "type": "result", "success": True, "result": None})
                await ws.send_json({"id": identifier, "type": "event", "event": {"a": snapshot}})
                if release is not None:
                    await release.wait()
                for event in events:  # тест управляет моментом/содержимым событий
                    await ws.send_json({"id": identifier, "type": "event", "event": event})
                continue
            await ws.send_json({"id": command.get("id"), "type": "result",
                                "success": bool(registries_ok),
                                "result": registries.get(kind, [])})
        return ws

    async def service(request):
        requests.append({"domain": request.match_info["domain"],
                         "service": request.match_info["service"],
                         "data": await request.json()})
        return web.json_response([])

    app = web.Application()
    app.router.add_get("/api/websocket", ws_handler)
    app.router.add_post("/api/services/{domain}/{service}", service)
    return await listen(app)


def registry_map(runtime):
    data = sdk.smart_home_devices()
    devices = data.get("devices") if isinstance(data, dict) else data
    return {d["id"]: d for d in devices}


# --- SDK 2.8.0: extension_runtime ------------------------------------------------------

async def test_extension_runtime_spawn_stop_idempotent(tmp_path):
    runtime = Runtime(tmp_path)
    handle = sdk.extension_runtime("test-ext")
    flag = asyncio.Event()

    async def job():
        flag.set()
        await asyncio.sleep(30)

    task1 = handle.spawn(job())  # обёртка запуска; живая задача — handle._task
    await asyncio.wait_for(flag.wait(), 3)
    assert handle.alive and handle._task is not None and not handle._task.done()
    handle.spawn(asyncio.sleep(0))  # идемпотентность: прежняя задача остановлена
    await asyncio.sleep(0.1)
    assert task1.done()  # обёртка прежнего spawn завершена
    handle.stop()
    await asyncio.sleep(0.05)
    assert not handle.alive
    await runtime.close()


async def test_extension_runtime_stop_from_thread(tmp_path):
    runtime = Runtime(tmp_path)
    handle = sdk.extension_runtime("test-ext")
    await handle.spawn(asyncio.sleep(30))
    assert handle.alive
    import threading
    thread = threading.Thread(target=handle.stop)  # как хук hooks.on_disable (поток)
    thread.start()
    await asyncio.sleep(0.3)
    thread.join()
    assert not handle.alive
    await runtime.close()


def test_extension_runtime_without_runtime_raises(monkeypatch):
    monkeypatch.setattr(sdk, "_RUNTIME", None)  # reload(sdk) пересоздал бы классы SDK
    with pytest.raises(sdk.SDKError):
        sdk.extension_runtime("test-ext")


# --- плагин: сущности HA → реестр, кеш, дельты, действия --------------------------------

async def test_ha_entities_fill_registry_and_actions(tmp_path):
    # проводной формат подписки: снимок event "a" ({s, a, c, lc}); дельта "c" —
    # "+" частичен, "-" удаляет ключи атрибутов; "r" — сущность удалена из HA
    events = [{"c": {ENTITY: {"+": {"s": "off"}, "-": {"a": ["brightness"]}}}},
              {"r": [ENTITY]}]
    requests = []
    runtime = Runtime(tmp_path)
    release = asyncio.Event()
    ha_runner, ha_url = await fake_ha(requests, events, release)
    await runtime.extensions.set_values("builtin.home-assistant",
                                        {"url": ha_url, "token": TOKEN})
    module = load_smart_home()
    handle = sdk.extension_runtime("builtin.home-assistant")
    try:
        await handle.spawn(module._run_forever(handle, asyncio.Event()))
        await asyncio.sleep(0.4)  # снимок сущностей → реестр
        # комнаты — WS-командами (REST-реестров у заглушки нет, как у HA 2026.5);
        # SDK префиксует id устройства расширением-источником; комната — area_id HA
        device = registry_map(runtime).get(f"{EXT_ID}:{ENTITY}")
        assert device is not None and device["type"] == "LightDeviceType"
        assert device["name"] == "Кухня свет" and device["online"] is True
        assert device["room"] == AREA
        assert sdk.smart_home_devices()["rooms"] == {AREA: "Кухня", "unassigned": "Не назначено"}
        assert {"type": "on_off", "value": True} in device["capabilities"]
        assert {"type": "level", "value": 50} in device["capabilities"]  # 127/2.55 ≈ 50
        # дельта: состояние off, яркость удалена; атрибуты сливаются с кешем снимка
        release.set()
        await asyncio.sleep(0.4)
        device = registry_map(runtime)[f"{EXT_ID}:{ENTITY}"]
        assert device["name"] == "Кухня свет"  # имя живёт в кеше, дельта его не теряет
        assert {"type": "on_off", "value": False} in device["capabilities"]
        assert {"type": "level", "value": 50} not in device["capabilities"]  # "-" убрал яркость
        assert device["online"] is False  # "r": сущность удалена из HA
        # действие карточки → сервис HA (REST)
        result = await module.action_handler(
            {"id": f"{EXT_ID}:{ENTITY}"}, {"instance": "on_off", "capability": "on_off", "value": True})
        assert result == []
        assert requests[0]["domain"] == "light" and requests[0]["service"] == "turn_on"
        assert requests[0]["data"]["entity_id"] == ENTITY
    finally:
        handle.stop()  # сначала клиент (WS-соединение закрывается), затем заглушка —
        await asyncio.sleep(0.1)  # иначе cleanup ждёт завершения её ws_handler с живым клиентом
        await ha_runner.cleanup()
        await runtime.close()


async def test_ha_rooms_unavailable_still_registers(tmp_path):
    # config-команды комнат отклонены: сущности в реестре без комнат,
    # соединение живёт (комнаты не критичны для реестра)
    runtime = Runtime(tmp_path)
    requests = []
    ha_runner, ha_url = await fake_ha(requests, [], registries_ok=False)
    await runtime.extensions.set_values("builtin.home-assistant",
                                        {"url": ha_url, "token": TOKEN})
    module = load_smart_home()
    handle = sdk.extension_runtime("builtin.home-assistant")
    try:
        await handle.spawn(module._run_forever(handle, asyncio.Event()))
        await asyncio.sleep(0.4)
        device = registry_map(runtime).get(f"{EXT_ID}:{ENTITY}")
        assert device is not None and device["online"] is True
        assert device.get("room") == "unassigned"  # комнаты недоступны — псевдокомната
        assert sdk.smart_home_devices()["rooms"] == {"unassigned": "Не назначено"}
        assert handle.alive  # сбой комнат не рвёт соединение
    finally:
        handle.stop()
        await asyncio.sleep(0.1)
        await ha_runner.cleanup()
        await runtime.close()


async def test_ha_action_maps_level_and_domain(tmp_path):
    runtime = Runtime(tmp_path)
    requests = []
    ha_runner, ha_url = await fake_ha(requests, [])
    await runtime.extensions.set_values("builtin.home-assistant",
                                        {"url": ha_url, "token": TOKEN})
    module = load_smart_home()
    handle = sdk.extension_runtime("builtin.home-assistant")
    try:
        await handle.spawn(asyncio.sleep(30))  # задача жива — ленивый старт не пересоздаёт
        await module.action_handler(
            {"id": "ha:light.lamp"}, {"instance": "level", "capability": "level", "value": 50})
        assert requests[0]["domain"] == "light" and requests[0]["service"] == "turn_on"
        assert requests[0]["data"]["brightness"] == 127  # 50 × 2.55 → 127 (float 127.499…)
        await module.action_handler(
            {"id": "ha:cover.blinds"}, {"instance": "level", "capability": "level", "value": 30})
        assert requests[1]["domain"] == "cover" and requests[1]["service"] == "set_cover_position"
        assert requests[1]["data"]["position"] == 30
        await module.action_handler(
            {"id": "ha:climate.thermo"}, {"instance": "level", "capability": "level", "value": 22})
        assert requests[2]["domain"] == "climate" and requests[2]["service"] == "set_temperature"
        assert requests[2]["data"]["temperature"] == 22
        # родные сервисы on_off доменов (не turn_on/turn_off)
        await module.action_handler(
            {"id": "ha:media_player.box"}, {"instance": "on_off", "capability": "on_off", "value": True})
        assert requests[3]["domain"] == "media_player" and requests[3]["service"] == "media_play"
        await module.action_handler(
            {"id": "ha:vacuum.bot"}, {"instance": "on_off", "capability": "on_off", "value": False})
        assert requests[4]["domain"] == "vacuum" and requests[4]["service"] == "stop"
        await module.action_handler(
            {"id": "ha:cover.blinds"}, {"instance": "on_off", "capability": "on_off", "value": True})
        assert requests[5]["domain"] == "cover" and requests[5]["service"] == "open_cover"
    finally:
        await ha_runner.cleanup()
        handle.stop()
        await runtime.close()


async def test_ha_action_without_config_finishes_quietly(tmp_path):
    runtime = Runtime(tmp_path)  # настройки не заданы: клиент тихо завершается
    module = load_smart_home()
    handle = sdk.extension_runtime("builtin.home-assistant")
    requests = []
    ha_runner, _ha_url = await fake_ha(requests, [])
    try:
        await handle.spawn(module._run_forever(handle, asyncio.Event()))
        await asyncio.sleep(0.3)
        assert not handle.alive  # не настроено — задача тихо завершилась
        assert requests == []  # вызовов к заглушке нет
    finally:
        await ha_runner.cleanup()
        handle.stop()
        await runtime.close()
