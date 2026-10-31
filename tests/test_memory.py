"""Долговременная память агента (agent/memory.py): подстановка в промпт, инструменты,
фоновая рефлексия LangMem, эмбеддинги и переиндексация, API вкладки «Память»."""
import json

import pytest
from aiohttp import web

from server_emulator.agent import memory
from server_emulator.runtime import Runtime
from server_emulator.ui.api.memory import memory_api
from tests.conftest import listen


def home(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u', 'name': 'Аня'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN1', 'user_id': 'u', 'device_ids': ['d1'], 'name': 'Кухня'})
    runtime.settings['model']['stub_text'] = ''
    runtime.settings['device_context'] = {'sn': 'SN1', 'user_id': 'u'}
    return runtime


def fake(runtime, chat_replies, fact='Пользователь любит джаз'):
    """Провайдер: рефлексии (инструменты Fact/Episode) отвечает вызовом, диалогу — по списку."""
    calls = []

    async def provider(kind, payload):
        calls.append(json.loads(json.dumps(payload)))
        names = [t['function']['name'] for t in payload.get('tools', [])]
        if 'Fact' in names or 'Episode' in names:
            name = 'Fact' if 'Fact' in names else 'Episode'
            message = {'content': '', 'tool_calls': [{'id': 'r%d' % len(calls), 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps({'text': fact if name == 'Fact' else 'Говорили о музыке'},
                                                      ensure_ascii=False)}}]}
        else:
            dialog = [c for c in calls if not any(t['function']['name'] in ('Fact', 'Episode', 'PatchDoc')
                                                  for t in c.get('tools', []))]
            message = chat_replies[min(len(dialog), len(chat_replies)) - 1]
        return json.dumps({'choices': [{'message': message}], 'usage': {}}).encode(), 'application/json'
    runtime.provider = provider
    return calls


