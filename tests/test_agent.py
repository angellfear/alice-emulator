"""Движок graph (server_emulator/agent): треды, классы риска, подтверждение, сценарии, лимит."""
import json

import pytest

from server_emulator import mcp_hub
from server_emulator.agent import policy
from server_emulator.runtime import Runtime


def home(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u', 'name': 'Аня'})
    runtime.identities.mutate({'operation': 'user', 'id': 'v', 'name': 'Вова'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN1', 'user_id': 'u', 'device_ids': ['d1'],
                               'name': 'Кухня', 'room': 'кухня'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN2', 'user_id': 'v', 'device_ids': ['d2'], 'name': 'Спальня'})
    runtime.settings['model']['stub_text'] = ''
    runtime.settings['device_context'] = {'sn': 'SN1', 'user_id': 'u'}
    return runtime


def fake(runtime, replies, calls=None):
    calls = [] if calls is None else calls

    async def provider(kind, payload):
        calls.append(json.loads(json.dumps(payload)))
        message = replies[min(len(calls), len(replies)) - 1]
        return json.dumps({'choices': [{'message': message}], 'usage': {'total_tokens': 1}}).encode(), 'application/json'
    runtime.provider = provider
    return calls


def tool_call(name, arguments='{}', call_id='c1'):
    return {'content': '', 'tool_calls': [{'id': call_id, 'type': 'function',
                                           'function': {'name': name, 'arguments': arguments}}]}


@pytest.fixture
def lock(monkeypatch):
    """Внешний инструмент класса external на сервере ha; вызовы сервера — без сети."""
    called = []

    async def request(runtime, server, method, params, timeout=30, server_id=None):
        called.append(params)
        return {'content': [{'type': 'text', 'text': 'locked'}], 'isError': False}
    monkeypatch.setattr(mcp_hub, 'request', request)

    def setup(runtime):
        mcp_hub.save_config(runtime, {'mcpServers': {'ha': {'type': 'http', 'url': 'http://ha.invalid/mcp'}}})
        runtime.settings['tools'] = [{'name': 'ha__lock', 'server_id': 'ha', 'server_tool': 'lock', 'enabled': True,
                                      'risk': 'external', 'description': 'lock',
                                      'inputSchema': {'type': 'object', 'properties': {}}}]
        return called
    return setup


def tool_messages(payload):
    return [m['content'] for m in payload['messages'] if m['role'] == 'tool']


async def test_history_persists_in_thread_and_restart(tmp_path):
    runtime = home(tmp_path)
    calls = fake(runtime, [{'content': 'Привет'}, {'content': 'Снова'}, {'content': 'Другой'}, {'content': 'Ещё'}])
    await runtime.chat('здравствуй', thread='t1')
    await runtime.chat('как дела', thread='t1')
    texts = [m['content'] for m in calls[1]['messages']]
    assert 'здравствуй' in texts and 'Привет' in texts
    await runtime.chat('кто ты', thread='t2')
    assert 'здравствуй' not in [m['content'] for m in calls[2]['messages']]
    await runtime.close()
    restarted = home(tmp_path)
    calls = fake(restarted, [{'content': 'Помню'}])
    await restarted.chat('что я говорил', thread='t1')
    assert 'как дела' in [m['content'] for m in calls[0]['messages']]
    await restarted.close()


async def test_reset_clears_thread(tmp_path):
    runtime = home(tmp_path)
    calls = fake(runtime, [{'content': 'Привет'}])
    await runtime.chat('здравствуй', thread='t1')
    await runtime.agent.reset(runtime, 't1')
    await runtime.chat('снова', thread='t1')
    assert [m['role'] for m in calls[-1]['messages']] == ['system', 'user']
    await runtime.close()


async def test_external_tool_waits_for_confirmation_then_yes(tmp_path, lock):
    runtime = home(tmp_path)
    called = lock(runtime)
    fake(runtime, [tool_call('ha__lock'), {'content': 'Закрыла'}])
    answer = await runtime.chat('закрой замок', thread='t1')
    assert answer['tool_calls'][0]['id'] == 'c1' and answer['tool_calls'][0]['thread_id'] == 't1'
    assert called == [] and 'да' in answer['text']
    assert await runtime.agent.has_pending(runtime, 't1')
    answer = await runtime.chat('да', thread='t1')
    assert answer['text'] == 'Закрыла' and len(called) == 1 and answer['tool_calls'] == []
    assert not await runtime.agent.has_pending(runtime, 't1')
    await runtime.close()


async def test_no_declines(tmp_path, lock):
    runtime = home(tmp_path)
    called = lock(runtime)
    calls = fake(runtime, [tool_call('ha__lock'), {'content': 'Не буду'}])
    await runtime.chat('закрой замок', thread='t1')
    answer = await runtime.chat('нет', thread='t1')
    assert called == [] and answer['text'] == 'Не буду'
    assert any('отказался' in text for text in tool_messages(calls[-1]))
    await runtime.close()


async def test_other_reply_passes_text_to_model(tmp_path, lock):
    runtime = home(tmp_path)
    called = lock(runtime)
    calls = fake(runtime, [tool_call('ha__lock'), {'content': 'Открываю окно'}])
    await runtime.chat('закрой замок', thread='t1')
    await runtime.chat('лучше открой окно', thread='t1')
    assert called == [] and any('лучше открой окно' in text for text in tool_messages(calls[-1]))
    await runtime.close()


async def test_panel_resume_approve_and_reject(tmp_path, lock):
    runtime = home(tmp_path)
    called = lock(runtime)
    fake(runtime, [tool_call('ha__lock'), {'content': 'Закрыла'}, tool_call('ha__lock', call_id='c2'), {'content': 'Ок'}])
    await runtime.chat('закрой замок', thread='t1')
    answer = await runtime.agent.resume(runtime, 't1', approve={'c1'})
    assert answer['text'] == 'Закрыла' and len(called) == 1
    await runtime.chat('и ещё раз', thread='t1')
    await runtime.agent.resume(runtime, 't1')  # без approve — отклонение
    assert len(called) == 1
    with pytest.raises(ValueError):
        await runtime.agent.resume(runtime, 't1')
    await runtime.close()


async def test_read_tool_runs_without_confirmation(tmp_path):
    runtime = home(tmp_path)
    fake(runtime, [tool_call('speakers_list'), {'content': 'ok'}])
    answer = await runtime.chat('какие колонки', thread='t1')
    assert answer['tool_calls'] == [] and answer['tool_runs'][0]['name'] == 'speakers_list'
    assert 'SN1' in answer['tool_runs'][0]['result'] and 'SN2' not in answer['tool_runs'][0]['result']
    await runtime.close()


def test_policy_risks(tmp_path):
    runtime = home(tmp_path)
    assert policy.tool_risk(runtime, 'speakers_list') == 'read'
    assert policy.tool_risk(runtime, 'speaker_command') == 'device'
    assert policy.tool_risk(runtime, 'alice_action') == 'device'
    assert policy.tool_risk(runtime, 'scenario_list') == 'read'
    runtime.settings['tools'] = [{'name': 'a', 'server_id': 's', 'mode': 'auto'},
                                 {'name': 'b', 'server_id': 's', 'mode': 'confirm'},
                                 {'name': 'c', 'server_id': 's', 'mode': 'auto', 'risk': 'config'},
                                 {'name': 'd'}]
    assert [policy.tool_risk(runtime, n) for n in 'abcd'] == ['device', 'external', 'config', 'external']
    assert policy.mode_of('config') == 'confirm' and policy.mode_of('device') == 'auto'


async def test_scenario_tools_offered_and_index_in_prompt(tmp_path):
    runtime = home(tmp_path)
    calls = fake(runtime, [{'content': 'ok'}])
    await runtime.chat('что ты умеешь', thread='t1')
    names = {t['function']['name'] for t in calls[0]['tools']}
    assert {'scenario_list', 'scenario_get', 'scenario_probe', 'scenario_run'} <= names
    system = calls[0]['messages'][0]['content']
    assert 'Сценарии' in system and 'smart_home.light' in system
    await runtime.close()


async def test_scenario_run_light_without_confirmation(tmp_path):
    runtime = home(tmp_path)
    device = next(d for d in runtime.identities.data['devices'] if d['sn'] == 'SN1')
    runtime.identities.mutate({'operation': 'state', 'sn': 'SN1',
                               'state': dict(device['state'], light={'кухня': {'on': False}})})
    arguments = json.dumps({'text': 'включи свет на кухне'}, ensure_ascii=False)
    fake(runtime, [tool_call('scenario_run', arguments), {'content': 'Включила'}])
    answer = await runtime.chat('сделай светлее на кухне', thread='t1')
    assert answer['tool_calls'] == [] and answer['text'] == 'Включила'
    run = answer['tool_runs'][0]
    assert run['name'] == 'scenario_run' and json.loads(run['result'])['status'] == 'finished'
    device = next(d for d in runtime.identities.data['devices'] if d['sn'] == 'SN1')
    assert device['state']['light']['кухня']['on'] is True
    await runtime.close()


async def test_scenario_with_external_risk_needs_confirmation(tmp_path):
    runtime = home(tmp_path)
    runtime.settings['scenarios'] = [{
        'format_version': 2, 'id': 'user.garage', 'version': '1.0.0', 'risk': 'external', 'enabled': True,
        'triggers': [{'kind': 'phrase', 'phrases': ['открой гараж']}],
        'flow': [{'id': 'say', 'op': 'compose', 'reply': {'text': 'Гараж открыт.'}, 'next': 'done'},
                 {'id': 'done', 'op': 'finish'}]}]
    arguments = json.dumps({'text': 'открой гараж', 'id': 'user.garage'}, ensure_ascii=False)
    fake(runtime, [tool_call('scenario_run', arguments), {'content': 'Открыла'}])
    answer = await runtime.chat('пусти машину', thread='t1')
    assert answer['tool_calls'][0]['function']['name'] == 'scenario_run' and answer['tool_runs'] == []
    answer = await runtime.chat('да', thread='t1')
    assert answer['text'] == 'Открыла' and json.loads(answer['tool_runs'][0]['result'])['status'] == 'finished'
    await runtime.close()


async def test_model_call_limit(tmp_path):
    runtime = home(tmp_path)
    calls = fake(runtime, [tool_call('speakers_list')])
    answer = await runtime.chat('колонки', thread='t1')
    assert answer['text'] == 'Не получилось завершить действие: слишком много шагов.' and len(calls) <= 6
    await runtime.close()


async def test_engine_flag(tmp_path, monkeypatch):
    runtime = home(tmp_path)
    fake(runtime, [{'content': 'ok'}])
    assert (await runtime.chat('привет'))['engine'] == 'graph'
    monkeypatch.setenv('EMULATOR_AGENT_ENGINE', 'legacy')
    answer = await runtime.chat('привет')
    assert answer['text'] == 'ok' and 'thread' not in answer
    await runtime.close()


async def test_panel_tool_call_confirms_agent_call(tmp_path, lock):
    from server_emulator.ui.api.mcp import _agent_decision
    runtime = home(tmp_path)
    called = lock(runtime)
    fake(runtime, [tool_call('ha__lock'), {'content': 'Закрыла'}])
    answer = await runtime.chat('закрой замок', thread='t1')
    runtime.pending_calls = list(answer['tool_calls'])  # как студия и голосовой тракт
    assert await _agent_decision(runtime, {'call_id': 'nope'}) is None  # чужой id — обычный tool-call
    result = await _agent_decision(runtime, {'call_id': 'c1'})
    assert result['answer'] == 'Закрыла' and result['content'][0]['text'] == 'locked' and len(called) == 1
    assert runtime.pending_calls == []
    await runtime.close()
