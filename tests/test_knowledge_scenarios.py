"""Знания: сценарий → поставщик вида knowledge (вкладка «Поставщики данных»).

Подменный поставщик отдаёт значение — сценарий отвечает по шаблону во всех трёх режимах
«Интеллекта»; поставщика нет — «Для этого нет источника данных.» в «Простом» и отказ
модели в «Модели»; поставщик вернул устаревшую отметку времени — ответ с пометкой о
времени данных; сбой поставщика и supports(domain)=False — честный отказ; фразы других
сценариев сюда не попадают.
"""
import datetime
import time

import pytest

from server_emulator import providers
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


def _provider(record=None, supports=None):
    """Подменный поставщик вида knowledge в реестр — штатный путь сценария (selected)."""
    entry = {"id": "stub", "kind": "knowledge", "ext_id": "test-stub", "title": "Заглушка"}
    if record is not None:
        entry["record"] = record
    if supports is not None:
        entry["supports"] = supports
    providers.PROVIDERS.setdefault("knowledge", {})["stub"] = entry


def _record(value, updated_at=None):
    async def record(context, domain, key, args):
        if updated_at is None:
            return value
        return {"value": value, "updated_at": updated_at}

    return record


async def test_knowledge_with_provider_all_modes(tmp_path):
    """Домен tv («скажи программу на вечер») — шаблонный ответ по значению поставщика
    во всех трёх режимах «Интеллекта»."""
    value = {"items": [{"channel": "Первый", "title": "Вечер", "start": "21:00"}]}
    for mode in ("scenarios", "model", "agent"):
        runtime = Runtime(tmp_path)
        _provider(record=_record(value))
        try:
            report = await _report(runtime, "Алиса, скажи программу на вечер", intelligence=mode)
            assert report["scenario"] == "knowledge.knowledge", report
            assert "Первый" in report["speech"] and "21:00" in report["speech"]
        finally:
            await runtime.close()


async def test_no_provider_simple_honest(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        report = await _report(runtime, "Алиса, какой курс доллара")
        assert report["scenario"] == "knowledge.knowledge"
        assert report["speech"] == "Для этого нет источника данных."
    finally:
        await runtime.close()


async def test_no_provider_model_yields(tmp_path):
    seen = {}

    async def chat(text, **kwargs):
        seen["text"] = text
        return {"text": "модель", "tool_calls": [], "usage": {}, "source": "model"}

    runtime = Runtime(tmp_path)
    try:
        report = await _report(runtime, "Алиса, какой курс доллара", intelligence="model", chat=chat)
        assert not report["handled"] and report["reason"] == "script_declined:knowledge.knowledge"
        await runtime.chat("какой курс доллара", history=[], thread="", sn="")
        assert "доллара" in seen.get("text", "")
    finally:
        await runtime.close()


async def test_phrases_from_builtin_plugin(tmp_path):
    """Фразы знаний объявляет встроенный плагин knowledge: совпадение идёт из справочника
    (reason dir:…), а не из триггеров сценария."""
    runtime = Runtime(tmp_path)
    try:
        report = await _report(runtime, "Алиса, какой курс доллара")
        assert report["scenario"] == "knowledge.knowledge"
        assert report["reason"] == "dir:builtin.knowledge.knowledge", report["reason"]
    finally:
        await runtime.close()


async def test_phrases_unknown_when_plugin_off(tmp_path):
    """Выключенный плагин — фраз знаний нет в справочнике: фраза не отвечает сценарием
    знаний (сценарий в каталоге остаётся, выбрать его нечему)."""
    runtime = Runtime(tmp_path)
    try:
        await runtime.extensions.disable("builtin.knowledge")
        directory = runtime.dialog.phrase_directory()
        assert not [e for e in directory.entries.values()
                    if directory.handler_scenario(e) == "knowledge.knowledge"]
        for phrase in ("Алиса, какой курс доллара", "Алиса, где рядом аптека",
                       "Алиса, найди рецепт блинов"):
            report = await _report(runtime, phrase)
            assert not report["handled"] or report["scenario"] != "knowledge.knowledge", (phrase, report)
    finally:
        await runtime.close()


async def test_stale_value_notes_age(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        value = {"unit": "USD", "value": 81.4}
        _provider(record=_record(value))  # значение без отметки — свежее
        report = await _report(runtime, "Алиса, какой курс доллара")
        assert "USD" in report["speech"] and "назад" not in report["speech"]
        providers.PROVIDERS["knowledge"].clear()
        # отметка двухчасовой давности (max_age prices — 600): ответ с пометкой о времени данных
        _provider(record=_record(value, updated_at=time.time() - 7200))
        report = await _report(runtime, "Алиса, какой курс доллара")
        assert "USD" in report["speech"] and "назад" in report["speech"], report["speech"]
    finally:
        await runtime.close()


async def test_provider_failure_honest(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        async def record(context, domain, key, args):
            raise RuntimeError("нет связи")

        _provider(record=record)
        report = await _report(runtime, "Алиса, какой курс доллара")
        assert report["speech"] == "Для этого нет источника данных.", report["speech"]
    finally:
        await runtime.close()


async def test_supports_false_honest(tmp_path):
    """Поставщик, не закрывающий домен (supports=False), — честный отказ, record не зовётся."""
    asked = []

    runtime = Runtime(tmp_path)
    try:
        async def supports(context, domain):
            asked.append(domain)
            return False

        async def record(context, domain, key, args):
            raise AssertionError("record не вызывается при supports=False")

        _provider(record=record, supports=supports)
        report = await _report(runtime, "Алиса, какой курс доллара")
        assert report["speech"] == "Для этого нет источника данных.", report["speech"]
        assert asked == ["prices"], asked
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
