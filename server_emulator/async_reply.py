"""Отложенный ответ модели: Push (agent.async_reply_s) или продолжение сессии (defer_mode).

Модель не ответила за async_reply_s секунд → короткий ответ сразу, модель дорабатывает в фоне.
Два варианта доставки (settings["agent"]["defer_mode"]):
- push (по умолчанию): готовый ответ хранится здесь, колонке уходит Push с директивой
  server_action @@mm_semantic_frame — как облако доставляет сработавшее напоминание
  (перехват volume-set, purpose shoot_reminder). Колонка сама присылает TextInput с этим
  server_action, и ответ модели уходит на него обычным AliceResponse с карточкой и голосом.
- continue_session: вместо Push колонке уходит tts_play_placeholder с OnFinish
  (TypedCallbackRequest = Struct continue_session_stage1) — после проигрывания колонка сама
  присылает TextInput с фреймом continue_session_stage2_semantic_frame.dialog_id, сервер
  отдаёт готовый ответ на него. Ответ ждёт колонку не дольше async_reply_s; не готов —
  «Не получилось» (как у quasar-server input-handler.ts).
"""
import asyncio
import time
import uuid

from google.protobuf import json_format

PURPOSE = "agent_answer"
ASYNC_AFTER_S = 3
WAIT_TEXT = "Секунду, думаю."
MISSING_TEXT = "Ответ уже недоступен."  # Push пришёл, а ответ устарел или уже выдан
KEEP_S = 600  # неполученный ответ живёт 10 минут
DEFER_MODES = ("push", "continue_session")

ANSWERS = {}  # id → {"answer", "text", "ts"}: общие для всех соединений (колонка могла переподключиться)
CONTINUES = {}  # dialog_id → {"future": Future(answer|None), "ts"} — stage2 ждёт модель


def defer_mode(runtime):
    """Вариант отложенного ответа: settings["agent"]["defer_mode"] (push | continue_session);
    неизвестное значение — push. Читается живьём."""
    value = str((runtime.settings.get("agent") or {}).get("defer_mode") or "push")
    return value if value in DEFER_MODES else "push"


def after(runtime):
    """Через сколько секунд без ответа модели отвечать «Секунду, думаю» и досылать Push; 0 — выкл."""
    from .agent import engine
    value = (runtime.settings.get("agent") or {}).get("async_reply_s")
    value = ASYNC_AFTER_S if value is None else float(value)
    return value if value > 0 and engine(runtime) == "graph" else 0


def wait_text(runtime):
    return " ".join(str((runtime.settings.get("agent") or {}).get("async_wait_text") or WAIT_TEXT).split())


def keep(answer, text):
    now = time.time()
    for key in [k for k, v in ANSWERS.items() if now - v["ts"] > KEEP_S]:
        ANSWERS.pop(key, None)
    answer_id = uuid.uuid4().hex
    ANSWERS[answer_id] = {"answer": answer, "text": text, "ts": now}
    return answer_id


def take(answer_id):
    return ANSWERS.pop(answer_id, None)


def keep_continue(key, text):
    """Ожидание модели по ключу диалога (continue_session): Future с ответом или None —
    колонка сама заберёт ответ stage2-фреймом, не дольше agent.async_reply_s."""
    now = time.time()
    for stale in [k for k, v in CONTINUES.items() if now - v["ts"] > KEEP_S]:
        CONTINUES.pop(stale, None)
    future = asyncio.get_running_loop().create_future()
    CONTINUES[str(key)] = {"future": future, "text": str(text or ""), "ts": now}
    return future


def resolve_continue(key, answer):
    """Модель закончила (ответ или сбой — failure-текст уже в answer) — stage2 получит его."""
    entry = CONTINUES.pop(str(key or ""), None)
    if entry is not None and not entry["future"].done():
        entry["future"].set_result(answer)


