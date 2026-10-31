"""R07: dialog/transport — continuation раньше роутера, correlation IDs, доставка."""
import datetime

import pytest

from server_emulator.scenario_engine.dialog import DialogManager
from server_emulator.scenario_engine.executor import Executor
from server_emulator.scenario_packages.validate import validate_scenario
from server_emulator.schema import load_type
from server_emulator.state_store import StateStore
from tests.conftest import CTX, step

SCHEMAS = "server_emulator/schemas"


def server_type():
    return load_type(SCHEMAS, "alice-api.pb", "NAlice.NAliceApi.TServerMessage")


def ask_scenario():
    return validate_scenario({
        "format_version": 2, "id": "test.ask", "version": "1.0.0",
        "triggers": [{"kind": "phrase", "name": "поставь таймер"}],
        "slots": {"minutes": {"type": "integer"}},
        "flow": [{"id": "q", "op": "ask", "question": "На сколько минут?",
                  **{"await": "minutes", "next": "done"}},
                 {"id": "done", "op": "finish"}]})


def decode(sent, index):
    return server_type().FromString(sent[index][4:])


async def test_phrase_route_delivers_question_with_correlation(tmp_path):
    sent = []

    async def send(data):
        sent.append(data)
    executor = Executor(store=StateStore(tmp_path / "emulator.sqlite3"))
    dialog = DialogManager(executor, lambda: [ask_scenario()], server_type())
    ctx = dict(CTX, input={"message_id": "msg-42", "request_id": "req-42", "dialog_id": "dlg-1"})
    # «ну …» остаётся в нормализованной реплике — матчинг по вхождению
    report = await dialog.handle_text("ну поставь таймер", ctx, "dlg-1", send)
    assert report["handled"] and report["status"] == "awaiting" and report["reason"] == "phrase:contains"
    message = decode(sent, 0)
    assert message.Event.Header.RefMessageId == "msg-42"  # correlation с входом
    assert message.Event.Header.MessageId
    assert message.Event.AliceResponse.Header.RequestId == "req-42"
    assert message.Event.AliceResponse.Response.Cards[0].Text == "На сколько минут?"


async def test_continuation_wins_over_new_trigger(tmp_path):
    sent = []

    async def send(data):
        sent.append(data)
    store = StateStore(tmp_path / "emulator.sqlite3")
    executor = Executor(store=store)
    dialog = DialogManager(executor, lambda: [ask_scenario()], server_type())
    ctx = dict(CTX, input={"message_id": "m1", "request_id": "r1", "dialog_id": "dlg-1"})
    await dialog.handle_text("поставь таймер", ctx, "dlg-1", send)
    # повторный «новый» триггер в ожидающем диалоге = ответ на уточнение, не новый сценарий
    report = await dialog.handle_text("поставь таймер", ctx, "dlg-1", send)
    assert report["reason"] == "continuation" and report["status"] == "finished"
    assert store.load_execution("dlg-1") is None


async def test_no_match_reported_for_fallback_caller(tmp_path):
    async def send(data):
        raise AssertionError("ничего не должно отправляться")
    dialog = DialogManager(Executor(store=StateStore(tmp_path / "e.sqlite3")),
                           lambda: [ask_scenario()], server_type())
    report = await dialog.handle_text("про погоду", CTX, "dlg-2", send)
    assert report["handled"] is False and report["verdict"] == "unsupported" and report["reason"] == "no_match"


async def test_protocol_event_route(tmp_path):
    sent = []

    async def send(data):
        sent.append(data)
    scenario = validate_scenario({
        "format_version": 2, "id": "evt.sync", "version": "1.0.0",
        "triggers": [{"kind": "protocol_event", "name": "SynchronizeStateRequest"}],
        "flow": [{"id": "done", "op": "finish"}]})
    dialog = DialogManager(Executor(store=StateStore(tmp_path / "e.sqlite3")),
                           lambda: [scenario], server_type())
    report = await dialog.handle_event("SynchronizeStateRequest", CTX, "dlg-3", send)
    assert report["handled"] and report["reason"] == "event:SynchronizeStateRequest"  # protocol_event — тот же event-матчер
    assert sent == []  # finish без compose — доставки нет, отчёт есть


async def test_dialog_restart_continues_awaiting(tmp_path):
    store = StateStore(tmp_path / "e.sqlite3")
    first = DialogManager(Executor(store=store), lambda: [ask_scenario()], server_type())

    async def send(data):
        pass
    await first.handle_text("поставь таймер", CTX, "dlg-r", send)
    store.close()
    store2 = StateStore(tmp_path / "e.sqlite3")
    second = DialogManager(Executor(store=store2), lambda: [ask_scenario()], server_type())
    sent2 = []

    async def send2(data):
        sent2.append(data)
    report = await second.handle_text("на пять минут", CTX, "dlg-r", send2)
    assert report["reason"] == "continuation" and report["status"] == "finished"
