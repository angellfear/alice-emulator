"""R06 / v9 §4: executor — приёмочный список: уточнение, да/нет, отмена, повтор
события (идемпотентность), restart во время ожидания, отсутствующая зависимость,
ошибка действия, одноразовый side effect, лимиты, correlation IDs."""
import asyncio
import hashlib
import pathlib

import pytest

from server_emulator.scenario_engine.executor import MAX_STEPS, Execution, Executor, Outcome
from server_emulator.scenario_engine.router import select
from server_emulator.scenario_packages.validate import validate_scenario
from server_emulator.state_store import StateStore
from tests.conftest import CTX, step


def scenario(flow, slots=None, sid="test.flow"):
    base = {"format_version": 2, "id": sid, "version": "1.0.0",
            "triggers": [{"kind": "phrase", "phrases": ["test"]}],
            "flow": flow}
    if slots is not None:
        base["slots"] = slots
    return validate_scenario(base)


# --- базовые проходы -----------------------------------------------------------


async def test_resolve_action_compose_finish_with_trace_and_ids():
    calls = []

    def volume(arguments, execution):
        calls.append(arguments)
        return {"level": arguments["level"]}

    executor = Executor(actions={"volume.set": volume},
                        resolvers={"slots.count": lambda execution: 3},
                        schemas_root=pathlib.Path("server_emulator/schemas"))
    flow = scenario([
        step("r", "resolve", ref="slots.count"),
        step("a", "action", ref="volume.set", arguments={"level": 5}),
        step("c", "compose", reply={"text": "Сейчас {{clock|time_ru}}."}),
        step("done", "finish"),
    ], slots={"count": {"type": "integer"}})
    outcome = await executor.execute(flow, CTX, "dlg-1")
    assert isinstance(outcome, Outcome) and outcome.status == "finished"
    assert [entry["op"] for entry in outcome.trace] == ["resolve", "action", "compose", "finish"]
    assert calls == [{"level": 5}]
    assert outcome.slots["count"] == 3
    assert len(outcome.composed) == 1
    ref, message = outcome.composed[0]
    assert ref == "reply"
    assert message.Event.AliceResponse.Header.RequestId == "req-1"  # correlation ID из input
    assert message.Event.AliceResponse.Response.Cards[0].Text == "Сейчас 12 часов."
    assert "Сейчас 0 часов." != message.Event.AliceResponse.Response.Cards[0].Text


async def test_missing_dependency_errors_terminal_with_reason():
    executor = Executor()
    flow = scenario([step("a", "action", ref="ghost.action", arguments={}),
                     step("done", "finish")])
    outcome = await executor.execute(flow, CTX, "dlg-2")
    assert outcome.status == "errored" and outcome.error == "missing_action:ghost.action"
    assert outcome.trace[-1]["status"] == "error"


async def test_action_failure_branch_and_terminal_error():
    def boom(arguments, execution):
        raise RuntimeError("device offline")

    executor = Executor(actions={"x.y": boom})
    with_branch = scenario([
        step("a", "action", ref="x.y", arguments={}, next="ok", on_failure="fail"),
        step("ok", "finish"),
        step("fail", "error", message="не удалось"),
    ])
    outcome = await executor.execute(with_branch, CTX, "dlg-3")
    assert outcome.status == "errored" and outcome.error == "не удалось"
    assert [entry["status"] for entry in outcome.trace] == ["failed", "terminal"]

    no_branch = scenario([step("a", "action", ref="x.y", arguments={}), step("done", "finish")])
    outcome = await executor.execute(no_branch, CTX, "dlg-4")
    assert outcome.status == "errored" and "action_failed" in outcome.error


