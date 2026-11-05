"""TASK-020: многошаговый диалог py-сценария — return ask(...) из SDK.

Ожидание хранится ТОТ ЖЕ способ, что у JSON-шага ask (Execution.awaiting +
снимок в StateStore.executions: сценарий, версия файла, ctx.state, имя функции
продолжения, TTL) — переживает рестарт Runtime, видно в pending(), снимается
cancel(). Следующая фраза диалога минует роутер и попадает в on_duration(ctx,
answer) того же модуля; «отмена»/«стоп» и TTL завершают ожидание; вопрос
доставляется тем же question_message с ShouldListen (слушает без «Алиса»).
Выбор файла тестов: механика диалогового слоя — отдельный модуль
tests/test_scenario_dialog.py; tests/test_scenario_script.py (TASK-019) тестов
на ask не содержит, обновлять там было нечего.
"""
import datetime
import json

from server_emulator.runtime import Runtime


def script_scenario(sid="user.script.hello", phrases=("скрипт привет",), priority=100):
    return {"format_version": 2, "id": sid, "version": "1.0.0", "category": "custom",
            "priority": priority,
            "triggers": [{"kind": "phrase", "phrases": list(phrases)}],
            "executor": {"kind": "script"}}


def make_runtime(tmp_path, pairs):
    """Runtime со сценариями и кодом .py во временном state (файлы — источник правды)."""
    runtime = Runtime(tmp_path)
    for scenario, code in pairs:
        (runtime.scenario_dir / (scenario["id"] + ".json")).write_text(
            json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
        if code is not None:
            (runtime.scenario_dir / (scenario["id"] + ".py")).write_text(code, encoding="utf-8")
    runtime.settings_service.load_scenario_files(runtime.settings)
    return runtime


async def say_to(runtime, text, sn="SN-S"):
    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dlg", "sn": sn, "text": text},
           "clock": {"now": datetime.datetime.now()}}
    ctx.update(runtime._scenario_context(sn=sn))
    return await runtime.dialog.handle_text(text, ctx, "dlg", None, deliver=False)


def stub_timer(runtime, calls):
    """Заглушка timer.start — счётчик вызовов (как в tests/test_scenario_script.py)."""

    def timer_start(arguments, execution):
        calls.append(dict(arguments))
        return {"id": "t"}

    runtime.scenario_executor.actions["timer.start"] = timer_start


# --- сценарии-фикстуры --------------------------------------------------------------

ASK_TIMER_SCRIPT = '''from server_emulator.sdk import ask, say


async def handle(ctx):
    return ask("На сколько поставить таймер?", then="on_duration", expect="duration")


async def on_duration(ctx, answer):
    digits = "".join(ch for ch in answer if ch.isdigit())
    await ctx.action("timer.start", seconds=int(digits or 0))
    return say("Таймер заведён")
'''

BAD_THEN_SCRIPT = '''from server_emulator.sdk import ask


async def handle(ctx):
    return ask("На сколько?", then="nope")
'''

EXPECT_SCRIPT = '''from server_emulator.sdk import ask, say


async def handle(ctx):
    return ask("На сколько поставить таймер?", then="on_duration", expect="duration")


async def on_duration(ctx, answer):
    data = ctx.ask or {}
    return say(f"{answer}|{data.get('expect')}|{data.get('question')}")
'''

CHAIN_SCRIPT = '''from server_emulator.sdk import ask, say


async def handle(ctx):
    ctx.state["step"] = 1
    return ask("Первый вопрос?", then="on_first")


async def on_first(ctx, answer):
    ctx.state["first"] = answer
    return ask("Второй вопрос?", then="on_second")


async def on_second(ctx, answer):
    return say(f"{ctx.state['first']} и {answer}, шаг {ctx.state['step']}")
'''

SILENT_SCRIPT = '''from server_emulator.sdk import ask


async def handle(ctx):
    return ask("Подтвердите?", then="on_confirm")


async def on_confirm(ctx, answer):
    return None  # после ответа — тихое завершение, реплики нет
'''


# --- вопрос → ответ → продолжение ----------------------------------------------------