def pending(key):
    """Ждёт ли ответ по этому ключу (continue_session ещё не получил stage2)."""
    entry = CONTINUES.get(str(key or ""))
    return entry is not None and not entry["future"].done()


def drop_continue(key):
    """Диалог отменён/истёк — ждущий stage2 получит None («Не получилось»)."""
    entry = CONTINUES.pop(str(key or ""), None)
    if entry is not None and not entry["future"].done():
        entry["future"].set_result(None)


async def wait_continue(key, timeout):
    """Ответ по ключу диалога или None — не готов вовремя (agent.async_reply_s)."""
    entry = CONTINUES.get(str(key or ""))
    if entry is None:
        return None
    try:
        return await asyncio.wait_for(asyncio.shield(entry["future"]), timeout)
    except asyncio.TimeoutError:
        return None


def push(server_type, answer_id):
    """Push с server_action @@mm_semantic_frame (форма — из перехвата напоминания облака);
    id ответа — в analytics.origin_info, колонка вернёт его в своём TextInput."""
    output = server_type()
    output.Event.Header.MessageId = uuid.uuid4().hex
    output.Event.Header.Ack = int(time.time() * 1000)
    message = output.Event.Push
    message.DeduplicationPushId = "agent-answer-" + answer_id
    message.PushIds.append(uuid.uuid4().hex)
    directive = message.Directives.add()
    directive.Type = "server_action"
    directive.Name = "@@mm_semantic_frame"
    directive.IsLedSilent = True
    json_format.ParseDict({
        "analytics": {"origin_info": answer_id, "purpose": PURPOSE, "origin": "Scenario"},
        "typed_semantic_frame": {"reminders_on_shoot_semantic_frame": {"id": {"string_value": answer_id}}},
    }, directive.Payload, descriptor_pool=server_type.DESCRIPTOR.file.pool)
    output.Timings.SendingTime.seconds = int(time.time())
    return output


def screen_card(response, text):
    """divui_show_view с простой Div2-карточкой текста ответа. На ответ по server_action колонка
    сама экран не открывает (нет голосовой активации) — карточку simple_text не видно; облако
    в ответе на напоминание тоже шлёт divui_show_view (перехват volume-set). Поля — как у
    погоды (scenarios/informers/weather.json), карточка — минимальная.
    TODO: длинный текст (≈800 символов) не помещается и не прокручивается — контейнер по центру
    обрезан сверху и снизу (проверено на колонке 2026-10-30). Переделать на прокручиваемую
    карточку: например, gallery orientation=vertical с текстом от верха."""
    import json
    from pathlib import Path
    from .scenario_engine.proto_build import encode
    body = {"templates": {}, "card": {"log_id": "agent_answer", "states": [{"state_id": 0, "div": {
        "type": "container", "width": {"type": "match_parent"}, "height": {"type": "match_parent"},
        "background": [{"type": "solid", "color": "#0b0f14"}], "content_alignment_vertical": "center",
        "paddings": {"left": 96, "right": 96, "top": 64, "bottom": 64},
        "items": [{"type": "text", "text": text, "font_size": 40, "line_height": 52, "text_color": "#ffffff"}]}}]}}
    fields = {"Name": "divui_show_view", "Layer": {"Dialog": {}}, "InactivityTimeout": "Long", "OverlayBehaviour": "Hide",
              "Div2Card": {"CardName": "agent_answer", "Id": {"CardName": "agent_answer", "CardId": uuid.uuid4().hex},
                           "StringBody": json.dumps(body, ensure_ascii=False, separators=(",", ":"))}}
    directive = response.Response.Directives.add()
    directive.Type, directive.Name = "client_action", "divui_show_view"
    directive.AnalyticsType, directive.IsLedSilent = "divui_show_view", True
    directive.PayloadRaw = encode(Path(__file__).resolve().parent / "schemas",
                                  "NAlice.TLayeredDivUICapability.TDivUIShowViewDirective", fields, "centaur")
