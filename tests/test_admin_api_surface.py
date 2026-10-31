"""RF-02/RF-03: поверхность админ-API стабильна при переносе доменов в реестр.

Снимок контракта: каталоговые домены отвечают на GET чем угодно, кроме 404
(содержание может требовать параметров — 400 законен); POST-only домены на
GET отвечают 405. Тест написан ДО переноса и должен проходить без изменений
после каждого шага RF-03.
"""
import pytest
from aiohttp import ClientSession

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen

CATALOG_ACTIONS = ["settings", "status", "identities", "actions", "updates",
                   "speech", "scenarios", "catalog", "endpoints",
                   "quasar_platforms", "quasar_composed", "params", "param_values",
                   "animations", "animations-media"]
POST_ONLY = ["chat", "stt", "tts", "mcp-discover", "tool-call"]


@pytest.mark.parametrize("action", CATALOG_ACTIONS + POST_ONLY)
async def test_admin_api_surface_stable(tmp_path, action):
    runtime = Runtime(tmp_path)
    app = create_app(runtime=runtime)
    runner, url = await listen(app)
    try:
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/" + action) as response:
                if action in POST_ONLY:
                    assert response.status == 405
                else:
                    assert response.status != 404
    finally:
        await runner.cleanup()
