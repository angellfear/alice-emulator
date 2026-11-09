"""continue_session: второй вариант отложенного ответа — placeholder с OnFinish
вместо Push. Колонка после проигрывания сама присылает TextInput с фреймом
continue_session_stage2_semantic_frame — готовый ответ модели уходит на него.
Push-режим (по умолчанию) не меняется — tests/test_async_reply.py."""
import asyncio
import json

from server_emulator import async_reply
from server_emulator.runtime import Runtime
from server_emulator.voice import Conversation
from tests.conftest import types
from tests.test_voice_failures import make_ogg

ANSWER = "Завтра в Москве солнечно."


def _defer_mode(runtime, mode):
    runtime.settings.setdefault("agent", {})["defer_mode"] = mode


async def defer_run(tmp_path, model_delay=2.0, answer=ANSWER, mode="continue_session", after_s=0.2):
    """Реплика «расскажи…» с медленной моделью: first — placeholder/ответ сразу,
    later — что ушло колонке после stage2 (или Push)."""
    runtime = Runtime(tmp_path)
    _defer_mode(runtime, mode)
    runtime.settings.update(aapi_model=True, aapi_tts=True)
    runtime.settings["model"]["stub_text"] = ""
    runtime.settings["agent"]["async_reply_s"] = after_s
    ogg, tts, sent = await make_ogg(), [], []

    async def provider(kind, payload=None, **kwargs):
        if kind == "tts":
            tts.append(payload["input"])
            return ogg, "audio/ogg"
        await asyncio.sleep(model_delay)
        return json.dumps({"choices": [{"message": {"content": answer}}], "usage": {}}).encode(), "application/json"
    runtime.provider = provider
    client, server = types()

    async def send(data):
        sent.append(data)
    conv = Conversation(runtime, server, send, trace_context={"sn": "SN-T"})

    def events():
        return [server.FromString(raw[4:]).Event for raw in sent if raw.startswith(b"AAPI")]

    incoming = client()
    incoming.Event.Header.MessageId = "m1"
    incoming.Event.TextInput.Header.RequestId = "r1"
    incoming.Event.TextInput.Header.DialogId = "dialog-1"
    incoming.Event.TextInput.Request.Event.Text = "расскажи что-нибудь"
    await conv.receive(incoming, "TextInput")
    first = []
    for _ in range(100):  # placeholder/заполнитель ушёл, модель думает
        first = [e for e in events() if e.WhichOneof("Event") == "AliceResponse"]
        if first:
            break
        await asyncio.sleep(0.05)
    return runtime, conv, client, server, events, first, tts


async def test_placeholder_with_on_finish(tmp_path):
    """Шаг 1: вместо речи — tts_play_placeholder с OnFinish continue_session_stage1."""
    runtime, conv, client, server, events, first, tts = await defer_run(tmp_path)
    try:
        assert len(first) == 1
        reply = first[0]
        assert reply.AliceResponse.VoiceResponse.OutputSpeech.Text == ""  # речи нет
        [directive] = reply.AliceResponse.Response.Directives
        assert directive.Name == "tts_play_placeholder"
        assert directive.Payload.fields["channel"].string_value == "Dialog"
        assert directive.HasField("OnFinish")
        frame = directive.OnFinish.TypedCallbackRequest.fields["typed_semantic_frame"].struct_value
        assert "continue_session_stage1_semantic_frame" in frame.fields
        assert tts == []  # озвучки «секунду, подумайте» нет
    finally:
        await conv.shutdown()
        await runtime.close()


async def test_stage2_delivers_model_answer(tmp_path):
    """Шаг 2: stage2-фрейм с dialog_id — готовый ответ модели с голосом.
    Модель завершается сразу после defer (0.5 с) — stage2 ждёт и получает ответ."""
    runtime, conv, client, server, events, first, tts = await defer_run(tmp_path, model_delay=0.6,
                                                                        after_s=0.5)
    try:
        back = client()
        back.Event.Header.MessageId = "m2"
        back.Event.TextInput.Header.RequestId = "r2"
        back.Event.TextInput.Header.DialogId = "dialog-1"
        action = back.Event.TextInput.Request.Event
        action.Type = 6
        action.Name = "@@mm_semantic_frame"
        from google.protobuf import json_format
        json_format.ParseDict({"typed_semantic_frame": {
            "continue_session_stage2_semantic_frame": {"dialog_id": "dialog-1"}}}, action.Payload)
        await conv.receive(back, "TextInput")
        await asyncio.wait_for(conv.task, 10)
        replies = [e.AliceResponse for e in events() if e.WhichOneof("Event") == "AliceResponse"]
        final = [r for r in replies if r.VoiceResponse.OutputSpeech.Text]
        assert final and final[-1].VoiceResponse.OutputSpeech.Text == ANSWER
        assert tts and ANSWER in tts  # озвучка ответа
        assert any(e.WhichOneof("Event") == "TtsSpeak" for e in events())
    finally:
        await conv.shutdown()
        await runtime.close()


async def test_stage2_without_answer_says_missing(tmp_path):
    """Модель не успела за async_reply_s — «Не получилось» (MISSING_TEXT)."""
    runtime, conv, client, server, events, first, tts = await defer_run(tmp_path, model_delay=30.0)
    try:
        back = client()
        back.Event.Header.MessageId = "m2"
        back.Event.TextInput.Header.RequestId = "r2"
        back.Event.TextInput.Header.DialogId = "dialog-1"
        action = back.Event.TextInput.Request.Event
        action.Type = 6
        action.Name = "@@mm_semantic_frame"
        from google.protobuf import json_format
        json_format.ParseDict({"typed_semantic_frame": {
            "continue_session_stage2_semantic_frame": {"dialog_id": "dialog-1"}}}, action.Payload)
        await conv.receive(back, "TextInput")
        await asyncio.wait_for(conv.task, 10)
        replies = [e.AliceResponse for e in events() if e.WhichOneof("Event") == "AliceResponse"]
        assert replies and replies[-1].VoiceResponse.OutputSpeech.Text == async_reply.MISSING_TEXT
    finally:
        await conv.shutdown()
        await runtime.close()


async def test_push_mode_unchanged(tmp_path):
    """defer_mode не задан (push) — прежняя механика: короткий ответ + Push."""
    runtime, conv, client, server, events, first, tts = await defer_run(tmp_path, mode="push")
    try:
        assert first and first[0].AliceResponse.VoiceResponse.OutputSpeech.Text  # реплика-заполнитель
        pushes = []
        for _ in range(100):  # модель отвечает в фоне — ждём Push
            pushes = [e.Push for e in events() if e.WhichOneof("Event") == "Push"]
            if pushes:
                break
            await asyncio.sleep(0.05)
        assert pushes and pushes[0].Directives[0].Name == "@@mm_semantic_frame"
    finally:
        await conv.shutdown()
        await runtime.close()


async def test_defer_mode_switch_live(tmp_path):
    """defer_mode читается живьём из настроек; неизвестное значение — push."""
    runtime = Runtime(tmp_path)
    try:
        assert async_reply.defer_mode(runtime) == "push"  # по умолчанию
        _defer_mode(runtime, "continue_session")
        assert async_reply.defer_mode(runtime) == "continue_session"
        _defer_mode(runtime, "что-то иное")
        assert async_reply.defer_mode(runtime) == "push"
    finally:
        await runtime.close()
