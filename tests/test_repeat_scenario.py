"""Сценарии system.repeat и system.history: «повтори», «ещё раз», «повтори N»,
«история», «история N».

Журнал реплик ведёт голосовой тракт (runtime.note_said при доставке), сценарии
читают его через SDK ctx.said. Ответ — карточка с нумерованным списком,
от новых к старым.
"""
import asyncio
from pathlib import Path

from server_emulator.runtime import Runtime
from server_emulator.schema import load_type
from server_emulator.voice import Conversation
from tests.conftest import types

ROOT = Path(__file__).resolve().parents[1] / 'server_emulator' / 'schemas'

RESET_TEXT = 'Готово, начинаем с чистого листа.'  # system.dialog.reset
NOTHING = 'Я пока ничего не говорила, повторять нечего.'


def reply_texts(sent):
    server_type = load_type(ROOT, 'alice-api.pb', 'NAlice.NAliceApi.TServerMessage')
    texts = []
    for blob in sent:
        if not blob.startswith(b'AAPI'):
            continue
        message = server_type.FromString(blob[4:])
        if message.Event.WhichOneof('Event') != 'AliceResponse':
            continue
        response = message.Event.AliceResponse
        text = response.Response.Cards[0].Text if response.Response.Cards else ''
        texts.append(text or response.VoiceResponse.OutputSpeech.Text)
    return texts


def _model_stub(runtime):
    """TextInput обслуживается только при aapi_model; фразы-промахи уходят в заглушку."""
    runtime.settings['aapi_model'] = True

    async def chat(text, history=None, **context):
        return {'text': 'модель', 'tool_calls': []}

    runtime.chat = chat


async def _turn(runtime, text, sn='SN-R'):
    sent = []

    async def send(data):
        sent.append(data)

    client_type, server_type = types()
    session = Conversation(runtime, server_type, send, trace_context={'sn': sn})
    message = client_type()
    message.Event.Header.MessageId = 'text'
    message.Event.TextInput.Request.Event.Text = text
    await session.receive(message, 'TextInput')
    await asyncio.wait_for(session.task, 3)
    await session.shutdown()
    return reply_texts(sent)


def test_note_said(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.note_said(['Первая часть.', '<speaker audio="x.opus">Вторая.</speaker>'], sn='SN-A')
    runtime.note_said('  Третья  реплика. ', sn='SN-B')
    runtime.note_said('', sn='SN-A')  # пустая реплика не пишется
    runtime.note_said(None, sn='SN-A')
    assert [entry['text'] for entry in runtime.said] == ['Первая часть. Вторая.', 'Третья реплика.']
    assert runtime.said[1]['sn'] == 'SN-B'
    for i in range(120):
        runtime.note_said(str(i))  # журнал ограничен
    assert len(runtime.said) == 100


async def test_repeat_last_message(tmp_path):
    runtime = Runtime(tmp_path)
    _model_stub(runtime)
    assert await _turn(runtime, 'забудь всё') == [RESET_TEXT]
    assert runtime.said == [{'ts': runtime.said[0]['ts'], 'sn': 'SN-R', 'text': RESET_TEXT}]
    assert await _turn(runtime, 'повтори') == ['1. ' + RESET_TEXT]  # карточка с номером
    assert await _turn(runtime, 'ещё раз') == ['1. ' + RESET_TEXT]
    assert await _turn(runtime, 'еще раз') == ['1. ' + RESET_TEXT]  # ASR без «ё» — та же фраза


async def test_history_and_repeat_n(tmp_path):
    runtime = Runtime(tmp_path)
    _model_stub(runtime)
    await _turn(runtime, 'забудь всё')  # фиксированный текст
    await _turn(runtime, 'сколько времени')  # второй, другой текст
    first, second = (entry['text'] for entry in runtime.said)
    assert second != first
    # «история» без числа — 3, в журнале пока 2: от новых к старым
    assert await _turn(runtime, 'история') == ['1. %s\n2. %s' % (second, first)]
    # «история 2» и «повтори 2» — последние 2 записи журнала; ответ истории
    # стал одной записью (озвучка целиком)
    journal = [entry['text'] for entry in runtime.said]
    assert journal[-1] == '%s %s' % (second, first)
    expected = '1. %s\n2. %s' % (journal[-1], journal[-2])
    assert await _turn(runtime, 'история 2') == [expected]
    journal = [entry['text'] for entry in runtime.said]
    assert await _turn(runtime, 'повтори 2') == ['1. %s\n2. %s' % (journal[-1], journal[-2])]


async def test_repeat_empty_and_other_column(tmp_path):
    runtime = Runtime(tmp_path)
    _model_stub(runtime)
    runtime.note_said('Не для этой колонки.', sn='SN-X')
    assert await _turn(runtime, 'повтори') == [NOTHING]  # чужая запись не повторяется

    runtime = Runtime(tmp_path)
    _model_stub(runtime)
    assert await _turn(runtime, 'история') == [NOTHING]  # пустой журнал

    runtime = Runtime(tmp_path)
    _model_stub(runtime)
    assert await _turn(runtime, 'повтори 2') == [NOTHING]


async def test_refusal_is_journaled(tmp_path):
    runtime = Runtime(tmp_path)
    _model_stub(runtime)
    assert await _turn(runtime, 'повтори') == [NOTHING]
    # отказ — тоже произнесённая реплика: её тоже можно повторить
    assert await _turn(runtime, 'повтори') == ['1. ' + NOTHING]


async def test_model_reply_recorded(tmp_path):
    runtime = Runtime(tmp_path)

    async def chat(text, history=None, **context):
        return {'text': 'Привет! Чем помочь?', 'tool_calls': []}

    _model_stub(runtime)
    runtime.chat = chat  # ответ модели — его и повторяем
    assert await _turn(runtime, 'просто привет') == ['Привет! Чем помочь?']
    assert runtime.said[-1]['text'] == 'Привет! Чем помочь?'
    assert await _turn(runtime, 'повтори') == ['1. Привет! Чем помочь?']
