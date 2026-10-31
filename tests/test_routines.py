"""Задания по расписанию (agent/routines.py): проверка, запуск агента и сценария, доставка,
входящие, тихие часы, лимит, пропуски после простоя, инструменты агента."""
import asyncio
import json
import sqlite3
import time

import pytest

from server_emulator import mcp_hub
from server_emulator.agent import routines as routines_module
from server_emulator.runtime import Runtime
from tests.conftest import types


def home(tmp_path, replies=None):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u', 'name': 'Аня'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN1', 'user_id': 'u', 'device_ids': ['d1'], 'name': 'Кухня'})
    device = next(d for d in runtime.identities.data['devices'] if d['sn'] == 'SN1')
    runtime.identities.mutate({'operation': 'state', 'sn': 'SN1',
                               'state': dict(device['state'], light={'кухня': {'on': False}})})
    runtime.settings['model']['stub_text'] = ''
    calls = []

    async def provider(kind, payload):
        calls.append(json.loads(json.dumps(payload)))
        message = (replies or [{'content': 'ok'}])[min(len(calls), len(replies or [1])) - 1]
        return json.dumps({'choices': [{'message': message}], 'usage': {}}).encode(), 'application/json'
    runtime.provider = provider
    return runtime, calls


def live(runtime, sn='SN1'):
    sent = []

    async def send(data):
        sent.append(data)
    runtime.sessions['s'] = {'connection_id': 's', 'device_sn': sn, 'user_id': 'u', 'send': send}
    return sent


def spoken(sent):
    """Реплики, отправленные колонке Push'ем (server_action, как напоминание облака): текст
    лежит в async_reply до запроса колонки."""
    from server_emulator import async_reply
    from server_emulator.voice import _string_member, _struct_member
    _client, server = types()
    texts = []
    for raw in sent:
        message = server.FromString(raw[4:])
        if message.Event.WhichOneof('Event') == 'Push':
            directive = message.Event.Push.Directives[0]
            assert (directive.Type, directive.Name) == ('server_action', '@@mm_semantic_frame')
            answer_id = _string_member(_struct_member(directive.Payload, 'analytics'), 'origin_info')
            texts.append(async_reply.ANSWERS[answer_id]['answer']['text'])
    return texts


def routine(**values):
    return {'title': 'Утро', 'owner': 'u', 'sn': 'SN1', 'kind': 'agent', 'prompt': 'расскажи погоду',
            'schedule': {'kind': 'cron', 'expr': '0 8 * * *'}, 'delivery': 'speak', **values}


def test_validation_errors(tmp_path):
    runtime, _ = home(tmp_path)
    store = runtime.routines
    for bad, message in [({'schedule': {'kind': 'cron', 'expr': '0 8 *'}}, 'cron'),
                         ({'schedule': {'kind': 'interval', 'expr': '10'}}, 'interval'),
                         ({'schedule': {'kind': 'once', 'expr': 'завтра'}}, 'once'),
                         ({'owner': 'ghost'}, 'owner'), ({'prompt': ''}, 'prompt'), ({'delivery': 'mail'}, 'delivery'),
                         ({'quiet': {'from': '25:00', 'to': '08:00'}}, 'quiet'), ({'tz': 'Mars/Base'}, 'tz')]:
        with pytest.raises(ValueError, match=message):
            store.save(routine(**bad))
    assert store.list() == []


async def test_agent_routine_speaks_on_live_speaker(tmp_path):
    runtime, calls = home(tmp_path, [{'content': 'Сегодня солнечно.'}])
    sent = live(runtime)
    saved = runtime.routines.save(routine())
    result = await runtime.routines.fire(saved['id'])
    assert result == {'status': 'finished', 'text': 'Сегодня солнечно.', 'delivered': 'spoken'}
    assert 'Сегодня солнечно.' in spoken(sent)
    system = calls[0]['messages'][0]['content']
    assert 'фоновое задание по расписанию «Утро»' in system
    assert runtime.routines.runs(saved['id'])[0]['status'] == 'finished'
    await runtime.close()


