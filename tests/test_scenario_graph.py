"""AUD-04/AUD-05: граф исполнения сценария и границы файловых путей пакета."""
import hashlib

import pytest

from server_emulator.scenario_packages.validate import PackageError, validate_package, validate_scenario


def scenario(flow, slots=None):
    base = {
        "format_version": 2,
        "id": "test.flow",
        "version": "1.0.0",
        "triggers": [{"kind": "phrase", "name": "фраза"}],
        "flow": flow,
    }
    if slots is not None:
        base["slots"] = slots
    return base


def step(sid, op, **kwargs):
    return dict(id=sid, op=op, **kwargs)


VALID_ASK = scenario(
    [
        step("ask1", "ask", question="Сколько?", **{"await": "count", "next": "done"}),
        step("done", "finish"),
    ],
    slots={"count": {"type": "integer"}},
)


def test_valid_flow_with_ask_and_branches_passes():
    validate_scenario(scenario(
        [
            step("act", "action", ref="volume.set", arguments={"level": 5}, next="check", on_failure="fail"),
            step("check", "action", ref="volume.check", arguments={}, next="done"),
            step("done", "finish"),
            step("fail", "error", message="не удалось"),
        ]
    ))
    validate_scenario(VALID_ASK)


def test_next_reference_to_unknown_step_rejected():
    with pytest.raises(PackageError, match="неизвестный шаг"):
        validate_scenario(scenario([
            step("a", "action", ref="x.y", arguments={}, next="missing"),
            step("done", "finish"),
        ]))


def test_on_failure_reference_validated():
    with pytest.raises(PackageError, match="on_failure"):
        validate_scenario(scenario([
            step("a", "action", ref="x.y", arguments={}, on_failure="nowhere", next="done"),
            step("done", "finish"),
        ]))


def test_unreachable_step_rejected():
    with pytest.raises(PackageError, match="недостижимые"):
        validate_scenario(scenario([
            step("a", "action", ref="x.y", arguments={}, next="done"),
            step("done", "finish"),
            step("orphan", "action", ref="x.y", arguments={}, next="done"),
        ]))


def test_cycle_without_terminal_exit_rejected():
    # вход → цикл loop_a↔loop_b без выхода; терминал достижим только из on_failure входа
    with pytest.raises(PackageError, match="нет пути к finish/error"):
        validate_scenario(scenario([
            step("entry", "action", ref="x.y", arguments={}, next="loop_a", on_failure="done"),
            step("loop_a", "action", ref="x.y", arguments={}, next="loop_b"),
            step("loop_b", "action", ref="x.y", arguments={}, next="loop_a"),
            step("done", "finish"),
        ]))


def test_cycle_with_exit_is_allowed():
    # цикл сам по себе допустим (исполнитель ограничивает шаги/deadline),
    # если из него есть путь к терминалу
    validate_scenario(scenario([
        step("a", "action", ref="x.y", arguments={}, next="b", on_failure="done"),
        step("b", "action", ref="x.y", arguments={}, next="a"),
        step("done", "finish"),
    ]))


def test_fallthrough_without_next_follows_list_order():
    # нет явного next → продолжение следующим шагом списка (как в fixture local.clock)
    validate_scenario(scenario([
        step("compose", "compose", reply={"text": "{{clock|time_ru}}"}),
        step("done", "finish"),
    ]))


def test_no_reachable_terminal_rejected():
    with pytest.raises(PackageError, match="не достижим ни один finish/error"):
        validate_scenario(scenario([
            step("a", "action", ref="x.y", arguments={}, next="b"),
            step("b", "action", ref="x.y", arguments={}, next="a"),
        ]))


def test_action_arguments_must_be_object():
    with pytest.raises(PackageError, match="arguments"):
        validate_scenario(scenario([
            step("a", "action", ref="x.y", arguments=[1, 2], next="done"),
            step("done", "finish"),
        ]))


def test_empty_ref_rejected():
    with pytest.raises(PackageError, match="ref"):
        validate_scenario(scenario([
            step("a", "action", ref="", next="done"),
            step("done", "finish"),
        ]))


def test_question_must_be_non_empty():
    bad = scenario(
        [
            step("ask1", "ask", question="", **{"await": "count", "next": "done"}),
            step("done", "finish"),
        ],
        slots={"count": {"type": "integer"}},
    )
    with pytest.raises(PackageError, match="question"):
        validate_scenario(bad)


def test_format_version_1_rejected():
    legacy = scenario([step("done", "finish")])
    legacy["format_version"] = 1
    with pytest.raises(PackageError, match="format_version"):
        validate_scenario(legacy)


def test_compose_requires_exactly_one_of_reply_or_raw():
    with pytest.raises(PackageError, match="ровно одно"):
        validate_scenario(scenario([step("c", "compose"), step("done", "finish")]))
    with pytest.raises(PackageError, match="ровно одно"):
        validate_scenario(scenario([step("c", "compose", reply={"text": "x"}, raw={"fields": {}}, debug=True),
                                    step("done", "finish")]))


def test_raw_and_payload_raw_only_with_debug():
    with pytest.raises(PackageError, match="debug"):
        validate_scenario(scenario([step("c", "compose", raw={"proto": "NAlice.NAliceApi.TServerMessage",
                                                              "fields": {}}),
                                    step("done", "finish")]))
    validate_scenario(scenario([step("c", "compose", debug=True,
                                     raw={"proto": "NAlice.NAliceApi.TServerMessage", "fields": {}}),
                                step("done", "finish")]))
    with pytest.raises(PackageError, match="payload_raw"):
        validate_scenario(scenario([step("c", "compose", reply={"directives": [
            {"name": "x", "payload_raw": "AAAA"}]}), step("done", "finish")]))
    validate_scenario(scenario([step("c", "compose", reply={"directives": [
        {"name": "x", "payload_raw": "AAAA", "debug": True}]}), step("done", "finish")]))


# --- AUD-05: границы файловых путей пакета ----------------------------------


def _package_with_schema(name):
    return {
        "manifest": {"format": "scenario-package", "version": 1},
        "scenarios": [scenario([step("done", "finish")])],
        "schemas": [{"file": name, "sha256": "0" * 64}],
    }


def test_schema_traversal_outside_package_rejected(tmp_path):
    (tmp_path / "secret.pb").write_bytes(b"secret")
    (tmp_path / "schemas").mkdir()
    with pytest.raises(PackageError, match="за пределы пакета"):
        validate_package(_package_with_schema("../secret.pb"), root=tmp_path)


def test_schema_absolute_path_rejected(tmp_path):
    with pytest.raises(PackageError, match="за пределы пакета"):
        validate_package(_package_with_schema(str(tmp_path / "secret.pb")), root=tmp_path)


def test_schema_inside_subdirectory_allowed(tmp_path):
    import shutil
    real = tmp_path / "schemas"
    real.mkdir()
    (real / "nested").mkdir()
    blob = b"payload"
    (real / "nested" / "x.pb").write_bytes(blob)
    package = _package_with_schema("nested/x.pb")
    package["schemas"][0]["sha256"] = hashlib.sha256(blob).hexdigest()
    validate_package(package, root=tmp_path)  # вложенный легальный путь — OK
