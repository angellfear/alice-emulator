"""Реестр обработчиков AAPI (aapi_handlers): обработчик регистрируется декоратором рядом со
своим кодом; Conversation.receive и has_handler (опция «Запись» → «Нет обработчиков») берут
его из реестра — отдельного списка событий нет."""
import types

from server_emulator import aapi_handlers
from server_emulator.aapi_handlers import aapi_handler, has_handler
from server_emulator.voice import Conversation


def test_new_handler_is_dispatched_and_visible_without_lists():
    calls = []

    @aapi_handler("TestOnlyEvent", enabled=lambda settings: bool(settings.get("test_flag")))
    async def handle(conversation, incoming, trigger):
        calls.append(trigger)
        return True

    try:
        assert not has_handler("TestOnlyEvent", {})              # условие по настройкам не выполнено
        assert has_handler("TestOnlyEvent", {"test_flag": True})
        conversation = Conversation(types.SimpleNamespace(settings={"test_flag": True}), None, None)
        import asyncio
        assert asyncio.run(conversation.receive(object(), "TestOnlyEvent")) is True
        assert calls == ["TestOnlyEvent"]
        conversation.runtime.settings = {}
        assert asyncio.run(conversation.receive(object(), "TestOnlyEvent")) is False
    finally:
        aapi_handlers._HANDLERS.pop("TestOnlyEvent", None)


def test_builtin_handlers_registered():
    for trigger in ("CancelRequest", "StreamControl", "Ack", "LogSpotter", "SynchronizeStateRequest", "RequestStat",
                    "MatchedUser", "CapabilityUpdateState", "ContinueRequest"):
        assert has_handler(trigger, {}), trigger
    assert not has_handler("НетТакогоСобытия", {})


def test_input_handlers_follow_profile_flags():
    assert not has_handler("TextInput", {})
    assert has_handler("TextInput", {"aapi_model": True})
    assert not has_handler("VoiceInput", {"aapi_model": True})
    assert has_handler("VoiceInput", {"aapi_voice": True})


# --- TASK-069: MatchedUser, CapabilityUpdateState, ContinueRequest --------------------

import time

import pytest

from server_emulator.runtime import Runtime
from tests.conftest import types as message_types

ACCESSIBILITY_TYPE = 78  # NAlice.TCapability.ECapabilityType.AccessibilityVoiceControlType (схема в репозитории)


def _client():
    client, _server = message_types()
    return client


async def test_matched_user_saved_without_reply(tmp_path):
    """Биометрия говорящего — в разговор и сессию устройства; ответа нет, промаха нет."""
    runtime = Runtime(tmp_path)
    client, server = message_types()
    sent = []
    conversation = Conversation(runtime, server, sent.append, trace_context={"session": "s1", "sn": "SN-B"})
    runtime.sessions["s1"] = {"device_sn": "SN-B"}
    message = client()
    message.Event.Header.MessageId = "mid-1"
    matched = message.Event.MatchedUser
    matched.Header.RequestId = "req-1"
    matched.VoiceInputMessageId = "voice-1"
    adult = matched.Request.Event.BiometryClassification.Scores.add()
    adult.ClassName, adult.Confidence, adult.Tag = "adult", 0.98866, "children"
    male = matched.Request.Event.BiometryClassification.Scores.add()
    male.ClassName, male.Confidence, male.Tag = "male", 0.99724, "gender"
    simple = matched.Request.Event.BiometryClassification.Simple.add()
    simple.ClassName, simple.Tag = "male", "gender"
    try:
        assert await conversation.receive(message, "MatchedUser") is True
        assert sent == []  # облако на MatchedUser не отвечает — и мы тоже
        assert conversation.speaker["scores"]["adult"] == pytest.approx(0.989, abs=0.001)
        assert conversation.speaker["simple"] == {"gender": "male"}
        session = runtime.sessions["s1"]
        assert session["speaker"]["scores"]["male"] == pytest.approx(0.997, abs=0.001)
        assert runtime.aapi_misses.public() == []  # в «Без ответа» не попадает
    finally:
        await runtime.close()


