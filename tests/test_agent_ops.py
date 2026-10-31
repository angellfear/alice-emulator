import json
import time

from aiohttp import ClientSession

from server_emulator.agent import ops
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen


def _make_runtime(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings['model']['stub_text'] = ''
    received = []

    async def provider(kind, payload):
        received.append(payload)
        body = {'choices': [{'message': {'content': 'ok'}}], 'usage': {'total_tokens': 0}}
        return json.dumps(body, ensure_ascii=False).encode(), 'application/json'

    runtime.provider = provider
    return runtime, received


async def test_run_is_traced_with_usage(tmp_path):
    runtime, received = _make_runtime(tmp_path)

    async def provider(kind, payload):
        received.append(payload)
        body = {'choices': [{'message': {'content': 'Привет'}}], 'usage': {'total_tokens': 7}}
        return json.dumps(body, ensure_ascii=False).encode(), 'application/json'

    runtime.provider = provider

    answer = await runtime.chat('привет', thread='voice:SN1')
    assert answer['text'] == 'Привет'
    assert len(received) == 1

    rows = runtime.agent.ops.runs()
    assert len(rows) == 1
    row = rows[0]
    assert row['kind'] == 'request'
    assert row['thread'] == 'voice:SN1'
    assert row['model_calls'] == 1
    assert row['tokens'] == 7
    assert row['answer'] == 'Привет'
    assert row['status'] == 'ok'
    await runtime.close()


async def test_daily_budget_blocks_model(tmp_path):
    runtime, received = _make_runtime(tmp_path)
    runtime.settings['agent']['daily_model_calls'] = 1

    async def provider(kind, payload):
        received.append(payload)
        body = {'choices': [{'message': {'content': 'Ответ'}}], 'usage': {'total_tokens': 5}}
        return json.dumps(body, ensure_ascii=False).encode(), 'application/json'

    runtime.provider = provider

    first = await runtime.chat('привет', thread='voice:SN1')
    assert first['text'] == 'Ответ'

    second = await runtime.chat('ещё раз', thread='voice:SN1')
    assert second['text'] == ops.BUDGET_TEXT
    assert len(received) == 1  #_provider не вызывался второй раз

    rows = runtime.agent.ops.runs()
    assert rows[0]['status'] == 'budget'  # последний прогон
    assert rows[1]['status'] == 'ok'
    await runtime.close()


async def test_secret_redacted_before_provider(tmp_path):
    runtime, received = _make_runtime(tmp_path)
    secret = 'sk-abcdefghijklmnopqrstuv1234'

    async def provider(kind, payload):
        received.append(payload)
        body = {'choices': [{'message': {'content': 'запомнил'}}], 'usage': {'total_tokens': 1}}
        return json.dumps(body, ensure_ascii=False).encode(), 'application/json'

    runtime.provider = provider

    await runtime.chat(f'мой ключ {secret} запомни', thread='voice:SN1')
    assert received, 'провайдер должен быть вызван'
    dumped = json.dumps(received, ensure_ascii=False)
    assert secret not in dumped
    await runtime.close()


async def test_misses_grouped(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings['model']['stub_text'] = ''
    now = time.time()
    for text in ('Включи гирлянду!', 'включи гирлянду', 'включи  гирлянду'):
        runtime.agent.ops.record({'ts': now, 'thread': 'voice:SN1', 'kind': 'request', 'text': text})
    runtime.agent.ops.record({'ts': now, 'thread': 'studio:x', 'kind': 'request', 'text': 'включи гирлянду'})

    groups = runtime.agent.ops.misses(min_count=3)
    assert len(groups) == 1
    assert groups[0]['phrase'] == 'включи гирлянду'
    assert groups[0]['count'] == 3
    await runtime.close()


async def test_event_webhook_requires_token(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings['model']['stub_text'] = ''
    runtime.settings['agent']['webhook_token'] = 't0k'

    async def provider(kind, payload):
        body = {'choices': [{'message': {'content': 'Дверь открыта, проверьте'}}],
                'usage': {'total_tokens': 3}}
        return json.dumps(body, ensure_ascii=False).encode(), 'application/json'

    runtime.provider = provider

    runtime.identities.mutate({'operation': 'user', 'id': 'u', 'name': 'Аня'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN1', 'user_id': 'u',
                               'device_ids': ['d1'], 'name': 'Кухня'})

    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(
                    url + '/emulator/api/agent-event',
                    json={'text': 'дверь открыта', 'sn': 'SN1', 'delivery': 'inbox'},
                    headers={'X-Emulator': '1'}) as response:
                assert response.status == 403

            async with client.post(
                    url + '/emulator/api/agent-event',
                    json={'text': 'дверь открыта', 'sn': 'SN1', 'delivery': 'inbox'},
                    headers={'X-Emulator': '1', 'X-Agent-Token': 't0k'}) as response:
                assert response.status == 200
                body = await response.json()
                assert body['status'] == 'finished'
                assert body['delivered'] == 'inbox'

        inbox = runtime.routines.inbox('u')
        assert len(inbox) == 1
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_routing_evals_pass(tmp_path):
    """Набор evals без модели: встроенные сценарии берут свои фразы (в т.ч. «забудь всё»,
    «сколько сейчас времени» — ё и разглей по phrases[]), болтовня уходит агенту."""
    import io
    from server_emulator.agent import evals
    out = io.StringIO()
    passed, total = await evals.run(tmp_path, llm=False, out=out)
    assert total >= 10 and passed == total, out.getvalue()


async def test_agent_settings_api_toggle_and_token(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u', 'name': 'Аня'})
    runner, url = await listen(create_app(runtime=runtime))
    headers = {'X-Emulator': '1'}
    api = url + '/emulator/api/agent-ops'
    try:
        async with ClientSession() as client:
            async with client.get(api, headers=headers) as response:
                assert response.status == 200
                data = await response.json()
                assert data['settings']['enabled'] is True
                assert data['settings']['daily_model_calls'] == 0

            async with client.post(
                    api, headers=headers,
                    json={'op': 'settings',
                          'settings': {'enabled': False, 'daily_model_calls': 50,
                                       'budgets': {'u': 5, 'ghost': 9},
                                       'webhook_token': 'secret-token',
                                       'suggest_cron': ''}}) as response:
                assert response.status == 200

            assert runtime.settings['intelligence'] == 'model'  # тумблер = режим «Интеллект»
            assert runtime.settings['agent']['daily_model_calls'] == 50
            assert runtime.settings['agent']['budgets'] == {'u': 5}
            assert runtime.settings['agent']['webhook_token'] == 'secret-token'

            async with client.get(api, headers=headers) as response:
                assert response.status == 200
                text = await response.text()
                assert 'secret-token' not in text
                data = await response.json()
                assert data['settings']['enabled'] is False

            async with client.post(
                    api, headers=headers,
                    json={'op': 'settings',
                          'settings': {'enabled': True, 'webhook_token': '__KEEP__'}}) as response:
                assert response.status == 200

            assert runtime.settings['intelligence'] == 'agent'
            assert runtime.settings['agent']['webhook_token'] == 'secret-token'

            async with client.post(
                    api, headers=headers,
                    json={'op': 'settings', 'settings': {'suggest_cron': 'not a cron'}}) as response:
                assert response.status == 400
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_disabled_agent_stops_routines_and_events(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings['model']['stub_text'] = ''
    runtime.settings['agent']['engine'] = 'legacy'
    runtime.settings['agent']['webhook_token'] = 't0k'

    runtime.identities.mutate({'operation': 'user', 'id': 'u', 'name': 'Аня'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN1', 'user_id': 'u',
                               'device_ids': ['d1'], 'name': 'Кухня'})

    calls = []

    async def provider(kind, payload):
        calls.append(payload)
        body = {'choices': [{'message': {'content': 'ok'}}], 'usage': {'total_tokens': 0}}
        return json.dumps(body, ensure_ascii=False).encode(), 'application/json'

    runtime.provider = provider

    routine = runtime.routines.save({
        'title': 'Утро',
        'owner': 'u',
        'sn': 'SN1',
        'kind': 'agent',
        'prompt': 'расскажи погоду',
        'schedule': {'kind': 'cron', 'expr': '0 8 * * *'},
        'delivery': 'inbox',
    })

    result = await runtime.routines.fire(routine['id'], manual=True)
    assert result['status'] == 'skipped_disabled'
    assert calls == []

    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(
                    url + '/emulator/api/agent-event',
                    json={'text': 'дверь открыта', 'sn': 'SN1'},
                    headers={'X-Emulator': '1', 'X-Agent-Token': 't0k'}) as response:
                assert response.status == 503

            async with client.get(url + '/emulator/api/agent-ops?brief=1',
                                  headers={'X-Emulator': '1'}) as response:
                assert response.status == 200
                data = await response.json()
                assert data == {'settings': {'enabled': False}}
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_cancelled_request_is_traced(tmp_path):
    """Колонка отменила запрос, пока модель думала, — прогон в журнале со статусом cancelled."""
    import asyncio
    runtime, received = _make_runtime(tmp_path)

    async def provider(kind, payload):
        await asyncio.sleep(30)
    runtime.provider = provider
    task = asyncio.create_task(runtime.chat('расскажи сказку', thread='voice:SN1'))
    await asyncio.sleep(0.5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    rows = runtime.agent.ops.runs()
    assert [r['status'] for r in rows] == ['cancelled'] and rows[0]['text'] == 'расскажи сказку'
    await runtime.close()