async def test_ask_awaits_and_answer_runs_continuation(tmp_path):
    runtime = make_runtime(tmp_path, [(script_scenario(), ASK_TIMER_SCRIPT)])
    calls = []
    stub_timer(runtime, calls)
    try:
        first = await say_to(runtime, "скрипт привет")
        assert first["handled"] and first["status"] == "awaiting"
        assert first["speech"] == "На сколько поставить таймер?"  # вопрос — тем же трактом
        assert {"step": "script", "op": "ask", "status": "await"} in first["trace"]
        second = await say_to(runtime, "на 300 секунд")
        assert second["reason"] == "continuation"  # минуя роутер — владелец диалога
        assert second["status"] == "finished" and second["speech"] == "Таймер заведён"
        assert {"step": "on_duration", "op": "ask", "status": "answered"} in second["trace"]
        assert calls == [{"seconds": 300}]  # продолжение получило ответ и завело таймер
        assert runtime.store.load_execution("dlg") is None  # ожидание снято и вычищено
    finally:
        await runtime.close()


async def test_question_message_sets_should_listen(tmp_path):
    runtime = make_runtime(tmp_path, [(script_scenario(), ASK_TIMER_SCRIPT)])
    try:
        report = await say_to(runtime, "скрипт привет")
        message = report["messages"][0]
        assert message.Event.AliceResponse.Response.Cards[0].Text == "На сколько поставить таймер?"
        # колонка слушает ответ без «Алиса» — тот же путь доставки вопроса, что JSON-ask
        assert message.Event.AliceResponse.VoiceResponse.ShouldListen is True
    finally:
        await runtime.close()


async def test_expect_reaches_continuation(tmp_path):
    # expect пока только доставляется продолжению (подсказка парсеру — TASK-022)
    runtime = make_runtime(tmp_path, [(script_scenario(), EXPECT_SCRIPT)])
    try:
        await say_to(runtime, "скрипт привет")
        report = await say_to(runtime, "полчаса")
        assert report["speech"] == "полчаса|duration|На сколько поставить таймер?"
    finally:
        await runtime.close()


async def test_chained_asks_keep_state(tmp_path):
    runtime = make_runtime(tmp_path, [(script_scenario(), CHAIN_SCRIPT)])
    try:
        first = await say_to(runtime, "скрипт привет")
        assert first["status"] == "awaiting"
        second = await say_to(runtime, "пять")
        assert second["status"] == "awaiting"  # продолжение спросило ещё — тот же механизм
        assert second["speech"] == "Второй вопрос?"
        third = await say_to(runtime, "шесть")
        assert third["status"] == "finished"
        assert third["speech"] == "пять и шесть, шаг 1"  # ctx.state жил между ожиданиями
        assert runtime.store.load_execution("dlg") is None
    finally:
        await runtime.close()


async def test_continuation_none_finishes_silently(tmp_path):
    # None после ответа — тихое завершение: продолжение владеет репликой,
    # «отказаться» в роутер нельзя (отказ возможен только на первом входе)
    runtime = make_runtime(tmp_path, [(script_scenario(), SILENT_SCRIPT)])
    try:
        await say_to(runtime, "скрипт привет")
        report = await say_to(runtime, "да")
        assert report["status"] == "finished" and report["speech"] == ""
        assert report["messages"] == []
    finally:
        await runtime.close()


# --- ожидание в StateStore: рестарт, pending/cancel, TTL -----------------------------


async def test_ask_survives_runtime_restart(tmp_path):
    runtime = make_runtime(tmp_path, [(script_scenario(), ASK_TIMER_SCRIPT)])
    first = await say_to(runtime, "скрипт привет")
    assert first["status"] == "awaiting"
    await runtime.close()
    calls = []
    runtime = make_runtime(tmp_path, [(script_scenario(), ASK_TIMER_SCRIPT)])  # новый Runtime, та же БД
    stub_timer(runtime, calls)
    try:
        report = await say_to(runtime, "на 300 секунд")
        assert report["reason"] == "continuation" and report["status"] == "finished"
        assert calls == [{"seconds": 300}]  # снимок вернул ctx.state и имя функции
    finally:
        await runtime.close()


async def test_pending_visible_and_cancellable_like_json(tmp_path):
    runtime = make_runtime(tmp_path, [(script_scenario(), ASK_TIMER_SCRIPT)])
    try:
        report = await say_to(runtime, "скрипт привет")
        assert report["status"] == "awaiting"
        pending = runtime.scenario_executor.pending()
        assert len(pending) == 1
        snapshot = pending[0]
        assert snapshot["dialog_id"] == "dlg"
        assert snapshot["awaiting"] == "on_duration"  # имя функции продолжения
        assert snapshot["awaiting_question"] == "На сколько поставить таймер?"
        assert snapshot["scenario"]["id"] == "user.script.hello"  # сценарий закреплён в снимке
        assert snapshot["script_ask"]["expect"] == "duration"  # версия файла + expect
        assert snapshot["script_ask"]["mtime_ns"]
        outcome = runtime.scenario_executor.cancel("dlg")  # cancel() — как у JSON-ожидания
        assert outcome.status == "cancelled"
        assert runtime.scenario_executor.pending() == []
        assert runtime.store.load_execution("dlg") is None
    finally:
        await runtime.close()


