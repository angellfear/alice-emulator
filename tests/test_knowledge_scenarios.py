"""TASK-081: знания по схеме «сценарий → база → плагин».

Подменный плагин датасета отдаёт значение — сценарий отвечает по шаблону во всех трёх режимах;
плагина нет — «Для этого нет источника данных.» в «Простом» и отказ модели в «Модели»;
устаревшая запись — ответ с пометкой о времени данных; фразы других сценариев сюда не попадают.
"""
import datetime

import pytest

from server_emulator.runtime import Runtime


def _ctx(runtime, text, sn=""):
    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg", "sn": sn, "text": text},
           "clock": {"now": datetime.datetime.now()}}
    ctx.update(runtime._scenario_context(sn=sn))
    return ctx


async def _report(runtime, text, intelligence="scenarios", chat=None):
    runtime.settings["intelligence"] = intelligence
    if chat is not None:
        runtime.chat = chat
    sent = []

    async def send(data):
        sent.append(data)

    return await runtime.dialog.handle_text(text, _ctx(runtime, text), "dg", send)


def _plugin(runtime, name, value, age_s=0):
    async def fn(key, args=None):
        return value

    runtime.scenario_executor.plugins[name] = fn


async def test_news_with_plugin_all_modes(tmp_path):
    value = {"items": [{"title": "Событие", "source": "Лента"}]}
    for mode in ("scenarios", "model", "agent"):
        runtime = Runtime(tmp_path)
        _plugin(runtime, "news", value)
        try:
            report = await _report(runtime, "Алиса, расскажи новости", intelligence=mode)
            assert report["scenario"] == "knowledge.knowledge", report
            assert "Событие" in report["speech"] and "Лента" in report["speech"]
        finally:
            await runtime.close()


async def test_no_plugin_simple_honest(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        report = await _report(runtime, "Алиса, расскажи новости")
        assert report["scenario"] == "knowledge.knowledge"
        assert report["speech"] == "Для этого нет источника данных."
    finally:
        await runtime.close()


async def test_no_plugin_model_yields(tmp_path):
    seen = {}

    async def chat(text, **kwargs):
        seen["text"] = text
        return {"text": "модель", "tool_calls": [], "usage": {}, "source": "model"}

    runtime = Runtime(tmp_path)
    try:
        report = await _report(runtime, "Алиса, расскажи новости", intelligence="model", chat=chat)
        assert not report["handled"] and report["reason"] == "script_declined:knowledge.knowledge"
        await runtime.chat("расскажи новости", history=[], thread="", sn="")
        assert "новости" in seen.get("text", "")
    finally:
        await runtime.close()


async def test_stale_record_notes_age(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        value = {"items": [{"title": "Событие", "source": "Лента"}]}

        async def plugin(key, args=None):
            return value

        runtime.scenario_executor.plugins["news"] = plugin
        report = await _report(runtime, "Алиса, расскажи новости")
        assert "Событие" in report["speech"] and "назад" not in report["speech"]
        # запись двухчасовой давности (max_age news — 600): ответ с пометкой о времени данных
        store = runtime.scenario_executor.data_store
        db = store._live()
        with db:
            db.execute("UPDATE scenario_data SET updated_ts = updated_ts - 7200")
        # устаревшая запись возвращается, когда плагин обновить не смог (нет связи)

        async def broken(key, args=None):
            raise RuntimeError("нет связи")

        runtime.scenario_executor.plugins["news"] = broken
        report = await _report(runtime, "Алиса, расскажи новости")
        assert "Событие" in report["speech"] and "назад" in report["speech"], report["speech"]
    finally:
        await runtime.close()


@pytest.mark.parametrize("phrase,scenario", [("Алиса, поставь таймер на 5 минут", "timers.timer.set"),
                                             ("Алиса, который час", "informers.clock")])
async def test_other_scenarios_not_captured(tmp_path, phrase, scenario):
    runtime = Runtime(tmp_path)
    try:
        report = await _report(runtime, phrase)
        assert report["scenario"] == scenario
    finally:
        await runtime.close()
