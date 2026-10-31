"""Режимы маршрута (TASK-044, TASK-054, TASK-064): три режима устройства (emulate | hybrid | proxy),
маршрута по умолчанию нет; миграция старой режимной модели (mode + emulation_excluded), маршрута record
и прежнего default_route_mode на устройства реестра."""
import pytest
from aiohttp import ClientSession

from server_emulator.runtime import ROUTE_MODES, Runtime
from server_emulator.server import create_app
from server_emulator.state_store import StateStore
from tests.conftest import listen


def _registry(runtime):
    """Три устройства: без своего маршрута (a), со своим hybrid (b), со своим emulate (c)."""
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    for sn, alias in (("SN-A", "a-id"), ("SN-B", "b-id"), ("SN-C", "c-id")):
        runtime.identities.mutate({"operation": "device", "sn": sn, "user_id": "u", "device_ids": [alias]})
    runtime.identities.mutate({"operation": "route_mode", "sn": "SN-B", "mode": "hybrid"})
    runtime.identities.mutate({"operation": "route_mode", "sn": "SN-C", "mode": "emulate"})


def _stored_legacy(tmp_path, **changes):
    """Подменить сохранённые настройки (ключи legacy-моделей)."""
    store = StateStore(tmp_path / "emulator.sqlite3")
    settings = dict(store.load_settings())
    settings.update(changes)
    store.save_settings(settings)
    store.close()


async def test_route_modes_enum_rejects_unknown(tmp_path):
    runtime = Runtime(tmp_path)
    assert ROUTE_MODES == ("emulate", "hybrid", "proxy")
    _registry(runtime)
    for mode in ("emulate", "hybrid", "proxy", None):
        assert (await runtime.set_device_route_mode("SN-A", mode))["mode"] == mode
    for bad in ("partial", "record", "turbo"):  # «Запись» — опция устройства, не маршрут (TASK-054)
        with pytest.raises(ValueError):
            await runtime.set_device_route_mode("SN-A", bad)
    assert not hasattr(runtime, "set_default_route_mode")
    await runtime.close()


async def test_legacy_mode_migrates_to_route_modes(tmp_path):
    """Старая модель (mode + emulation_excluded): mode emulate → эмуляция, mode proxy → proxy + record «Включено»
    устройствам без своего маршрута (TASK-054, TASK-064); известные SN из исключений сразу получают
    route_mode=proxy (без записи), подключившиеся позже — при регистрации; ключи удаляются."""
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-REAL", "user_id": "u",
                               "device_ids": ["real-id"]})
    runtime.identities.mutate({"operation": "device", "sn": "SN-PLAIN", "user_id": "u",
                               "device_ids": ["plain-id"]})
    legacy = dict(runtime.store.load_settings())
    await runtime.close()
    legacy["mode"] = "proxy"
    legacy["emulation_excluded"] = ["SN-REAL", "SN-LATE", "SN-LATE2"]
    store = StateStore(tmp_path / "emulator.sqlite3")
    store.save_settings(legacy)
    store.close()

    migrated = Runtime(tmp_path)
    assert "default_route_mode" not in migrated.settings
    assert "mode" not in migrated.settings and "emulation_excluded" not in migrated.settings
    assert migrated.identities.resolve("real-id")["route_mode"] == "proxy"
    assert migrated.identities.resolve("real-id")["record"] == "off"  # исключённый SN: прокси без журнала
    assert migrated.identities.resolve("plain-id")["record"] == "on"  # прежнее поведение default record
    assert migrated.identities.resolve("plain-id")["route_mode"] == "proxy"  # и маршрут прежнего default
    assert migrated.identities.pending_route_migration() == ["SN-LATE", "SN-LATE2"]
    # устройство, представившееся SN из очереди: регистрация и привязка дают route_mode=proxy
    migrated.identities.observe("SN-LATE", register=True)
    assert migrated.identities.resolve("SN-LATE")["route_mode"] == "proxy"
    migrated.identities.mutate({"operation": "device", "sn": "SN-LATE2", "device_ids": ["late2-id"]})
    assert migrated.identities.resolve("late2-id")["route_mode"] == "proxy"
    assert migrated.identities.pending_route_migration() == []
    await migrated.close()

    restarted = Runtime(tmp_path)  # миграция однократная: повторный запуск ничего не меняет
    assert "default_route_mode" not in restarted.settings
    assert restarted.identities.resolve("plain-id")["record"] == "on"
    assert restarted.identities.resolve("plain-id")["route_mode"] == "proxy"
    await restarted.close()


