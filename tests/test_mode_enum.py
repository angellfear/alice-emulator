"""Режимы маршрута (TASK-044): default_route_mode, миграция старой режимной модели
(mode + emulation_excluded) и сохранение выбора между запусками."""
import pytest
from aiohttp import ClientSession
from aiohttp import web

from server_emulator.runtime import ROUTE_MODES, Runtime
from server_emulator.server import create_app
from tests.conftest import listen


async def test_route_modes_enum_rejects_unknown(tmp_path):
    runtime = Runtime(tmp_path)
    assert ROUTE_MODES == ("emulate", "hybrid", "proxy", "record")
    for mode in ("hybrid", "proxy", "record"):
        result = await runtime.set_default_route_mode(mode, "wait")
        assert result["mode"] == mode and runtime.settings["default_route_mode"] == mode
    with pytest.raises(ValueError):
        await runtime.set_default_route_mode("partial", "wait")  # режим отменён
    with pytest.raises(ValueError):
        await runtime.save_settings({**runtime.settings, "default_route_mode": "turbo"})
    await runtime.close()


async def test_legacy_mode_migrates_to_route_modes(tmp_path):
    """Старая модель (mode + emulation_excluded): mode emulate → default emulate,
    mode proxy → default record; известные SN из исключений сразу получают
    route_mode=proxy, подключившиеся позже — при регистрации; ключи удаляются."""
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-REAL", "user_id": "u",
                               "device_ids": ["real-id"]})
    legacy = dict(runtime.store.load_settings())
    await runtime.close()
    legacy["mode"] = "proxy"
    legacy["emulation_excluded"] = ["SN-REAL", "SN-LATE", "SN-LATE2"]
    from server_emulator.state_store import StateStore
    store = StateStore(tmp_path / "emulator.sqlite3")
    store.save_settings(legacy)
    store.close()

    migrated = Runtime(tmp_path)
    assert migrated.settings["default_route_mode"] == "record"
    assert "mode" not in migrated.settings and "emulation_excluded" not in migrated.settings
    assert migrated.identities.resolve("real-id")["route_mode"] == "proxy"
    assert migrated.identities.pending_route_migration() == ["SN-LATE", "SN-LATE2"]
    # устройство, представившееся SN из очереди: регистрация и привязка дают route_mode=proxy
    migrated.identities.observe("SN-LATE", register=True)
    assert migrated.identities.resolve("SN-LATE")["route_mode"] == "proxy"
    migrated.identities.mutate({"operation": "device", "sn": "SN-LATE2", "device_ids": ["late2-id"]})
    assert migrated.identities.resolve("late2-id")["route_mode"] == "proxy"
    assert migrated.identities.pending_route_migration() == []
    await migrated.close()

    restarted = Runtime(tmp_path)  # миграция однократная: повторный запуск ничего не меняет
    assert restarted.settings["default_route_mode"] == "record"
    await restarted.close()


async def test_default_route_mode_survives_restart(tmp_path):
    runtime = Runtime(tmp_path)
    await runtime.set_default_route_mode("record", "wait")
    await runtime.close()
    fresh = Runtime(tmp_path)
    assert fresh.settings["default_route_mode"] == "record"
    await fresh.close()


async def test_mode_api_and_stale_form(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings["model"]["api_key"] = "keep-secret"
    stale_form = runtime.public()
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession(headers={"X-Emulator": "1"}) as client:
            async with client.put(url + "/emulator/api/mode", json={"mode": "record"}) as response:
                assert await response.json() == {"mode": "record", "connections": "wait", "disconnected": 0}
            assert runtime.settings["model"]["api_key"] == "keep-secret"
            async with client.get(url + "/emulator/api/mode") as response:
                body = await response.json()
                assert body["mode"] == "record" and body["modes"] == list(ROUTE_MODES)
            stale_form["system_prompt"] = "edited prompt"
            async with client.put(url + "/emulator/api/settings", json=stale_form) as response:
                assert response.status == 200
                assert (await response.json())["default_route_mode"] == "record"  # форма настроек не меняет маршрут
            assert runtime.settings["system_prompt"] == "edited prompt"
            async with client.put(url + "/emulator/api/mode", json={"mode": "invalid"}) as response:
                assert response.status == 400
            fresh = Runtime(tmp_path)
            assert fresh.settings["default_route_mode"] == "record"
            await fresh.close()
    finally:
        await runner.cleanup()
        await runtime.close()
