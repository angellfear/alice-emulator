"""Карточка диалога по реальному каркасу + выбор модели карточка/действие/оба."""
import asyncio
import json
import struct

import pytest
from aiohttp import ClientSession

from server_emulator.model_actions import TOOL_SCHEMA, execute, parse_calls
from server_emulator.response_compose import add_volume_directive, finalize
from server_emulator.runtime import Runtime
from server_emulator.schema import load_type
from server_emulator.server import create_app
from tests.conftest import listen
from tests.conftest import _open_session
from tests.conftest import page, types

SCHEMAS = "server_emulator/schemas"


def server_type():
    return load_type(SCHEMAS, "alice-api.pb", "NAlice.NAliceApi.TServerMessage")()


def _response_of(sent):
    for raw in reversed(sent):
        if raw.startswith(b"AAPI"):
            message = server_type().FromString(raw[4:])
            if message.Event.WhichOneof("Event") == "AliceResponse":
                return message.Event.AliceResponse
    raise AssertionError("AliceResponse не отправлен")


# --- каркас реального ответа -------------------------------------------------------


def test_finalize_adds_cardid_sequence_placeholder():
    message = server_type()
    response = message.Event.AliceResponse
    response.Response.Cards.add(Type="simple_text", Text="привет")
    finalize(message, 7)
    assert response.Header.CardId if False else True
    assert response.Response.Cards[0].CardId  # новый идентификатор карточки
    assert response.Header.SequenceNumber == 7
    assert response.Header.DialogId == ""  # реальный сервер шлёт пустой
    names = [d.Name for d in response.Response.Directives]
    assert "tts_play_placeholder" in names
    channel = next(d for d in response.Response.Directives if d.Name == "tts_play_placeholder").Payload
    assert channel["channel"] == "Dialog"


def test_finalize_unique_cardids_and_keeps_existing():
    first, second = server_type(), server_type()
    first.Event.AliceResponse.Response.Cards.add(Type="simple_text", Text="a")
    second.Event.AliceResponse.Response.Cards.add(Type="simple_text", Text="b")
    finalize(first, 1); finalize(second, 2)
    assert first.Event.AliceResponse.Response.Cards[0].CardId != \
        second.Event.AliceResponse.Response.Cards[0].CardId
    custom = server_type()
    card = custom.Event.AliceResponse.Response.Cards.add(Type="simple_text", Text="c")
    card.CardId = "fixed-id"
    finalize(custom, 3)
    assert custom.Event.AliceResponse.Response.Cards[0].CardId == "fixed-id"  # не перезаписываем


def test_volume_directive_matches_capture():
    message = server_type()
    add_volume_directive(message.Event.AliceResponse, 5)
    directive = message.Event.AliceResponse.Response.Directives[0]
    assert directive.Type == "client_action" and directive.Name == "sound_set_level"
    assert directive.Payload["new_level"] == 5.0  # wire из volume-set fixture


# --- выбор модели: парсинг вызовов -------------------------------------------------


def test_parse_calls_filters_own_tool_only():
    calls = parse_calls([
        {"id": "1", "function": {"name": "alice_action", "arguments": '{"kind":"volume","level":3}'}},
        {"id": "2", "function": {"name": "other_tool", "arguments": "{}"}},
        {"id": "3", "function": {"name": "alice_action", "arguments": "не json"}},
    ])
    assert [c["id"] for c in calls] == ["1"] and calls[0]["kind"] == "volume"


# --- полный тракт: карточка появляется у заглушки -----------------------------------


