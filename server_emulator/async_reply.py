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
import re
import time
import uuid
from urllib.parse import quote

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


def screen_card(response, text, suggests=()):
    """divui_show_view с карточкой ответа агента — шаблон agent_answer (реестр screen_templates,
    раскладка карты skill_card навыка Диалогов): шапка «Алиса», прокручиваемый текст, картинка
    и кнопки-подсказки (dialog://text_command — нажатие шлёт фразу), кнопка закрытия.
    На ответ по server_action колонка сама экран не открывает — карточку simple_text не видно.
    Картинка — первая markdown-картинка ответа ![…](https://…), из текста она убирается;
    нет картинки — элемент убран. Нет кнопок — убрана нижняя панель."""
    import json
    from pathlib import Path
    from . import screen_templates
    from .scenario_engine.proto_build import encode
    image = IMAGE.search(text or "")
    if image:
        text = (text[:image.start()] + text[image.end():]).strip()
    built = screen_templates.registry().build(
        "agent_answer", {"title": "Алиса", "text": text, "image_url": image.group(1) if image else ""})
    fields = built["fields"]
    body = json.loads(fields["Div2Card"]["StringBody"])
    content = body["card"]["states"][0]["div"]["items"][0]["close_button_wrapper_items"][0]
    for node in content["items"]:
        if node.get("id") == "answer_scroll" and not image:
            node["items"] = [item for item in node["items"] if item.get("id") != "answer_image"]
        if node.get("id") == "answer_suggests":
            node["items"][0]["items"] = [_suggest(phrase) for phrase in suggests]
    content["items"] = [node for node in content["items"]
                        if node.get("id") != "answer_suggests" or suggests]
    fields["Div2Card"]["StringBody"] = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    directive = response.Response.Directives.add()
    directive.Type, directive.Name = "client_action", built["name"]
    directive.AnalyticsType, directive.IsLedSilent = built["analytics_type"], True
    directive.PayloadRaw = encode(Path(__file__).resolve().parent / "schemas", built["proto"], fields, "centaur")


IMAGE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")


def _suggest(phrase):
    """Кнопка-подсказка карты (форма — кнопки skill_card): нажатие шлёт фразу как реплику."""
    return {"type": "answer_suggest_global_template", "answer_suggest_text": phrase,
            "answer_suggest_actions": [
                {"log_id": "agent_answer_suggest",
                 "url": "dialog://text_command?query=" + quote(phrase, safe="") + "&enable_nlg=false"}],
            "answer_suggest_backgroundColor": "#1b2129", "answer_suggest_textColor": "#e6eef9"}
