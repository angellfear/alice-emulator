"""TASK-046: предупреждения о границах system_config.syncd.pollPeriodSec ([60, 900] с)."""
import pytest

from server_emulator.quasar_config import compose, poll_period_warnings
from test_quasar_config import FakeRuntime


def _body(value):
    return {"config": {"system_config": {"syncd": {"pollPeriodSec": value}}}}


@pytest.mark.parametrize("value", [30, 1200, "90", True, "abc"])
def test_out_of_range_or_not_number_warns(value):
    warnings = poll_period_warnings(_body(value))
    assert len(warnings) == 1
    assert "pollPeriodSec" in warnings[0]


def test_text_names_value_and_range():
    text = poll_period_warnings(_body(1200))[0]
    assert "1200" in text and "900" in text and "60" in text
    assert "30" in poll_period_warnings(_body(30))[0] and "60" in poll_period_warnings(_body(30))[0]


@pytest.mark.parametrize("value", [60, 90, 900])
def test_in_range_no_warning(value):
    assert poll_period_warnings(_body(value)) == []


@pytest.mark.parametrize("body", [{}, {"config": {}}, {"config": {"system_config": {}}},
                                  {"config": {"system_config": {"syncd": {}}}}, None])
def test_missing_key_no_warning(body):
    assert poll_period_warnings(body) == []


def test_compose_value_unchanged(tmp_path):
    runtime = FakeRuntime(tmp_path)
    body = compose(_body(1200), None, None, runtime, None)
    assert body["config"]["system_config"]["syncd"]["pollPeriodSec"] == 1200
    assert len(poll_period_warnings(body)) == 1


@pytest.mark.asyncio
async def test_composed_api_returns_warnings(tmp_path):
    from aiohttp import ClientSession

    from server_emulator.runtime import Runtime
    from server_emulator.server import create_app
    from tests.conftest import listen

    runtime = Runtime(directory=tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u1', 'name': 'U'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-1', 'user_id': 'u1',
                               'device_ids': ['id-1'], 'profile': {'platform': 'chiron'}})
    runtime.quasar_platforms.save_common(_body(1200))
    runtime.quasar_platforms.save("chiron", {"config": {"device_config": {"platform": "chiron"}}})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/quasar_composed?device=SN-1") as response:
                body = await response.json()
                assert response.status == 200
                assert body["config"]["config"]["system_config"]["syncd"]["pollPeriodSec"] == 1200
                assert len(body["warnings"]) == 1 and "1200" in body["warnings"][0]
    finally:
        await runner.cleanup()
        await runtime.close()
