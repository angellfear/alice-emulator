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
    for trigger in ("CancelRequest", "StreamControl", "Ack", "LogSpotter", "SynchronizeStateRequest", "RequestStat"):
        assert has_handler(trigger, {}), trigger
    assert not has_handler("НетТакогоСобытия", {})


def test_input_handlers_follow_profile_flags():
    assert not has_handler("TextInput", {})
    assert has_handler("TextInput", {"aapi_model": True})
    assert not has_handler("VoiceInput", {"aapi_model": True})
    assert has_handler("VoiceInput", {"aapi_voice": True})
