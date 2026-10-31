"""Правка сценариев агентом (agent/scenario_edit.py): черновик, JSON Patch, проверка фраз и
перехвата, активация (личная / предложение / администратор), история и откат, инструменты."""
import datetime
import json

import pytest

from server_emulator.agent import scenario_edit
from server_emulator.runtime import Runtime


def home(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u', 'name': 'Аня'})
    runtime.identities.mutate({'operation': 'user', 'id': 'v', 'name': 'Вова'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN1', 'user_id': 'u', 'device_ids': ['d1'], 'name': 'Кухня'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN2', 'user_id': 'v', 'device_ids': ['d2'], 'name': 'Спальня'})
    runtime.settings['model']['stub_text'] = ''
    return runtime


GARLAND = {'id': 'agent.garland', 'title': {'ru': 'Гирлянда'},
           'triggers': [{'kind': 'phrase', 'phrases': ['включи гирлянду']}],
           'flow': [{'id': 'reply', 'op': 'compose', 'reply': {'text': 'Гирлянда включена'}}, {'id': 'done', 'op': 'finish'}]}


async def handled(runtime, text, sn):
    ctx = {'input': {'message_id': 'm', 'request_id': 'r', 'dialog_id': '', 'sn': sn, 'text': text},
           'clock': {'now': datetime.datetime.now()}, 'sequence': 1}
    ctx.update(runtime._scenario_context(sn=sn))
    report = await runtime.dialog.handle_text(text, ctx, 'dlg-' + sn, None, deliver=False)
    return report.get('scenario') if report.get('handled') else None


async def test_builtin_copy_patch_test_activate(tmp_path):
    runtime = home(tmp_path)
    draft = scenario_edit.create(runtime, from_id='smart_home.light', by='user')
    assert draft['scenario']['id'] == 'user.smart_home.light' and draft['disable_builtin'] == 'smart_home.light'
    path = next(f'/triggers/{i}/phrases/-' for i, t in enumerate(draft['scenario']['triggers'])
                if t.get('kind') == 'phrase' and t.get('phrases') is not None)
    draft = scenario_edit.patch(runtime, draft['draft_id'], [{'op': 'add', 'path': path, 'value': 'зажги лампу'}])
    assert draft['error'] is None
    report = scenario_edit.test(runtime, draft['draft_id'], ['зажги лампу на кухне'])
    assert report['results'][0]['scenario'] == 'user.smart_home.light' and report['stolen'] == []
    assert await handled(runtime, 'зажги лампу на кухне', 'SN1') is None  # черновик в роутинг не попадает
    assert (await scenario_edit.activate(runtime, draft['draft_id']))['status'] == 'active'
    assert 'smart_home.light' in runtime.settings['builtin_disabled']
    assert await handled(runtime, 'зажги лампу на кухне', 'SN1') == 'user.smart_home.light'
    await runtime.close()


async def test_invalid_patch_reports_validator_error(tmp_path):
    runtime = home(tmp_path)
    draft = scenario_edit.create(runtime, scenario=GARLAND)
    draft = scenario_edit.patch(runtime, draft['draft_id'], [{'op': 'remove', 'path': '/flow'}])
    assert 'flow' in draft['error']
    assert scenario_edit.test(runtime, draft['draft_id'])['valid'] is False
    with pytest.raises(ValueError):
        await scenario_edit.activate(runtime, draft['draft_id'])
    with pytest.raises(ValueError, match='JSON Patch'):
        scenario_edit.patch(runtime, draft['draft_id'], [{'op': 'replace', 'path': '/nope/x', 'value': 1}])
    await runtime.close()


async def test_stolen_phrases_detected(tmp_path):
    runtime = home(tmp_path)
    draft = scenario_edit.create(runtime, scenario={**GARLAND, 'id': 'agent.greedy', 'priority': 1,
                                                    'triggers': [{'kind': 'phrase', 'phrases': ['свет'], 'match': 'contains'}]})
    report = scenario_edit.test(runtime, draft['draft_id'])
    assert not report['ok'] and any(s['was'] == 'smart_home.light' for s in report['stolen'])
    await runtime.close()


async def test_user_activation_personal_or_proposed(tmp_path):
    runtime = home(tmp_path)
    shared = scenario_edit.create(runtime, scenario=GARLAND, user_id='u')
    assert (await scenario_edit.activate(runtime, shared['draft_id'], user_id='u'))['status'] == 'proposed'
    assert runtime.settings['scenarios'] == []
    mine = scenario_edit.create(runtime, scenario={**GARLAND, 'id': 'agent.mine'}, user_id='u', make_personal=True)
    assert (await scenario_edit.activate(runtime, mine['draft_id'], user_id='u'))['status'] == 'active'
    assert await handled(runtime, 'включи гирлянду', 'SN1') == 'agent.mine'
    assert await handled(runtime, 'включи гирлянду', 'SN2') is None  # чужой колонке сценарий не отвечает
    assert (await scenario_edit.activate(runtime, shared['draft_id'], approved_by_admin=True))['status'] == 'active'
    await runtime.close()


async def test_history_and_revert(tmp_path):
    runtime = home(tmp_path)
    first = scenario_edit.create(runtime, scenario=GARLAND)
    await scenario_edit.activate(runtime, first['draft_id'])
    second = scenario_edit.create(runtime, from_id='agent.garland')
    scenario_edit.patch(runtime, second['draft_id'], [{'op': 'replace', 'path': '/flow/0/reply/text', 'value': 'Готово'}])
    await scenario_edit.activate(runtime, second['draft_id'])
    current = next(s for s in runtime.settings['scenarios'] if s['id'] == 'agent.garland')
    assert current['flow'][0]['reply']['text'] == 'Готово'
    assert [v['version'] for v in scenario_edit.history(runtime, 'agent.garland')] == [1]
    await scenario_edit.revert(runtime, 'agent.garland', 1)
    current = next(s for s in runtime.settings['scenarios'] if s['id'] == 'agent.garland')
    assert current['flow'][0]['reply']['text'] == 'Гирлянда включена'
    with pytest.raises(ValueError):
        await scenario_edit.revert(runtime, 'agent.garland', 1, user_id='u')  # общий — только админ
    await runtime.close()


async def test_agent_creates_personal_scenario_with_confirmation(tmp_path):
    runtime = home(tmp_path)
    runtime.settings['device_context'] = {'sn': 'SN1', 'user_id': 'u'}
    calls = []

    async def provider(kind, payload):
        calls.append(payload)
        tools = [m for m in payload['messages'] if m['role'] == 'tool']
        if not tools:
            arguments = {'scenario': GARLAND}
            message = {'content': '', 'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {
                'name': 'scenario_draft', 'arguments': json.dumps(arguments, ensure_ascii=False)}}]}
        elif len(tools) == 1:
            draft_id = json.loads(tools[0]['content'])['draft_id']
            message = {'content': 'Включу сценарий гирлянды.', 'tool_calls': [{'id': 'c2', 'type': 'function', 'function': {
                'name': 'scenario_activate', 'arguments': json.dumps({'draft_id': draft_id})}}]}
        else:
            message = {'content': 'Готово: скажите «включи гирлянду».'}
        return json.dumps({'choices': [{'message': message}], 'usage': {}}).encode(), 'application/json'
    runtime.provider = provider
    answer = await runtime.chat('сделай команду включи гирлянду', thread='voice:SN1')
    assert answer['tool_calls'][0]['function']['name'] == 'scenario_activate'  # draft — без подтверждения
    assert runtime.settings['scenarios'] == []
    answer = await runtime.chat('да', thread='voice:SN1')
    assert answer['text'].startswith('Готово')
    assert await handled(runtime, 'включи гирлянду', 'SN1') == 'agent.garland'
    assert await handled(runtime, 'включи гирлянду', 'SN2') is None
    await runtime.close()
