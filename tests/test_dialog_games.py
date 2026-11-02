"""TASK-082: диалоговые игры в «Модели/AI агенте» — игру ведёт модель.

Начало игры и типичные ходы игрока не должны перехватываться сценариями в «Модели»/«Агенте».
Замер (таблица в «Итоге» задания): перехватывают только «хватит» и «стоп» — device.player.pause
(штатная функция остановки плеера/сигнала, не ошибка шаблона) — БЛОКЕР на флаг «идёт игра».
"""
import datetime

import pytest

from server_emulator.runtime import Runtime

STARTS = ["Алиса, давай сыграем в города", "Алиса, давай сыграем в верю не верю",
          "Алиса, давай сыграем в загадки"]
# ходы, которые обязаны дойти до модели (замер 2026-11-03: перехватов нет)
FREE_MOVES = ["да", "нет", "верю", "не верю", "Москва", "сдаюсь", "ещё", "первое", "второе",
              "два", "загадывай", "дальше", "новый вопрос", "не знаю", "конец игры"]
# ходы, перехваченные штатными сценариями плеера (БЛОКЕР 082 — таблица в «Итоге»)
PLAYER_MOVES = {"хватит": "device.player.pause", "стоп": "device.player.pause"}


def _ctx(runtime, text):
    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg", "sn": "", "text": text},
           "clock": {"now": datetime.datetime.now()}}
    ctx.update(runtime._scenario_context(sn=""))
    return ctx


async def _report(runtime, text):
    runtime.settings["intelligence"] = "model"
    sent = []

    async def send(data):
        sent.append(data)

    return await runtime.dialog.handle_text(text, _ctx(runtime, text), "dg", send)


@pytest.mark.parametrize("phrase", STARTS)
async def test_game_start_reaches_model(tmp_path, phrase):
    seen = {}

    async def chat(text, **kwargs):
        seen["text"] = text
        return {"text": "Начнём!", "tool_calls": [], "usage": {}, "source": "model"}

    runtime = Runtime(tmp_path)
    try:
        runtime.chat = chat
        report = await _report(runtime, phrase)
        # 090 уступает в «Модели»: фраза не обработана сценарием — тракт вызовет модель
        assert not report["handled"] and report["reason"].startswith("script_declined"), report
        answer = await runtime.chat(phrase, history=[], thread="game", sn="")
        assert seen.get("text") == phrase and answer["source"] == "model"
    finally:
        await runtime.close()


@pytest.mark.parametrize("move", FREE_MOVES)
async def test_game_moves_reach_model(tmp_path, move):
    runtime = Runtime(tmp_path)
    try:
        report = await _report(runtime, move)
        assert not report["handled"], (move, report.get("scenario"))
    finally:
        await runtime.close()


@pytest.mark.parametrize("move,scenario", sorted(PLAYER_MOVES.items()))
async def test_player_scenarios_still_own_their_phrases(tmp_path, move, scenario):
    """Штатные фразы плеера не сужались: перехват задокументирован БЛОКЕРОМ в задании."""
    runtime = Runtime(tmp_path)
    try:
        report = await _report(runtime, move)
        assert report["handled"] and report["scenario"] == scenario
    finally:
        await runtime.close()