async def test_capability_update_state_saved_with_preview(tmp_path):
    """Последнее состояние каждого типа возможностей — в сессии (память) и в разговоре."""
    runtime = Runtime(tmp_path)
    client, server = message_types()
    sent = []
    conversation = Conversation(runtime, server, sent.append, trace_context={"session": "s1", "sn": "SN-B"})
    runtime.sessions["s1"] = {"device_sn": "SN-B"}
    message = client()
    state = message.Event.CapabilityUpdateState
    state.RequestMessageId = "voice-1"
    state.EndpointId = "SN-B"
    state.CapabilityType = ACCESSIBILITY_TYPE
    state.AnyCapability.RawCapability = b"com.yandex.devices.player \xd0\xa1\xd0\xba\xd0\xb0\xd0\xb7\xd0\xba\xd0\xb8 00:00"
    raw_size = len(state.AnyCapability.RawCapability)
    try:
        assert await conversation.receive(message, "CapabilityUpdateState") is True
        assert sent == []
        entry = runtime.sessions["s1"]["capabilities"]["AccessibilityVoiceControlType"]
        assert entry["bytes"] == raw_size and "com.yandex.devices.player" in entry["preview"]
        assert "Сказки" in entry["preview"]
        assert conversation.capabilities["AccessibilityVoiceControlType"]["ts"] <= time.time()
        # второй тип — отдельная запись, первый не затёрт
        other = client()
        other.Event.CapabilityUpdateState.CapabilityType = 23  # VolumeCapabilityType
        other.Event.CapabilityUpdateState.AnyCapability.RawCapability = b"volume 5"
        assert await conversation.receive(other, "CapabilityUpdateState") is True
        caps = runtime.sessions["s1"]["capabilities"]
        assert set(caps) == {"AccessibilityVoiceControlType", "VolumeCapabilityType"}
        assert caps["VolumeCapabilityType"]["preview"] == "volume 5"
    finally:
        await runtime.close()


async def test_continue_request_no_reply_logged(tmp_path):
    """ContinueRequest без образца: ответа нет, промаха нет, запись в журнале сервера."""
    runtime = Runtime(tmp_path)
    client, server = message_types()
    sent = []
    conversation = Conversation(runtime, server, sent.append, trace_context={"session": "s1", "sn": "SN-C"})
    runtime.sessions["s1"] = {"device_sn": "SN-C"}
    message = client()
    message.Event.ContinueRequest.RequestMessageId = "mid-9"
    try:
        assert await conversation.receive(message, "ContinueRequest") is True
        assert sent == []
        await runtime.server_log.flush()
        entries = runtime.server_log.query(source="ws")["items"]
        assert any("ContinueRequest" in item["message"] for item in entries)
        assert entries[0]["detail"].get("request_message_id") == "mid-9"
    finally:
        await runtime.close()


async def test_scenario_context_carries_speaker_and_capabilities(tmp_path):
    """Сценарии и агент видят говорящего и возможности колонки в контексте устройства."""
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-B", "user_id": "u"})
    runtime.sessions["s1"] = {"device_sn": "SN-B",
                              "speaker": {"scores": {"adult": 0.99}, "simple": {"gender": "male"}, "ts": 1.0},
                              "capabilities": {"VolumeCapabilityType": {"ts": 2.0, "bytes": 10,
                                                                        "preview": "громкость 5"}}}
    try:
        context = runtime._scenario_context(sn="SN-B")
        assert context["speaker"]["scores"]["adult"] == 0.99
        assert context["device"]["capabilities"]["VolumeCapabilityType"] == "громкость 5"
        empty = runtime._scenario_context(sn="SN-NOPE")
        assert empty["speaker"] is None and empty["device"]["capabilities"] == {}
    finally:
        await runtime.close()
