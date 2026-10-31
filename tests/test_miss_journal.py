"""Журналы «Без ответа» (server_emulator/miss_journal.py): фразы, AAPI, колбэки."""
from server_emulator import miss_journal
from server_emulator.miss_journal import MissJournal, exact


def test_groups_by_normalized_phrase(tmp_path):
    misses = MissJournal(tmp_path / 'm.json')
    misses.note('Расскажи анекдот!', sn='SN1')
    misses.note('расскажи  анекдот', sn='SN2')
    [item] = misses.public()
    assert item['key'] == 'расскажи анекдот' and item['count'] == 2
    assert item['examples'] == ['Расскажи анекдот!', 'расскажи  анекдот'] and item['sns'] == ['SN1', 'SN2']
    assert MissJournal(tmp_path / 'm.json').public()[0]['count'] == 2  # пережило перезапуск


def test_limit_evicts_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(miss_journal, 'LIMIT', 3)
    misses = MissJournal(tmp_path / 'm.json')
    for i in range(5):
        misses.note(f'фраза {i}')
        misses.items[f'фраза {i}']['last_ts'] = i
    assert sorted(misses.items) == ['фраза 2', 'фраза 3', 'фраза 4']


def test_forget_and_empty(tmp_path):
    misses = MissJournal(tmp_path / 'm.json')
    misses.note('?!')
    assert misses.public() == []
    misses.note('один'); misses.note('два')
    misses.forget('один')
    assert [m['key'] for m in misses.public()] == ['два']
    misses.forget()
    assert misses.public() == []


def test_exact_key_keeps_names_and_examples(tmp_path):
    journal = MissJournal(tmp_path / 'c.json', key=exact)
    journal.note('@@mm_typed_callback', sn='SN1', example='{"purpose": "x"}')
    journal.note('@@mm_typed_callback', example='y' * 1000)
    [item] = journal.public()
    assert item['key'] == '@@mm_typed_callback' and item['count'] == 2
    assert item['examples'] == ['{"purpose": "x"}', 'y' * 600]


async def test_unanswered_aapi_message_queued_and_api(tmp_path):
    """WS AAPI-сообщение без обработчика (Ping) — в очереди; Ack — нет (на него не отвечают)."""
    import asyncio

    from aiohttp import ClientSession

    from server_emulator.runtime import Runtime
    from server_emulator.server import create_app
    from tests.conftest import listen, types

    runtime = Runtime(tmp_path)
    client_type, _ = types()
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url + '/uni.ws') as ws:
                for kind in ('Ping', 'Ack'):
                    message = client_type()
                    message.Event.Header.MessageId = kind.lower()
                    getattr(message.Event, kind).SetInParent()
                    await ws.send_bytes(b'AAPI' + message.SerializeToString())
                for _ in range(50):
                    if runtime.aapi_misses.public():
                        break
                    await asyncio.sleep(0.02)
            assert [m['key'] for m in runtime.aapi_misses.public()] == ['Ping']
            assert 'Ping' in runtime.aapi_misses.public()[0]['examples'][0]
            runtime.callback_misses.note('@@mm_typed_callback', example='{}')
            headers = {'X-Emulator': '1'}
            async with client.get(url + '/emulator/api/misses', headers=headers) as response:
                data = await response.json()
            assert [m['key'] for m in data['aapi']] == ['Ping']
            assert [m['key'] for m in data['callbacks']] == ['@@mm_typed_callback']
            async with client.delete(url + '/emulator/api/misses?kind=aapi&key=*', headers=headers) as response:
                assert response.status == 200
            assert runtime.aapi_misses.public() == []
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_callback_without_scenario_queued(tmp_path):
    """Служебный колбэк server_action без protocol_event-сценария — в журнале колбэков, не во фразах."""
    import asyncio

    from server_emulator.runtime import Runtime
    from server_emulator.voice import Conversation
    from tests.conftest import types

    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, intelligence='scenarios')
    client, server = types()

    async def send(data):
        pass
    conv = Conversation(runtime, server, send, trace_context={'sn': 'SN-T'})
    incoming = client()
    incoming.Event.Header.MessageId = 'm1'
    incoming.Event.TextInput.Header.RequestId = 'r1'
    incoming.Event.TextInput.Request.Event.Type = 6
    incoming.Event.TextInput.Request.Event.Name = '@@future_callback'
    try:
        await conv.receive(incoming, 'TextInput')
        await asyncio.wait_for(conv.task, 10)
        [item] = runtime.callback_misses.public()
        assert item['key'] == '@@future_callback' and item['sns'] == ['SN-T']
        assert runtime.phrase_misses.public() == []
    finally:
        await conv.shutdown()
        await runtime.close()