async def test_legacy_mode_emulate_leaves_devices_in_emulation(tmp_path):
    """mode: emulate → устройства без своего маршрута остаются в эмуляции, свои маршруты не меняются."""
    runtime = Runtime(tmp_path)
    _registry(runtime)
    await runtime.close()
    _stored_legacy(tmp_path, mode="emulate")
    migrated = Runtime(tmp_path)
    try:
        assert "mode" not in migrated.settings and "default_route_mode" not in migrated.settings
        modes = {sn: migrated.identities.resolve(alias).get("route_mode")
                 for sn, alias in (("SN-A", "a-id"), ("SN-B", "b-id"), ("SN-C", "c-id"))}
        assert modes == {"SN-A": None, "SN-B": "hybrid", "SN-C": "emulate"}
    finally:
        await migrated.close()


@pytest.mark.parametrize("old, expected", [("proxy", "proxy"), ("hybrid", "hybrid"), ("emulate", None)])
async def test_default_route_mode_moves_to_devices_once(tmp_path, old, expected):
    """TASK-064: прежний default_route_mode ≠ emulate записывается устройствам реестра без своего
    маршрута; устройства со своим маршрутом не меняются; ключа в настройках больше нет; однократно."""
    runtime = Runtime(tmp_path)
    _registry(runtime)
    await runtime.close()
    _stored_legacy(tmp_path, default_route_mode=old)

    migrated = Runtime(tmp_path)
    try:
        assert "default_route_mode" not in migrated.settings
        assert migrated.identities.resolve("a-id").get("route_mode") == expected
        assert migrated.identities.resolve("b-id")["route_mode"] == "hybrid"  # свой маршрут — без изменений
        assert migrated.identities.resolve("c-id")["route_mode"] == "emulate"
        # новое устройство после миграции — без маршрута, то есть эмуляция
        migrated.identities.observe("SN-NEW", register=True)
        assert migrated.identities.resolve("SN-NEW").get("route_mode") is None
    finally:
        await migrated.close()
    assert "default_route_mode" not in StateStore(tmp_path / "emulator.sqlite3").load_settings()

    # пользователь вернул устройству эмуляцию (None); повторный запуск не возвращает прежний default
    again = Runtime(tmp_path)
    try:
        await again.set_device_route_mode("SN-A", None)
    finally:
        await again.close()
    fresh = Runtime(tmp_path)
    try:
        assert fresh.identities.resolve("a-id").get("route_mode") is None
        assert "default_route_mode" not in fresh.settings
    finally:
        await fresh.close()


async def test_default_route_mode_is_not_saved_from_stale_form(tmp_path):
    """Устаревшая форма настроек присылает default_route_mode: он не сохраняется, прочие поля — да;
    API `mode` удалён."""
    runtime = Runtime(tmp_path)
    runtime.settings["model"]["api_key"] = "keep-secret"
    stale_form = runtime.public()
    stale_form["default_route_mode"] = "proxy"
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession(headers={"X-Emulator": "1"}) as client:
            stale_form["system_prompt"] = "edited prompt"
            async with client.put(url + "/emulator/api/settings", json=stale_form) as response:
                assert response.status == 200
                assert "default_route_mode" not in await response.json()
            assert runtime.settings["system_prompt"] == "edited prompt"
            assert runtime.settings["model"]["api_key"] == "keep-secret"
            for method in (client.get, client.put):
                async with method(url + "/emulator/api/mode") as response:
                    assert response.status == 404
            fresh = Runtime(tmp_path)
            assert "default_route_mode" not in fresh.settings
            await fresh.close()
    finally:
        await runner.cleanup()
        await runtime.close()