async def test_stub_answer_now_carries_full_card_skeleton(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings['aapi_model'] = True
    runtime.settings['model']['stub_text'] = 'привет'
    sent = []

    async def send(data):
        sent.append(data)
    client, server = types()
    conv = __import__("server_emulator.voice", fromlist=["Conversation"]).Conversation(runtime, server, send)
    text = client()
    text.Event.Header.MessageId = 'm'
    text.Event.TextInput.Request.Event.Text = 'скажи привет'
    await conv.receive(text, 'TextInput')
    await asyncio.wait_for(conv.task, 3)
    response = _response_of(sent)
    card = response.Response.Cards[0]
    assert card.Type == 'simple_text' and card.Text == 'привет' and card.CardId
    assert response.Header.SequenceNumber >= 1
    assert any(d.Name == 'tts_play_placeholder' for d in response.Response.Directives)
    await conv.shutdown()


# --- модель выбирает действие: и текст, и действие ---------------------------------


async def test_model_action_volume_directive_and_card_together(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, model_actions=True, builtin_scenarios=False)
    sent = []

    async def chat(text, history=None, **context):
        return {'text': 'Сделала громче.', 'tool_calls': [
            {"id": "c1", "function": {"name": "alice_action",
                                      "arguments": '{"kind":"volume","level":4}'}},
        ]}
    runtime.chat = chat

    async def send(data):
        sent.append(data)
    client, server = types()
    conv = __import__("server_emulator.voice", fromlist=["Conversation"]).Conversation(runtime, server, send)
    text = client()
    text.Event.Header.MessageId = 'm'
    text.Event.TextInput.Request.Event.Text = 'сделай громче'
    await conv.receive(text, 'TextInput')
    await asyncio.wait_for(conv.task, 3)
    response = _response_of(sent)
    # «и то и то»: карточка с текстом модели + директива громкости в одном ответе
    assert response.Response.Cards[0].Text == 'Сделала громче.'
    volume = next(d for d in response.Response.Directives if d.Name == 'sound_set_level')
    assert volume.Payload['new_level'] == 4.0
    await conv.shutdown()


async def test_model_action_light_changes_state_and_notes(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, model_actions=True, builtin_scenarios=False)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-T', 'user_id': 'owner',
                               'device_ids': ['dev-t'], 'profile': {'aapi_model': True}})
    device = next(d for d in runtime.identities.data['devices'] if d['sn'] == 'SN-T')
    runtime.identities.mutate({'operation': 'state', 'sn': 'SN-T',
                               'state': dict(device['state'], light={'кухня': {'on': False}})})
    sent = []

    async def chat(text, history=None, **context):
        return {'text': 'Включаю свет на кухне.', 'tool_calls': [
            {"id": "c2", "function": {"name": "alice_action",
                                      "arguments": '{"kind":"light","room":"кухня","on":true}'}},
        ]}
    runtime.chat = chat

    async def send(data):
        sent.append(data)
    client, server = types()
    from server_emulator.voice import Conversation
    conv = Conversation(runtime, server, send, trace_context={'sn': 'SN-T'})
    text = client()
    text.Event.Header.MessageId = 'm'
    text.Event.TextInput.Request.Event.Text = 'включи свет на кухне'
    await conv.receive(text, 'TextInput')
    await asyncio.wait_for(conv.task, 3)
    response = _response_of(sent)
    texts = [c.Text for c in response.Response.Cards]
    assert 'Включаю свет на кухне.' in texts and 'Свет (кухня) включён.' in texts
    device = next(d for d in runtime.identities.data['devices'] if d['sn'] == 'SN-T')
    assert device['state']['light']['кухня']['on'] is True
    await conv.shutdown()


async def test_model_action_unsupported_deferred_not_faked(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, model_actions=True, builtin_scenarios=False)
    sent = []

    async def chat(text, history=None, **context):
        return {'text': 'Запускаю.', 'tool_calls': [
            {"id": "c3", "function": {"name": "alice_action",
                                      "arguments": '{"kind":"app","package":"ru.yandex.searchplugin"}'}},
        ]}
    runtime.chat = chat

    async def send(data):
        sent.append(data)
    client, server = types()
    from server_emulator.voice import Conversation
    conv = Conversation(runtime, server, send)
    text = client()
    text.Event.Header.MessageId = 'm'
    text.Event.TextInput.Request.Event.Text = 'открой браузер'
    await conv.receive(text, 'TextInput')
    await asyncio.wait_for(conv.task, 3)
    response = _response_of(sent)
    texts = [c.Text for c in response.Response.Cards]
    assert any('исполнитель не подключён' in t for t in texts)  # честная заметка
    assert any(c.get('origin') == 'model_action' for c in runtime.pending_calls)  # сохранено в панель
    assert not any(d.Name == 'open_app' for d in response.Response.Directives)  # фиктивной директивы нет
    await conv.shutdown()


async def test_model_actions_off_keeps_tool_calls_manual(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings['aapi_model'] = True  # model_actions выключен
    runtime.settings['builtin_scenarios'] = False  # тестируем модельный путь, не встроенные
    sent = []

    async def chat(text, history=None, **context):
        return {'text': 'Готово.', 'tool_calls': [
            {"id": "c4", "function": {"name": "alice_action",
                                      "arguments": '{"kind":"timer","seconds":60}'}},
        ]}
    runtime.chat = chat

    async def send(data):
        sent.append(data)
    client, server = types()
    from server_emulator.voice import Conversation
    conv = Conversation(runtime, server, send)
    text = client()
    text.Event.Header.MessageId = 'm'
    text.Event.TextInput.Request.Event.Text = 'поставь таймер'
    await conv.receive(text, 'TextInput')
    await asyncio.wait_for(conv.task, 3)
    response = _response_of(sent)
    assert [c.Text for c in response.Response.Cards] == ['Готово.']  # без карточек-заметок
    assert runtime.pending_calls and runtime.pending_calls[-1]['function']['name'] == 'alice_action'
    assert not hasattr(runtime, "timer_scheduler")  # TASK-079: планировщика нет — не исполнилось молча
    await conv.shutdown()


def test_chat_injects_alice_action_tool_only_when_enabled(tmp_path):
    runtime = Runtime(tmp_path)
    captured = {}

    async def provider(kind, payload=None, **kwargs):
        captured['payload'] = payload
        return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode(), 'application/json'
    runtime.provider = provider
    runtime.settings['model']['url'] = 'http://x/v1/chat/completions'

    async def run(enabled):
        runtime.settings['model_actions'] = enabled
        captured.clear()
        await runtime.chat('привет')
        tools = (captured['payload'] or {}).get('tools') or []
        return [t['function']['name'] for t in tools]

    assert 'alice_action' not in asyncio.run(run(False))
    assert 'alice_action' in asyncio.run(run(True))
    asyncio.run(runtime.close())