def tool_call(name, arguments, call_id='c1'):
    return {'content': '', 'tool_calls': [{'id': call_id, 'type': 'function',
                                           'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]}


async def stored(runtime, kind='facts', query=None):
    async with memory.open_store(runtime, runtime.agent.path) as store:
        return await memory.items(store, 'u', kind, query)


async def test_recall_puts_memory_into_system_prompt(tmp_path):
    runtime = home(tmp_path)
    async with memory.open_store(runtime, runtime.agent.path) as store:
        await memory.put(store, 'u', 'facts', 'Кота зовут Барсик')
        await memory.put(store, 'u', 'instructions', 'Отвечай на «ты»')
    calls = fake(runtime, [{'content': 'Барсик'}])
    await runtime.chat('как зовут кота', thread='t1')
    system = calls[0]['messages'][0]['content']
    assert 'Кота зовут Барсик' in system and 'Отвечай на «ты»' in system
    names = {t['function']['name'] for t in calls[0]['tools']}
    assert {'manage_memory', 'search_memory', 'memory_instructions'} <= names
    await runtime.close()


async def test_no_user_no_memory(tmp_path):
    runtime = home(tmp_path)
    runtime.settings['device_context'] = {}
    calls = fake(runtime, [{'content': 'ok'}])
    await runtime.chat('привет', thread='t1')
    assert 'manage_memory' not in {t['function']['name'] for t in calls[0]['tools']}
    await runtime.close()


async def test_remember_on_request_without_confirmation(tmp_path):
    runtime = home(tmp_path)
    fake(runtime, [tool_call('manage_memory', {'content': 'Пользователь любит джаз', 'action': 'create'}),
                   {'content': 'Запомнила'}])
    answer = await runtime.chat('запомни, я люблю джаз', thread='t1')
    assert answer['text'] == 'Запомнила' and answer['tool_calls'] == []
    assert [i['text'] for i in await stored(runtime)] == ['Пользователь любит джаз']
    await runtime.close()


async def test_instructions_need_confirmation(tmp_path):
    runtime = home(tmp_path)
    fake(runtime, [tool_call('memory_instructions', {'text': 'Отвечай коротко'}), {'content': 'Хорошо'}])
    answer = await runtime.chat('отвечай короче', thread='t1')
    assert answer['tool_calls'][0]['function']['name'] == 'memory_instructions'
    assert await stored(runtime, 'instructions') == []
    await runtime.chat('да', thread='t1')
    assert [i['text'] for i in await stored(runtime, 'instructions')] == ['Отвечай коротко']
    await runtime.close()


async def test_reflection_extracts_facts_without_tool_results(tmp_path):
    runtime = home(tmp_path)
    calls = fake(runtime, [tool_call('speakers_list', {}), {'content': 'Запомню, что вы любите джаз'}])
    await runtime.chat('я люблю джаз', thread='voice:SN1')
    tool_result = next(m['content'] for m in calls[1]['messages'] if m['role'] == 'tool')
    record = await runtime.agent.reflector.reflect(runtime, 'voice:SN1', 'u')
    assert record.get('facts') == 1 and 'error' not in record
    reflection = [c for c in calls if any(t['function']['name'] == 'Fact' for t in c.get('tools', []))]
    assert reflection and tool_result not in json.dumps(reflection[0]['messages'], ensure_ascii=False)
    assert [i['text'] for i in await stored(runtime)] == ['Пользователь любит джаз']
    assert [i['text'] for i in await stored(runtime, 'episodes')] == ['Говорили о музыке']
    again = await runtime.agent.reflector.reflect(runtime, 'voice:SN1', 'u')
    assert again.get('skipped')  # новых реплик нет — модель не вызывается
    await runtime.close()


async def test_reflection_scheduled_after_pause(tmp_path):
    runtime = home(tmp_path)
    runtime.settings['memory'] = {**runtime.settings['memory'], 'reflect_after_s': 0}
    fake(runtime, [{'content': 'Хорошо'}])
    await runtime.chat('я люблю джаз', thread='voice:SN1')
    await runtime.agent.reflector.tasks['voice:SN1']
    assert runtime.agent.reflector.log[-1].get('facts') == 1
    await runtime.close()


@pytest.fixture
async def embeddings():
    """Сервер эмбеддингов: вектор — есть ли в тексте «музык»/«джаз» и «кот»."""
    seen = []

    async def handler(request):
        body = await request.json()
        seen.append(body['model'])
        vectors = [[1.0 if ('джаз' in t or 'музык' in t) else 0.0, 1.0 if 'кот' in t.lower() else 0.0, 0.1]
                   + ([0.0] if body['model'] == 'big' else []) for t in body['input']]
        return web.json_response({'data': [{'index': i, 'embedding': v} for i, v in enumerate(vectors)]})
    app = web.Application()
    app.router.add_post('/v1/embeddings', handler)
    runner, url = await listen(app)
    yield url + '/v1/embeddings', seen
    await runner.cleanup()


async def test_semantic_recall_and_reindex_on_model_change(tmp_path, embeddings):
    url, seen = embeddings
    runtime = home(tmp_path)
    runtime.settings['memory'] = {**runtime.settings['memory'], 'embed': {'url': url, 'model': 'small', 'api_key': ''},
                                  'recall_limit': 1}
    async with memory.open_store(runtime, runtime.agent.path) as store:
        await memory.put(store, 'u', 'facts', 'Пользователь любит джаз')
        await memory.put(store, 'u', 'facts', 'Кота зовут Барсик')
    assert (await stored(runtime, query='как зовут кота'))[0]['text'] == 'Кота зовут Барсик'
    calls = fake(runtime, [{'content': 'ok'}])
    await runtime.chat('какую музыку я люблю', thread='t1')
    system = calls[0]['messages'][0]['content']
    assert 'джаз' in system and 'Барсик' not in system  # recall_limit=1, ближайший к реплике
    runtime.settings['memory']['embed']['model'] = 'big'  # другая размерность — переиндексация
    assert (await stored(runtime, query='как зовут кота'))[0]['text'] == 'Кота зовут Барсик'
    assert seen.count('big') >= 2
    await runtime.close()


async def test_embeddings_down_memory_still_works(tmp_path):
    runtime = home(tmp_path)
    runtime.settings['memory'] = {**runtime.settings['memory'],
                                  'embed': {'url': 'http://127.0.0.1:9/v1/embeddings', 'model': 'x', 'api_key': ''}}
    async with memory.open_store(runtime, runtime.agent.path) as store:
        await memory.put(store, 'u', 'facts', 'Кота зовут Барсик')
    calls = fake(runtime, [{'content': 'ok'}])
    await runtime.chat('привет', thread='t1')
    assert 'Барсик' in calls[0]['messages'][0]['content']
    assert any(e.get('kind') == 'memory' for e in runtime.recent)
    await runtime.close()


class FakeRequest:
    def __init__(self, method, value=None, query=None):
        self.method, self.value, self.query = method, value, query or {}

    async def json(self):
        return self.value


async def call_api(runtime, method, value=None, query=None):
    response = await memory_api(FakeRequest(method, value, query), runtime, None)
    return json.loads(response.body)


async def test_memory_api_crud_forget_and_masked_key(tmp_path):
    runtime = home(tmp_path)
    key = (await call_api(runtime, 'POST', {'op': 'put', 'user': 'u', 'kind': 'facts', 'text': 'Любит джаз'}))['key']
    await call_api(runtime, 'POST', {'op': 'put', 'user': 'u', 'kind': 'facts', 'text': 'Кот Барсик'})
    view = await call_api(runtime, 'GET', query={'user': 'u'})
    assert view['available'] and len(view['items']['facts']) == 2 and view['users'][0]['id'] == 'u'
    await call_api(runtime, 'POST', {'op': 'delete', 'user': 'u', 'kind': 'facts', 'key': key})
    assert [i['text'] for i in (await call_api(runtime, 'GET', query={'user': 'u'}))['items']['facts']] == ['Кот Барсик']
    assert (await call_api(runtime, 'POST', {'op': 'forget', 'user': 'u'}))['removed'] == 1
    with pytest.raises(ValueError):
        await call_api(runtime, 'POST', {'op': 'put', 'user': 'ghost', 'kind': 'facts', 'text': 'x'})
    await call_api(runtime, 'POST', {'op': 'settings', 'enabled': True, 'recall_limit': 4, 'reflect_after_s': 60,
                                     'embed': {'url': 'http://h/v1/embeddings', 'model': 'm', 'api_key': 'secret'}})
    view = await call_api(runtime, 'GET', query={'user': 'u'})
    assert view['settings']['embed']['api_key'] == '__KEEP__' and view['settings']['recall_limit'] == 4
    await call_api(runtime, 'POST', {'op': 'settings', 'embed': {'url': 'http://h/v1/embeddings', 'model': 'm2',
                                                                 'api_key': '__KEEP__'}})
    assert runtime.settings['memory']['embed'] == {'url': 'http://h/v1/embeddings', 'model': 'm2', 'api_key': 'secret'}
    assert runtime.public()['memory']['embed']['api_key'] == '__KEEP__'
    await runtime.close()
