"""Режимная модель (решение пользователя 2026-10-24): ровно 2 режима —
proxy/emulate; сохранённый partial миграцией переводится в emulate."""


import pytest
from aiohttp import ClientSession
from aiohttp import web

from server_emulator.runtime import MODES, Runtime
from server_emulator.server import create_app


async def test_mode_enum_two_modes_rejects_partial_and_unknown(tmp_path):
    runtime = Runtime(tmp_path)
    assert MODES == ("proxy", "emulate")
    for mode in ("proxy", "emulate"):
        result = await runtime.set_mode(mode, "wait")
        assert result["mode"] == mode and runtime.settings["mode"] == mode
    with pytest.raises(ValueError):
        await runtime.set_mode("partial", "wait")  # режим отменён 2026-10-24
    with pytest.raises(ValueError):
        await runtime.set_mode("full", "wait")
    with pytest.raises(ValueError):
        await runtime.save_settings({**runtime.settings, "mode": "turbo"})
    await runtime.close()


async def test_legacy_partial_migrates_to_emulate(tmp_path):
    """Старые настройки с partial/emulation_ws_local читаются один раз и
    мигрируют: partial → emulate, упразднённый список отбрасывается."""
    runtime = Runtime(tmp_path)
    await runtime.close()
    seed = Runtime(tmp_path)
    legacy = dict(seed.store.load_settings())
    await seed.close()
    legacy["mode"] = "partial"
    legacy["emulation_ws_local"] = ["SN-X"]
    from server_emulator.state_store import StateStore
    store = StateStore(tmp_path / "emulator.sqlite3")
    store.save_settings(legacy)
    store.close()

    migrated = Runtime(tmp_path)
    assert migrated.settings["mode"] == "emulate"
    assert "emulation_ws_local" not in migrated.settings
    assert migrated.store.load_settings()["mode"] == "emulate"
    await migrated.close()


async def test_saved_mode_survives_restart_unchanged(tmp_path):
    runtime = Runtime(tmp_path)
    await runtime.set_mode("emulate", "wait")
    restarted = Runtime(tmp_path)
    assert restarted.settings["mode"] == "emulate"
    await restarted.close()
    await runtime.set_mode("proxy", "wait")
    fresh = Runtime(tmp_path)
    assert fresh.settings["mode"] == "proxy"
    await fresh.close()
    await runtime.close()


async def test_standalone_forces_emulate_and_rejects_proxy(tmp_path):
    runtime = Runtime(tmp_path)
    await runtime.set_mode("proxy", "wait")
    runner = web.AppRunner(create_app(runtime=runtime))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        assert runtime.settings["mode"] == "emulate"  # форс в памяти
        assert runtime.store.load_settings()["mode"] == "proxy"  # сохранённое не меняется форсом
        async with ClientSession() as client:
            async with client.get(f"http://127.0.0.1:{port}/emulator/api/mode") as response:
                body = await response.json()
                assert response.status == 200 and body["mode"] == "emulate" and body["modes"] == list(MODES)
                assert "ws_local" not in body  # список упразднён вместе с partial
            async with client.post(f"http://127.0.0.1:{port}/emulator/api/mode",
                                   json={"mode": "proxy"}, headers={"X-Emulator": "1"}) as response:
                assert response.status == 400
                assert "эмуляции" in (await response.json())["error"]
    finally:
        await runner.cleanup()
    await runtime.close()
