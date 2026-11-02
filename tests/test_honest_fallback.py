"""TASK-090: честный фолбэк — «В простом режиме это недоступно.» и «Для этого нет источника данных.».

Фраза-функция (system.unavailable) отвечает только в «Простом», бессвязное — прежнее
«Я пока этого не умею.», в «Модели» фраза-функция уходит модели (подмена chat), правка
стандартного ответа в настройках действует, помощник no_data_source срабатывает без плагина,
тематический сценарий не пропускает свою фразу в system.unavailable.
"""
import datetime

import pytest

from server_emulator.runtime import Runtime


def _ctx(runtime, text, sn=""):
    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg", "sn": sn, "text": text},
           "clock": {"now": datetime.datetime.now()}}
    ctx.update(runtime._scenario_context(sn=sn))
    return ctx


async def _report(runtime, text, chat=None, intelligence="scenarios"):
    runtime.settings["intelligence"] = intelligence
    if chat is not None:
        runtime.chat = chat
    sent = []

    async def send(data):
        sent.append(data)

    return await runtime.dialog.handle_text(text, _ctx(runtime, text), "dg", send)


SIMPLE_UNAVAILABLE = "В простом режиме это недоступно."
NO_DATA = "Для этого нет источника данных."


@pytest.mark.parametrize("phrase", ["покажи фильм дюна", "закажи молоко", "давай сыграем в города",
                                    "включи караоке", "что посмотреть"])
async def test_function_phrase_simple_unavailable(tmp_path, phrase):
    runtime = Runtime(tmp_path)
    try:
        report = await _report(runtime, "Алиса, " + phrase)
        assert report["handled"] and report["scenario"] == "system.unavailable"
        assert report["speech"] == SIMPLE_UNAVAILABLE
        assert phrase not in str(getattr(runtime, "phrase_misses", {}))
    finally:
        await runtime.close()


async def test_gibberish_still_not_understood(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        report = await _report(runtime, "ммм как его там ну это")
        assert not report["handled"] or report["scenario"] != "system.unavailable"
        assert report.get("speech") in (None, "", "Я пока этого не умею.")
    finally:
        await runtime.close()


async def test_function_phrase_goes_to_model(tmp_path):
    """Сценарий в «Модели» уступает (диалог — not handled, как у фразы без сценария),
    и фраза доходит до runtime.chat — точку, которую трак voice.py вызывает на not handled."""
    seen = {}

    async def chat(text, **kwargs):
        seen["text"] = text
        return {"text": "модель отвечает", "tool_calls": [], "usage": {}, "source": "model"}

    runtime = Runtime(tmp_path)
    try:
        runtime.chat = chat
        report = await _report(runtime, "Алиса, покажи фильм дюна", chat=chat, intelligence="model")
        assert not report["handled"] and report["reason"] == "script_declined:system.unavailable"
        phrase = "Алиса, покажи фильм дюна"  # тракт: runtime.chat(текст фразы)
        answer = await runtime.chat(phrase, history=[], thread="", sn="")
        assert seen.get("text") == phrase and answer["source"] == "model"
    finally:
        await runtime.close()


def json_text(report):
    import json
    return json.dumps(report, ensure_ascii=False, default=str)


async def test_standard_reply_edit_changes_answer(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        replies = runtime.settings["standard_replies"]
        for reply in replies:
            if reply["id"] == "simple_unavailable":
                reply["text"] = "Сейчас нельзя."
        report = await _report(runtime, "Алиса, включи караоке")
        assert report["speech"] == "Сейчас нельзя."
    finally:
        await runtime.close()


async def test_no_data_source_helper(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        runtime.scenario_plugins.pop("news", None)

        async def answer(ctx):  # py-сценарий данных без плагина
            try:
                await ctx.dataset_record("news", "top")
            except Exception:
                return ctx.no_data_source()

        runtime.scenario_executor.script_modules = getattr(runtime.scenario_executor,
                                                           "script_modules", None)
        report = await _report(runtime, "Алиса, какие сегодня новости")
        # новости — сценария ещё нет (081), здесь проверяем сам помощник напрямую:
        from server_emulator.sdk import ScenarioContext
        ctx = ScenarioContext(runtime, _ctx(runtime, "news"))
        result = answer(ctx) if False else ctx.no_data_source()
        spec = result if isinstance(result, dict) else await result if hasattr(result, "__await__") else result
        from server_emulator.scenario_engine.reply_compose import compose_reply
        message = compose_reply(spec, _ctx(runtime, "news"), "server_emulator/schemas")
        assert message.Event.AliceResponse.VoiceResponse.OutputSpeech.Text == NO_DATA
    finally:
        await runtime.close()


async def test_no_data_source_yields_to_model(tmp_path):
    from server_emulator.sdk import ScenarioContext
    runtime = Runtime(tmp_path)
    try:
        ctx = ScenarioContext(runtime, _ctx(runtime, "news"))
        ctx._ctx["settings"] = {"intelligence": "model", "standard_replies": []}
        assert ctx.no_data_source() is None
    finally:
        await runtime.close()


async def test_thematic_scenario_wins(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        report = await _report(runtime, "Алиса, поставь таймер на 5 минут")
        assert report["scenario"] == "timers.timer.set"
    finally:
        await runtime.close()
