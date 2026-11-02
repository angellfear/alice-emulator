"""TASK-080: болталка, дата/время и простые игры — узкие триггеры, свои формулировки.

Дата/день недели/город сверяются с независимым расчётом в тесте (zoneinfo), игры —
на множество допустимых ответов, «повтори за мной» — на эхо фразы. Модель в этих
фразах не участвует: runtime.chat подменён на AssertionError.
"""
import datetime
import re
from zoneinfo import ZoneInfo

import pytest

from server_emulator.runtime import Runtime
from server_emulator.sdk import ScenarioContext

MONTHS_GEN = {1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
              7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"}
WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _ctx(runtime, text, sn=""):
    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg", "sn": sn, "text": text},
           "clock": {"now": datetime.datetime.now()}}
    ctx.update(runtime._scenario_context(sn=sn))
    return ctx


async def _say(runtime, text, sn=""):
    async def send(data):
        pass

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    return await runtime.dialog.handle_text(text, _ctx(runtime, text, sn), "dg", send)


async def _reply(runtime, phrase):
    report = await _say(runtime, phrase)
    assert report["handled"], f"{phrase!r}: {report.get('reason')}"
    return report


async def test_date_number_and_weekday_today(tmp_path):
    runtime = Runtime(tmp_path)
    now = ScenarioContext(runtime, _ctx(runtime, "x")).now  # тот же путь «сейчас», что у сценария
    try:
        report = await _reply(runtime, "Алиса, какое сегодня число")
        assert report["speech"] == f"Сегодня {now.day} {MONTHS_GEN[now.month]}."
        report = await _reply(runtime, "Алиса, какой сегодня день недели")
        assert report["speech"] == f"Сегодня {WEEKDAYS_RU[now.weekday()]}."
    finally:
        await runtime.close()


def _next_month_day(day, month, today):
    candidate = today.replace(month=month, day=day)
    if candidate < today:
        candidate = candidate.replace(year=candidate.year + 1)
    return candidate


async def test_weekday_of_month_day(tmp_path):
    today = datetime.datetime.now(datetime.timezone.utc).date()
    date = _next_month_day(8, 3, today)
    runtime = Runtime(tmp_path)
    try:
        report = await _reply(runtime, "Алиса, в какой день недели будет 8 марта")
        assert report["speech"] == f"8 марта {date.year} года будет {WEEKDAYS_RU[date.weekday()]}."
    finally:
        await runtime.close()


async def test_weekday_number(tmp_path):
    runtime = Runtime(tmp_path)
    today = ScenarioContext(runtime, _ctx(runtime, "x")).now.date()  # тот же путь «сейчас»
    shift = (0 - today.weekday()) % 7  # ближайший понедельник, сегодня тоже считается
    date = today + datetime.timedelta(days=shift)
    try:
        report = await _reply(runtime, "Алиса, понедельник — это какое число")
        assert report["speech"] == f"В понедельник {date.day} {MONTHS_GEN[date.month]}."
    finally:
        await runtime.close()


async def test_city_time(tmp_path):
    local = datetime.datetime.now(ZoneInfo("Asia/Yekaterinburg"))
    runtime = Runtime(tmp_path)
    try:
        report = await _reply(runtime, "Алиса, точное время в Екатеринбурге")
        pattern = (r"^Сейчас в Екатеринбурге \d{2}:\d{2}, "
                   rf"{WEEKDAYS_RU[local.weekday()]}, \d+ {MONTHS_GEN[local.month]}\.$")
        assert re.match(pattern, report["speech"]), report["speech"]
    finally:
        await runtime.close()


