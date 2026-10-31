"""H02: probe — side-effect-free решение о поддержке с reason codes."""
from server_emulator.scenario_engine.dialog import DialogManager
from server_emulator.scenario_engine.executor import Executor
from server_emulator.scenario_engine.library import SCENARIOS
from server_emulator.scenario_engine.probe import dependencies_ok, probe
from server_emulator.scenario_packages.validate import validate_scenario
from server_emulator.state_store import StateStore


def scenario_with_action(sid="x.act", ref="light.on", phrase="включи свет"):
    return validate_scenario({
        "format_version": 2, "id": sid, "version": "1.0.0",
        "triggers": [{"kind": "phrase", "name": phrase}],
        "flow": [{"id": "a", "op": "action", "ref": ref, "arguments": {}},
                 {"id": "done", "op": "finish"}]})


async def test_supported_when_dependency_registered():
    executor = Executor(actions={"light.on": lambda a, e: True})
    verdict, reason, scenario = probe([scenario_with_action()], executor, text="включи свет")
    assert verdict == "supported" and reason == "phrase:exact" and scenario["id"] == "x.act"


async def test_unsupported_missing_dependency_no_side_effects(tmp_path):
    sent = []

    async def send(data):
        sent.append(data)
    executor = Executor(store=StateStore(tmp_path / "e.sqlite3"))  # без light.on
    dialog = DialogManager(executor, lambda: [scenario_with_action()],
                           __import__("tests.test_dialog_transport", fromlist=["server_type"]).server_type())
    report = await dialog.handle_text("включи свет", {"input": {}}, "dlg-p", send)
    assert report == {"handled": False, "verdict": "unsupported",
                      "reason": "missing_dependency:action:light.on", "status": None}
    assert sent == []  # ничего не исполнено и не отправлено
    assert executor.pending() == []


def test_dependencies_reject_v1():
    scenario = scenario_with_action()
    assert dependencies_ok(scenario, Executor(actions={"light.on": lambda a, e: True})) == ("supported", "deps")
    legacy = {**scenario, "format_version": 1}
    assert dependencies_ok(legacy, Executor()) == ("rejected", "format_version:1")


async def test_ambiguous_tie_reported():
    executor = Executor(actions={"light.on": lambda a, e: True})
    two = [scenario_with_action("x.act"), scenario_with_action("y.act")]
    verdict, reason, _ = probe(two, executor, text="включи свет")
    assert verdict == "ambiguous" and "tie:y.act" in reason


async def test_local_catalog_not_gated_by_publication():
    # публикационный гейт — атрибут реестра пакетов (v9 §7), не локального каталога
    verdict, _, _ = probe([scenario_with_action()], Executor(actions={"light.on": lambda a, e: True}),
                          text="включи свет")
    assert verdict == "supported"
    verdict, _, scenario = probe([SCENARIOS["informers.clock"]], Executor(),
                                 text="сколько времени")
    assert verdict == "supported" and scenario["id"] == "informers.clock"


async def test_builtin_pair_resolved_by_curated_order():
    """Встроенный каталог курируется (priority): погода («погода») раньше generic
    app.open («открой», priority 200) — равное вхождение не даёт ambiguous."""
    catalog = list(SCENARIOS.values())
    executor = Executor()
    verdict, reason, scenario = probe(catalog, executor, text="открой погода")
    assert verdict == "supported" and reason == "phrase:contains"
    assert scenario["id"] == "informers.weather"
    # конфликт пользовательского сценария с builtin по-прежнему честно ambiguous
    user = scenario_with_action("user.weather", phrase="погода")
    executor.actions["light.on"] = lambda a, e: True
    verdict, reason, _ = probe(catalog + [user], executor, text="открой погода")
    assert verdict == "ambiguous" and "user.weather" in reason