async def test_action_idempotency_single_side_effect():
    executions_count = []

    async def once(arguments, execution):
        executions_count.append(1)
        return "done"

    executor = Executor(actions={"light.on": once})
    flow = scenario([
        step("a1", "action", ref="light.on", arguments={"room": "kitchen"}),
        step("a2", "action", ref="light.on", arguments={"room": "kitchen"}),  # тот же Action ID
        step("done", "finish"),
    ])
    outcome = await executor.execute(flow, CTX, "dlg-5")
    assert outcome.status == "finished"
    assert len(executions_count) == 1  # одноразовый side effect
    assert outcome.trace[1]["reason"].startswith("idempotent:")


async def test_step_limit_terminates_loops():
    executor = Executor(actions={"x.noop": lambda arguments, execution: True})
    # валидный граф (достижимый терминал), но длиннее лимита шагов
    long_chain = scenario([step(f"s{i}", "action", ref="x.noop", arguments={"i": i}) for i in range(MAX_STEPS + 5)]
                          + [step("done", "finish")])
    outcome = await executor.execute(long_chain, CTX, "dlg-6")
    assert outcome.status == "errored" and outcome.error == "step_limit"
    assert len(outcome.trace) <= MAX_STEPS + 1


# --- ask / ответ / отмена / рестарт ---------------------------------------------


async def test_ask_awaits_then_answer_resumes_to_finish(tmp_path):
    executor = Executor(store=StateStore(tmp_path / "emulator.sqlite3"))
    flow = scenario([
        step("q", "ask", question="Сколько минут?", **{"await": "minutes", "next": "done"}),
        step("done", "finish"),
    ], slots={"minutes": {"type": "integer"}})
    outcome = await executor.execute(flow, CTX, "dlg-ask")
    assert outcome.status == "awaiting"
    assert outcome.awaiting == "minutes" and outcome.question == "Сколько минут?"
    assert executor.store.load_execution("dlg-ask") is not None  # персистентно
    resumed = await executor.answer("dlg-ask", 5, CTX)
    assert resumed.status == "finished"
    assert resumed.slots["minutes"] == 5
    assert executor.store.load_execution("dlg-ask") is None  # снято с хранения


async def test_cancel_drops_awaiting_dialog(tmp_path):
    store = StateStore(tmp_path / "emulator.sqlite3")
    executor = Executor(store=store)
    flow = scenario([
        step("q", "ask", question="Да или нет?", **{"await": "confirm", "next": "done"}),
        step("done", "finish"),
    ], slots={"confirm": {"type": "bool"}})
    outcome = await executor.execute(flow, CTX, "dlg-cancel")
    assert outcome.status == "awaiting"
    cancelled = executor.cancel("dlg-cancel")
    assert cancelled.status == "cancelled"
    assert store.load_execution("dlg-cancel") is None
    assert await executor.answer("dlg-cancel", True, CTX) is None  # продолжать некому


async def test_restart_during_await_resumes_from_store(tmp_path):
    store = StateStore(tmp_path / "emulator.sqlite3")
    first = Executor(store=store)
    flow = scenario([
        step("q", "ask", question="Погода в каком городе?", **{"await": "city", "next": "done"}),
        step("done", "finish"),
    ], slots={"city": {"type": "string"}})
    outcome = await first.execute(flow, CTX, "dlg-restart")
    assert outcome.status == "awaiting"
    store.close()

    store2 = StateStore(tmp_path / "emulator.sqlite3")  # «рестарт» процесса
    second = Executor(store=store2)
    assert any(p["dialog_id"] == "dlg-restart" for p in second.pending())
    resumed = await second.answer("dlg-restart", "Москва", CTX)
    assert resumed.status == "finished" and resumed.slots["city"] == "Москва"


# --- роутер ----------------------------------------------------------------------