async def test_city_unknown_time_zone(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        report = await _reply(runtime, "Алиса, точное время в Урюпинске")
        assert report["speech"] == "Не знаю часовой пояс города Урюпинске."
        assert report["scenario"] == "informers.clock.date"
    finally:
        await runtime.close()


async def test_repeat_after_me(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        report = await _reply(runtime, "Алиса, повтори за мной синий трактор")
        assert report["speech"] == "синий трактор."
        report = await _reply(runtime, "повтори за мной Привет, мир!")
        assert report["speech"] == "Привет, мир."
    finally:
        await runtime.close()


async def test_repeat_without_phrase_asks_back(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        report = await _reply(runtime, "Алиса, повтори за мной")
        assert "повторить" in report["speech"].lower()
    finally:
        await runtime.close()


@pytest.mark.parametrize("phrase", ["как дела", "кто тебя создал", "что ты умеешь",
                                    "расскажи скороговорку", "скажи тост", "расскажи кричалку",
                                    "расскажи свое стихотворение"])
async def test_small_talk_answers(tmp_path, phrase):
    runtime = Runtime(tmp_path)
    try:
        report = await _reply(runtime, "Алиса, " + phrase)
        assert report["scenario"] == "chat.chat"
        assert report["speech"]
    finally:
        await runtime.close()


async def test_singing_honest_refusal(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        report = await _reply(runtime, "Алиса, спой что-нибудь")
        assert "не умею" in report["speech"].lower() and "музык" in report["speech"].lower()
    finally:
        await runtime.close()


@pytest.mark.parametrize("phrase", ["подбрось монетку", "брось монетку"])
async def test_coin(tmp_path, phrase):
    runtime = Runtime(tmp_path)
    try:
        report = await _reply(runtime, "Алиса, " + phrase)
        assert report["speech"] in ("Орёл.", "Решка.")
    finally:
        await runtime.close()


async def test_dice(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        report = await _reply(runtime, "Алиса, брось кубик")
        match = re.match(r"^На кубике выпало ([1-6])\.$", report["speech"])
        assert match, report["speech"]
    finally:
        await runtime.close()


async def test_fortune_variants(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        first = (await _reply(runtime, "Алиса, шар судьбы"))["speech"]
        second = (await _reply(runtime, "Алиса, давай погадаем"))["speech"]
        assert first and second
    finally:
        await runtime.close()


async def test_no_steal_from_other_domains(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        report = await _say(runtime, "Алиса, включи тостер")
        assert not report["handled"] or report["scenario"] != "chat.chat"
    finally:
        await runtime.close()


# --- список покупок (TASK-080): состояние по пользователю, переживает перезапуск ---

async def _shopping(runtime, phrase):
    report = await _say(runtime, phrase)
    assert report["handled"], f"{phrase!r}: {report.get('reason')}"
    assert report["scenario"] == "lists.shopping", report["scenario"]
    return report["speech"]


async def test_shopping_add_show_remove_clear(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        assert await _shopping(runtime, "Алиса, добавь в список покупок молоко") == \
            "Добавила молоко в ваш список покупок."
        assert await _shopping(runtime, "Алиса, добавь в список покупок хлеб и яйца") == \
            "Добавила хлеб и яйца в ваш список покупок."
        speech = await _shopping(runtime, "Алиса, что в списке покупок")
        assert speech == ("В вашем списке покупок сейчас лежит 3 товара: "
                          "1) молоко, 2) хлеб, 3) яйца."), speech
        assert await _shopping(runtime, "Алиса, удали из списка покупок хлеб") == \
            "Убрала «хлеб» из списка покупок."
        assert await _shopping(runtime, "Алиса, удали из списка покупок чай") == \
            "В списке покупок нет «чай»."
        assert await _shopping(runtime, "Алиса, очисти список покупок") == \
            "Ваш список покупок теперь пуст."
        assert await _shopping(runtime, "Алиса, что в списке покупок") == "Ваш список покупок пуст."
    finally:
        await runtime.close()


async def test_shopping_survives_restart(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        await _shopping(runtime, "Алиса, добавь в список покупок йогурт хлеб кефир и шампунь")
        speech = await _shopping(runtime, "Алиса, покажи список покупок")
        assert "4 товара" in speech and "шампунь" in speech, speech
    finally:
        await runtime.close()
    runtime = Runtime(tmp_path)  # новый экземпляр на той же папке состояния
    try:
        speech = await _shopping(runtime, "Алиса, покажи список покупок")
        assert "4 товара" in speech and "йогурт" in speech, speech
        assert await _shopping(runtime, "Алиса, добавь в список покупок йогурт") == \
            "Эти товары уже в списке покупок."
    finally:
        await runtime.close()
