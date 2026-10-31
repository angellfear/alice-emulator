"""Отложенный ответ модели через Push (async_reply.py): модель думает дольше async_reply_s —
короткий ответ сразу, затем Push server_action @@mm_semantic_frame (как напоминание облака),
колонка присылает TextInput с ним и получает ответ модели с голосом."""
import asyncio
import json

from server_emulator import async_reply
from server_emulator.runtime import Runtime
from server_emulator.voice import Conversation
from tests.conftest import types
from tests.test_voice_failures import make_ogg

ANSWER = "Завтра в Москве солнечно."


async def talk(tmp_path, model_delay, model_error=None, answer=ANSWER, tool_calls=None):
    async_reply.ANSWERS.clear()  # общее хранилище модуля: ответы других тестов не мешают
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, aapi_tts=True)
    runtime.settings["model"]["stub_text"] = ""
    runtime.settings["agent"]["async_reply_s"] = 0.2
    ogg, tts, sent = await make_ogg(), [], []

    async def provider(kind, payload=None, **kwargs):
        if kind == "tts":
            tts.append(payload["input"])
            return ogg, "audio/ogg"
        await asyncio.sleep(model_delay)
        if model_error:
            raise model_error
        return json.dumps({"choices": [{"message": {"content": answer}}], "usage": {}}).encode(), "application/json"
    runtime.provider = provider
    if tool_calls:  # ответ агента, ждущий подтверждения действия (как AgentService._answer)
        async def chat(text, **kwargs):
            await asyncio.sleep(model_delay)
            return {"text": answer, "tool_calls": tool_calls}
        runtime.chat = chat
    client, server = types()

    async def send(data):
        sent.append(data)
    conv = Conversation(runtime, server, send, trace_context={"sn": "SN-T"})

    def events():
        return [server.FromString(raw[4:]).Event for raw in sent if raw.startswith(b"AAPI")]

    async def until(check):
        for _ in range(100):
            if check():
                return
            await asyncio.sleep(0.05)
        raise AssertionError("не дождались")

    incoming = client()
    incoming.Event.Header.MessageId = "m1"
    incoming.Event.TextInput.Header.RequestId = "r1"
    incoming.Event.TextInput.Request.Event.Text = "расскажи что-нибудь"
    await conv.receive(incoming, "TextInput")
    await asyncio.wait_for(conv.task, 10)
    first = [e.AliceResponse for e in events() if e.WhichOneof("Event") == "AliceResponse"]
    await until(lambda: any(e.WhichOneof("Event") == "Push" for e in events()))
    push = next(e.Push for e in events() if e.WhichOneof("Event") == "Push")
    synthesized = list(tts)  # звук ответа готов до запроса колонки

    # колонка выполняет server_action из Push: TextInput с тем же payload
    back = client()
    back.Event.Header.MessageId = "m2"
    back.Event.TextInput.Header.RequestId = "r2"
    action = back.Event.TextInput.Request.Event
    action.Type = 6
    action.Name = push.Directives[0].Name
    action.Payload.CopyFrom(push.Directives[0].Payload)
    sent.clear()
    await conv.receive(back, "TextInput")
    await asyncio.wait_for(conv.task, 10)
    later = [e for e in events() if e.WhichOneof("Event") in ("AliceResponse", "TtsSpeak")]
    await conv.shutdown()
    await runtime.close()
    return first, push, later, tts, synthesized


async def test_slow_model_answers_later_by_push(tmp_path):
    first, push, later, tts, synthesized = await talk(tmp_path, model_delay=0.6)
    assert first[0].Response.Cards[0].Text == async_reply.WAIT_TEXT  # колонка сразу слышит «Секунду, думаю»
    directive = push.Directives[0]
    assert (directive.Type, directive.Name) == ("server_action", "@@mm_semantic_frame")
    response = later[0].AliceResponse
    assert response.Header.RequestId == "r2" and response.Response.Cards[0].Text == ANSWER
    assert response.VoiceResponse.HasVoiceResponse and later[1].WhichOneof("Event") == "TtsSpeak"
    # экран колонка на server_action сама не открывает — карточка ответа директивой divui_show_view
    screen = [d for d in response.Response.Directives if d.Name == "divui_show_view"]
    assert len(screen) == 1 and ANSWER.encode() in screen[0].PayloadRaw
    assert tts == [async_reply.WAIT_TEXT, ANSWER] and not async_reply.ANSWERS  # ответ выдан один раз
    assert synthesized == tts  # синтез — до Push, на запрос колонки звук уходит сразу
    assert not response.VoiceResponse.ShouldListen  # обычный ответ — колонка не слушает дальше


async def test_slow_model_failure_comes_by_push_too(tmp_path):
    first, push, later, tts, synthesized = await talk(tmp_path, model_delay=0.6, model_error=ValueError("HTTP 502"))
    text = later[0].AliceResponse.Response.Cards[0].Text
    assert text.startswith("Не получилось получить ответ модели") and "502" in text
    assert tts[-1] == text.replace(" 502", "")  # голос — фраза сбоя из кеша, без кода


async def test_fast_model_answers_in_place(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings["agent"]["async_reply_s"] = 3
    assert async_reply.after(runtime) == 3
    runtime.settings["agent"]["async_reply_s"] = 0
    assert async_reply.after(runtime) == 0
    await runtime.close()


async def test_question_by_push_keeps_listening(tmp_path):
    """Отложенный ответ — вопрос: колонка слушает ответ без «Алиса»."""
    first, push, later, tts, synthesized = await talk(tmp_path, 0.6, answer="В какой комнате включить свет?")
    assert later[0].AliceResponse.VoiceResponse.ShouldListen


async def test_confirmation_by_push_keeps_listening(tmp_path):
    """Агент ждёт подтверждения действия: ответ по Push просит подтвердить и колонка слушает «да/нет»."""
    call = {"id": "c1", "type": "function", "function": {"name": "light_off", "arguments": "{}"}}
    first, push, later, tts, synthesized = await talk(tmp_path, 0.6, answer="Выключить свет. Подтвердите.",
                                                      tool_calls=[call])
    response = later[0].AliceResponse
    assert response.Response.Cards[0].Text == "Выключить свет. Подтвердите." and response.VoiceResponse.ShouldListen