def test_router_selects_by_event_intent_and_phrase_with_reasons():
    scenarios = [
        validate_scenario({"format_version": 2, "id": "a.volume", "version": "1.0.0",
                           "triggers": [{"kind": "intent", "name": "volume.set"}],
                           "flow": [{"id": "done", "op": "finish"}]}),
        validate_scenario({"format_version": 2, "id": "b.phrase", "version": "1.0.0",
                           "triggers": [{"kind": "phrase", "phrases": ["погода"]}],
                           "flow": [{"id": "done", "op": "finish"}]}),
    ]
    picked, reason = select(scenarios, event_name="volume.set")
    assert picked["id"] == "a.volume" and reason == "intent:volume.set"
    picked, reason = select(scenarios, text="а погода какая?")
    assert picked["id"] == "b.phrase" and reason == "phrase:contains"
    picked, reason = select(scenarios, text="погода")
    assert reason == "phrase:exact"
    picked, reason = select(scenarios, event_name="unknown", text="не то")
    assert picked is None and reason == "no_match"


def test_execution_snapshot_roundtrip_keeps_position_and_version():
    flow = scenario([step("q", "ask", question="?", **{"await": "x", "next": "done"}),
                     step("done", "finish")], slots={"x": {"type": "string"}})
    execution = Execution("dlg-s", flow)
    execution.position = 1
    execution.slots["x"] = "значение"
    execution.state["k"] = 1
    snapshot = execution.snapshot()
    restored = Execution.restore(snapshot)
    assert restored.position == 1 and restored.slots == {"x": "значение"}
    assert restored.scenario["id"] == flow["id"]  # версия определения закреплена снимком


# --- compose.reply из JSON (кодовых композеров больше нет) ---------------------


async def test_compose_reply_builds_card_from_json():
    """Декларативный compose.reply: текст-шаблон раскрывается из slots,
    карточка собирается по схеме alice-api без исполнения кода."""
    executor = Executor(schemas_root=pathlib.Path("server_emulator/schemas"))
    flow = scenario([
        step("say", "compose", reply={"text": "Привет, {{slots.name}}"}),
        step("done", "finish"),
    ])
    outcome = await executor.execute(flow, {**CTX, "slots": {"name": "Мир"}}, "dlg-card")
    assert outcome.status == "finished"
    assert len(outcome.composed) == 1
    ref, message = outcome.composed[0]
    assert ref == "reply"
    assert message.Event.AliceResponse.Response.Cards[0].Text == "Привет, Мир"


async def test_json_ask_ttl_expires_and_frees_the_phrase(tmp_path):
    """TASK-097: истёкший JSON-ask не съедает реплику — ожидание снято, answer → None
    (вызывающий обрабатывает фразу как новую: сценарии → «Вместе» → модель)."""
    store = StateStore(tmp_path / "emulator.sqlite3")
    executor = Executor(store=store)
    flow = scenario([
        step("q", "ask", question="На какое время?", **{"await": "on_time", "next": "done"}),
        step("done", "finish"),
    ], slots={"on_time": {"type": "string"}})
    await executor.execute(flow, CTX, "dlg-ttl")
    payload = store.load_execution("dlg-ttl")
    payload["deadline_remaining_s"] = 0  # ожидание истекло (как зависший будильник в бою)
    store.save_execution("dlg-ttl", payload)
    assert await executor.answer("dlg-ttl", "расскажи новости", CTX) is None
    assert store.load_execution("dlg-ttl") is None  # снято с хранения


async def test_json_ask_cancel_phrase_ends_wait(tmp_path):
    """TASK-097: «отмена»/«стоп» снимают JSON-ask — как у py-сценариев (TASK-020)."""
    store = StateStore(tmp_path / "emulator.sqlite3")
    executor = Executor(store=store)
    flow = scenario([
        step("q", "ask", question="На какое время?", **{"await": "on_time", "next": "done"}),
        step("done", "finish"),
    ], slots={"on_time": {"type": "string"}})
    await executor.execute(flow, CTX, "dlg-cancel-2")
    outcome = await executor.answer("dlg-cancel-2", "отмена", CTX)
    assert outcome.status == "cancelled"
    assert store.load_execution("dlg-cancel-2") is None
