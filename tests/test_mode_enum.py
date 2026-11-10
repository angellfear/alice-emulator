"""Режимы маршрута: три режима устройства (emulate | hybrid | proxy),
маршрута по умолчанию нет; миграция старой режимной модели (mode + emulation_excluded), маршрута record
и прежнего default_route_mode на устройства реестра."""
import pytest

from server_emulator.runtime import ROUTE_MODES, Runtime


def _registry(runtime):
    """Три устройства: без своего маршрута (a), со своим hybrid (b), со своим emulate (c)."""
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    for sn, alias in (("SN-A", "a-id"), ("SN-B", "b-id"), ("SN-C", "c-id")):
        runtime.identities.mutate({"operation": "device", "sn": sn, "user_id": "u", "device_ids": [alias]})
    runtime.identities.mutate({"operation": "route_mode", "sn": "SN-B", "mode": "hybrid"})
    runtime.identities.mutate({"operation": "route_mode", "sn": "SN-C", "mode": "emulate"})



async def test_route_modes_enum_rejects_unknown(tmp_path):
    runtime = Runtime(tmp_path)
    assert ROUTE_MODES == ("emulate", "hybrid", "proxy")
    _registry(runtime)
    for mode in ("emulate", "hybrid", "proxy", None):
        assert (await runtime.set_device_route_mode("SN-A", mode))["mode"] == mode
    for bad in ("partial", "record", "turbo"):  # «Запись» — опция устройства, не маршрут
        with pytest.raises(ValueError):
            await runtime.set_device_route_mode("SN-A", bad)
    assert not hasattr(runtime, "set_default_route_mode")
    await runtime.close()