async def test_cancel_phrase_ends_wait(tmp_path):
    runtime = make_runtime(tmp_path, [(script_scenario(), ASK_TIMER_SCRIPT)])
    calls = []
    stub_timer(runtime, calls)
    try:
        await say_to(runtime, "скрипт привет")
        report = await say_to(runtime, "Отмена.")
        assert report["status"] == "cancelled" and report["messages"] == []
        assert calls == []  # продолжение не вызывалось
        assert runtime.store.load_execution("dlg") is None
        await say_to(runtime, "скрипт привет")  # сценарий выбирается заново
        report = await say_to(runtime, "стоп")
        assert report["status"] == "cancelled"
        assert runtime.store.load_execution("dlg") is None
    finally:
        await runtime.close()


async def test_ttl_expires_awaiting(tmp_path):
    runtime = make_runtime(tmp_path, [(script_scenario(), ASK_TIMER_SCRIPT)])
    try:
        report = await say_to(runtime, "скрипт привет")
        assert report["status"] == "awaiting"
        payload = runtime.store.load_execution("dlg")  # TTL — deadline того же снимка
        payload["deadline_remaining_s"] = 0
        runtime.store.save_execution("dlg", payload)
        expired = await say_to(runtime, "на 300 секунд")
        assert expired["status"] == "errored" and expired["speech"].endswith("deadline")
        assert runtime.store.load_execution("dlg") is None  # ожидание снято
    finally:
        await runtime.close()


# --- сбои -----------------------------------------------------------------------------


async def test_unknown_then_errors_at_first_ask(tmp_path):
    # валидатор файл не читает — неизвестная функция then даёт понятную errored
    # при ПЕРВОМ выполнении ask (интерпретация карточки, см. отчёт TASK-020)
    runtime = make_runtime(tmp_path, [(script_scenario(), BAD_THEN_SCRIPT)])
    try:
        report = await say_to(runtime, "скрипт привет")
        assert report["handled"] and report["status"] == "errored"
        assert report["speech"] == "Команда не выполнена: script_continuation_missing:nope"
        assert runtime.store.load_execution("dlg") is None  # ожидание не создано
        entries = [e for e in runtime.recent if e.get("kind") == "scenario"]
        assert len(entries) == 1 and "script_continuation_missing" in entries[0]["error"]
    finally:
        await runtime.close()


async def test_expired_json_ask_lets_phrase_route_normally(tmp_path):
    """TASK-097: истёкшее ожидание JSON-сценария не перехватывает следующую фразу —
    она идёт обычным путём (роутер), а не съедается продолжением старого диалога."""
    alarm = {"format_version": 2, "id": "timers.alarm.set", "version": "1.0.0",
             "category": "custom", "priority": 100,
             "triggers": [{"kind": "phrase", "phrases": ["поставь будильник"]}],
             "flow": [{"id": "q", "op": "ask", "question": "На какое время поставить будильник?",
                       "await": "on_time", "next": "done"},
                      {"id": "done", "op": "finish"}],
             "slots": {"on_time": {"type": "string"}}}
    news = script_scenario(sid="knowledge.news", phrases=("расскажи новости",))
    runtime = make_runtime(tmp_path, [(alarm, None), (news, None)])
    (runtime.scenario_dir / (news["id"] + ".py")).write_text(
        'from server_emulator.sdk import say\n\n\ndef handle(ctx):\n    return say("Новости дня")\n',
        encoding="utf-8")
    try:
        first = await say_to(runtime, "поставь будильник")
        assert first["handled"] and first["status"] == "awaiting"
        payload = runtime.store.load_execution("dlg")
        payload["deadline_remaining_s"] = 0  # вопрос истёк (два дня назад в бою)
        runtime.store.save_execution("dlg", payload)
        second = await say_to(runtime, "расскажи новости")
        assert second["reason"] != "continuation"      # не съедено старым диалогом
        assert second["scenario"] == "knowledge.news"  # фраза обработана заново роутером
        assert runtime.store.load_execution("dlg") is None
    finally:
        await runtime.close()