async def test_offline_goes_to_inbox_and_next_dialog_tells_it(tmp_path):
    runtime, calls = home(tmp_path, [{'content': 'Сегодня солнечно.'}, {'content': 'Кстати, сегодня солнечно.'}])
    saved = runtime.routines.save(routine())
    assert (await runtime.routines.fire(saved['id']))['delivered'] == 'inbox'
    assert runtime.routines.inbox('u', unread=True)[0]['text'] == 'Сегодня солнечно.'
    runtime.settings['device_context'] = {'sn': 'SN1', 'user_id': 'u'}
    await runtime.chat('привет', thread='voice:SN1')
    assert 'Сегодня солнечно.' in calls[-1]['messages'][0]['content']
    assert runtime.routines.inbox('u', unread=True) == []
    await runtime.close()


async def test_quiet_hours_and_silent(tmp_path, monkeypatch):
    runtime, _ = home(tmp_path, [{'content': 'Итог'}])
    sent = live(runtime)
    monkeypatch.setattr(routines_module, 'quiet_now', lambda r, now=None: bool(r.get('quiet')))
    quiet = runtime.routines.save(routine(quiet={'from': '22:00', 'to': '08:00'}))
    assert (await runtime.routines.fire(quiet['id']))['delivered'] == 'inbox' and sent == []
    silent = runtime.routines.save(routine(title='Тихо', delivery='silent'))
    assert (await runtime.routines.fire(silent['id']))['delivered'] == 'silent' and sent == []
    await runtime.close()


def test_quiet_window_crosses_midnight():
    r = {'quiet': {'from': '22:00', 'to': '08:00'}, 'tz': 'UTC'}
    day = 1793145600  # 2026-10-28 00:00 UTC
    assert routines_module.quiet_now(r, day + 23 * 3600) and routines_module.quiet_now(r, day + 3 * 3600)
    assert not routines_module.quiet_now(r, day + 12 * 3600)


async def test_scenario_routine_runs_without_model(tmp_path):
    runtime, calls = home(tmp_path)
    saved = runtime.routines.save(routine(kind='scenario', scenario_id='smart_home.light', text='включи свет на кухне',
                                          prompt=None, delivery='silent'))
    result = await runtime.routines.fire(saved['id'])
    assert result['status'] == 'finished' and calls == []
    device = next(d for d in runtime.identities.data['devices'] if d['sn'] == 'SN1')
    assert device['state']['light']['кухня']['on'] is True
    await runtime.close()


async def test_daily_limit_and_once_disables(tmp_path):
    runtime, _ = home(tmp_path)
    limited = runtime.routines.save(routine(max_runs_per_day=1, delivery='silent'))
    await runtime.routines.fire(limited['id'])
    assert (await runtime.routines.fire(limited['id']))['status'] == 'skipped_budget'
    once = runtime.routines.save(routine(title='Раз', delivery='silent', schedule={'kind': 'once', 'expr': '2030-01-01T08:00'}))
    await runtime.routines.fire(once['id'])
    assert runtime.routines.get(once['id'])['enabled'] is False
    await runtime.close()


async def test_start_marks_missed_and_schedules(tmp_path):
    runtime, _ = home(tmp_path)
    saved = runtime.routines.save(routine(schedule={'kind': 'interval', 'expr': '60'}, delivery='silent'))
    with sqlite3.connect(runtime.routines.path) as db:  # задание создано час назад, ни разу не запускалось
        db.execute('UPDATE routines SET created = ? WHERE id = ?', (time.time() - 3600, saved['id']))
    runtime.routines.start()
    assert runtime.routines.runs(saved['id'])[0]['status'] == 'missed'
    assert runtime.routines.get(saved['id'])['next_run'] > time.time()
    await runtime.close()


async def test_scheduler_fires_job(tmp_path):
    runtime, _ = home(tmp_path, [{'content': 'Итог'}])
    runtime.routines.start()
    saved = runtime.routines.save(routine(delivery='silent'))
    runtime.routines.scheduler.modify_job(saved['id'], next_run_time=__import__('datetime').datetime.now(
        __import__('datetime').timezone.utc))
    for _ in range(50):
        await asyncio.sleep(0.05)
        if runtime.routines.runs(saved['id']):
            break
    assert runtime.routines.runs(saved['id'])[0]['status'] == 'finished'
    await runtime.close()


