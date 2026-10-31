"""S01/S09: API каталога сценариев, dry-run, экспорт. Реестр пакетов (TASK-036) — tests/test_registry_migration.py."""
from aiohttp import ClientSession

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen

REPLY_SCENARIO = {
    "format_version": 2, "id": "user.hello", "version": "1.0.0", "category": "custom",
    "triggers": [{"kind": "phrase", "phrases": ["привет"]}],
    "flow": [{"id": "done", "op": "finish"}],
    "publication": {"enabled": False},
}


def api_headers():
    return {"X-Emulator": "1"}


async def test_scenario_crud_and_routing(tmp_path):
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/scenarios") as r:
                assert r.status == 200 and (await r.json())["scenarios"] == []
            async with client.put(url + "/emulator/api/scenarios", headers=api_headers(),
                                  json={"scenario": REPLY_SCENARIO}) as r:
                assert r.status == 200 and (await r.json())["saved"] == "user.hello"
            bad = dict(REPLY_SCENARIO, format_version=9)
            async with client.put(url + "/emulator/api/scenarios", headers=api_headers(),
                                  json={"scenario": bad}) as r:
                assert r.status == 400  # валидатор rejects
            # dry-run: сценарий отвечает без устройства и без модели
            runtime.settings["aapi_model"] = False
            async with client.post(url + "/emulator/api/scenarios", headers=api_headers(),
                                   json={"text": "привет"}) as r:
                report = await r.json()
                assert report["handled"] and report["scenario"] == "user.hello"
            async with client.post(url + "/emulator/api/scenarios", headers=api_headers(),
                                   json={"text": "неизвестная фраза"}) as r:
                report = await r.json()
                assert report["handled"] is False  # уйдёт в AI/fallback
            async with client.delete(url + "/emulator/api/scenarios?id=user.hello",
                                     headers=api_headers()) as r:
                assert r.status == 200
            assert runtime.settings["scenarios"] == []
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_scenario_export_as_registry_package(tmp_path):
    """Экспорт сценария валидным пакетом реестра, без служебных полей."""
    runtime = Runtime(tmp_path)
    runtime.settings["scenarios"] = [dict(REPLY_SCENARIO, enabled=False, _group="clock/watches")]
    runner, url = await listen(create_app(runtime=runtime))
    try:
        from server_emulator.scenario_packages.validate import validate_package
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/scenarios?export=user.hello") as r:
                assert r.status == 200
                package = await r.json()
            validate_package(package)  # экспорт — валидный пакет реестра
            exported = package["scenarios"][0]
            assert exported["id"] == "user.hello"
            assert "enabled" not in exported and "_group" not in exported  # служебное вычищено
    finally:
        await runner.cleanup()
        await runtime.close()
