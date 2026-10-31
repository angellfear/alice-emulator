"""TASK-027: шаги JSON fetch/extract/load/refresh/store/branch/set/wait_state удалены.

Сценарий с таким шагом не проходит валидацию («шаг <op> больше не поддерживается — перенесите
в py») ни при сохранении, ни при загрузке; в JSON остаются compose / action / ask / finish / error."""
import copy

import pytest
from aiohttp import ClientSession

from server_emulator.agent import scenario_edit
from server_emulator.runtime import Runtime
from server_emulator.scenario_packages.validate import (
    FLOW_OPS, LEGACY_OPS, PackageError, legacy_ops, validate_package, validate_scenario)
from server_emulator.server import create_app
from tests.conftest import listen

def api_headers():
    return {"X-Emulator": "1"}


SIMPLE = {"format_version": 2, "id": "user.simple", "version": "1.0.0", "category": "custom",
          "triggers": [{"kind": "phrase", "name": "привет"}],
          "flow": [{"id": "say", "op": "compose", "reply": {"text": "Привет"}}, {"id": "done", "op": "finish"}]}
SETTER = {**SIMPLE, "id": "user.setter",
          "flow": [{"id": "s", "op": "set", "name": "x", "value": "1"},
                   {"id": "say", "op": "compose", "reply": {"text": "Привет"}}, {"id": "done", "op": "finish"}]}


def test_flow_ops_registry_is_closed_to_simple_steps():
    assert set(FLOW_OPS) == {"resolve", "action", "compose", "ask", "finish", "error"}
    assert set(LEGACY_OPS) == {"fetch", "extract", "load", "refresh", "store", "branch", "set", "wait_state"}
    assert not set(LEGACY_OPS) & set(FLOW_OPS)
    assert legacy_ops(SIMPLE) == []
    assert legacy_ops(SETTER) == ["set"]
    assert legacy_ops({"executor": {"kind": "script"}}) == []


@pytest.mark.parametrize("op", LEGACY_OPS)
def test_every_legacy_op_is_rejected_with_move_to_py_message(op):
    scenario = copy.deepcopy(SIMPLE)
    scenario["flow"].insert(0, {"id": "old", "op": op})
    with pytest.raises(PackageError, match=f"шаг {op} больше не поддерживается — перенесите в py"):
        validate_scenario(scenario)


def test_simple_scenario_still_valid():
    assert validate_scenario(copy.deepcopy(SIMPLE))["id"] == "user.simple"


def test_package_with_legacy_step_is_rejected():
    package = {"manifest": {"format": "scenario-package", "version": 1}, "scenarios": [copy.deepcopy(SETTER)]}
    with pytest.raises(PackageError, match="перенесите в py"):
        validate_package(package)
    package["scenarios"] = [copy.deepcopy(SIMPLE)]
    validate_package(package)


async def test_put_scenarios_rejects_legacy(tmp_path):
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async def put(scenario):
                async with client.put(url + "/emulator/api/scenarios", headers=api_headers(),
                                      json={"scenario": scenario}) as r:
                    return r.status, await r.json()
            assert (await put(SIMPLE))[0] == 200
            status, body = await put(SETTER)
            assert status == 400 and "перенесите в py" in str(body)
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_agent_drafts_reject_legacy(tmp_path):
    runtime = Runtime(tmp_path)
    draft = scenario_edit.create(runtime, scenario={**SETTER, "id": "agent.setter"})
    assert "перенесите в py" in draft["error"]
    with pytest.raises(PackageError):
        await scenario_edit.activate(runtime, draft["draft_id"])
    simple = scenario_edit.create(runtime, scenario={**SIMPLE, "id": "agent.simple"})
    assert simple["error"] is None
    patched = scenario_edit.patch(runtime, simple["draft_id"], [
        {"op": "add", "path": "/flow/0", "value": {"id": "s", "op": "store", "key": "k", "value": "1"}}])
    assert "перенесите в py" in patched["error"]
    await runtime.close()