def tool_call(name, arguments, call_id='c1'):
    return {'content': '', 'tool_calls': [{'id': call_id, 'type': 'function',
                                           'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]}


async def test_agent_creates_routine_after_confirmation(tmp_path):
    arguments = {'title': 'Погода утром', 'kind': 'scenario', 'text': 'какая погода',
                 'schedule': {'kind': 'cron', 'expr': '0 8 * * *'}}
    runtime, _ = home(tmp_path, [tool_call('routine_create', arguments), {'content': 'Готово, каждое утро в 8.'}])
    runtime.settings['device_context'] = {'sn': 'SN1', 'user_id': 'u'}
    answer = await runtime.chat('каждое утро в 8 говори погоду', thread='voice:SN1')
    assert answer['tool_calls'][0]['function']['name'] == 'routine_create' and runtime.routines.list() == []
    answer = await runtime.chat('да', thread='voice:SN1')
    created = runtime.routines.list()
    assert answer['text'] == 'Готово, каждое утро в 8.' and len(created) == 1
    assert created[0]['owner'] == 'u' and created[0]['sn'] == 'SN1' and created[0]['created_by'] == 'agent'
    await runtime.close()


async def test_background_confirmation_goes_to_panel(tmp_path, monkeypatch):
    runtime, _ = home(tmp_path, [tool_call('ha__lock', {}), {'content': 'Закрыла'}])
    mcp_hub.save_config(runtime, {'mcpServers': {'ha': {'type': 'http', 'url': 'http://ha.invalid/mcp'}}})
    runtime.settings['tools'] = [{'name': 'ha__lock', 'server_id': 'ha', 'server_tool': 'lock', 'enabled': True,
                                  'risk': 'external', 'inputSchema': {'type': 'object', 'properties': {}}}]
    saved = runtime.routines.save(routine(prompt='закрой замок на ночь', delivery='speak'))
    result = await runtime.routines.fire(saved['id'])
    assert result['status'] == 'needs_confirmation' and result['delivered'] == 'inbox'
    assert runtime.pending_calls[-1]['thread_id'] == 'routine:' + saved['id']
    assert 'ждёт подтверждения' in runtime.routines.inbox('u')[0]['text']
    await runtime.close()


async def test_routine_speech_is_synthesized_before_push(tmp_path):
    """Реплика задания озвучена до Push: на запрос колонки звук уходит сразу."""
    from server_emulator import async_reply
    from tests.test_voice_failures import make_ogg
    async_reply.ANSWERS.clear()
    runtime, calls = home(tmp_path, [{'content': 'Сегодня солнечно.'}])
    runtime.settings['aapi_tts'] = True
    ogg, model = await make_ogg(), runtime.provider

    async def provider(kind, payload=None, **kwargs):
        if kind == 'tts':
            return ogg, 'audio/ogg'
        return await model(kind, payload, **kwargs)
    runtime.provider = provider
    sent = live(runtime)
    saved = runtime.routines.save(routine())
    await runtime.routines.fire(saved['id'])
    assert spoken(sent) == ['Сегодня солнечно.']
    assert next(iter(async_reply.ANSWERS.values()))['answer'].get('audio')


async def test_answer_to_routine_question_keeps_context(tmp_path):
    """Вопрос задания, сказанный на колонке, лежит в голосовом треде: «да» понятно модели."""
    runtime, calls = home(tmp_path, [{'content': 'Рассказать анекдот?'}, {'content': 'Слушай.'}])
    live(runtime)
    saved = runtime.routines.save(routine())
    assert (await runtime.routines.fire(saved['id']))['delivered'] == 'spoken'
    await runtime.chat('да', thread='voice:SN1', sn='SN1', user_id='u')
    roles = [(m['role'], m.get('content')) for m in calls[1]['messages'][1:]]
    assert roles[-2:] == [('assistant', 'Рассказать анекдот?'), ('user', 'да')]
    await runtime.close()
