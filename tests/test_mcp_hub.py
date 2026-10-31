import json
import sys

import pytest
from aiohttp import web

from server_emulator import mcp_hub
from server_emulator.runtime import Runtime
from tests.conftest import listen


def test_merge_tools_keeps_choice():
    existing = [{'name': 'ha__x', 'server_id': 'ha', 'enabled': True, 'mode': 'confirm'},
                {'name': 'other', 'server_id': 's2', 'enabled': True}]
    merged = {t['name']: t for t in mcp_hub.merge_tools(existing, 'ha', [
        {'name': 'x', 'description': 'd', 'inputSchema': {'type': 'object'}}, {'name': 'y'}])}
    assert merged['ha__x']['enabled'] and merged['ha__x']['mode'] == 'confirm' and merged['ha__x']['description'] == 'd'
    assert not merged['ha__y']['enabled'] and merged['ha__y']['mode'] == 'auto'
    assert merged['other']['server_id'] == 's2'


def test_result_text_error():
    text = mcp_hub.result_text({'content': [{'type': 'text', 'text': 'boom'}], 'isError': True})
    assert text.startswith('Ошибка инструмента: ') and 'boom' in text


def test_mcp_json_created_with_ha_template_and_validated(tmp_path):
    runtime = Runtime(tmp_path)
    config = mcp_hub.load_config(runtime)
    assert config['mcpServers']['homeassistant']['disabled'] is True
    assert (tmp_path / 'mcp.json').exists()
    with pytest.raises(ValueError, match='type'):
        mcp_hub.save_config(runtime, {'mcpServers': {'x': {'type': 'sse', 'url': 'http://a'}}})
    with pytest.raises(ValueError, match='alice'):
        mcp_hub.save_config(runtime, {'mcpServers': {'alice': {'url': 'http://a'}}})


def test_legacy_mcp_servers_migrate_to_mcp_json(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings['mcp_servers'] = [{'id': 'dev', 'url': 'http://h/mcp', 'api_key': 'k'}]
    server = mcp_hub.load_config(runtime)['mcpServers']['dev']
    assert server == {'type': 'http', 'url': 'http://h/mcp', 'headers': {'Authorization': 'Bearer k'}}


def home(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'u', 'name': 'Аня'})
    runtime.identities.mutate({'operation': 'user', 'id': 'v', 'name': 'Вова'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN1', 'user_id': 'u', 'device_ids': ['d1'], 'name': 'Кухня', 'room': 'кухня'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN2', 'user_id': 'v', 'device_ids': ['d2'], 'name': 'Спальня'})
    runtime.settings['model']['stub_text'] = ''
    return runtime


def test_db_query_scoped_readonly_and_masks_secrets(tmp_path):
    runtime = home(tmp_path)
    runtime.quasar_params.def_upsert({'key': 'user.token', 'scope': 'user', 'type': 'secret', 'title': 'Токен'})
    runtime.quasar_params.set_value('user:u', 'user.token', 'very-secret')
    context = {'user_id': 'u', 'sn': 'SN1'}
    rows = mcp_hub.db_query(runtime, context, 'SELECT sn, room, current FROM speakers')['rows']
    assert rows == [('SN1', 'кухня', 1)]  # колонки другого пользователя не видны
    params = mcp_hub.db_query(runtime, context, "SELECT value FROM params WHERE key='user.token'")['rows']
    assert params == [('***',)]
    for sql in ('DELETE FROM speakers', 'SELECT 1; DROP TABLE speakers', 'SELECT * FROM settings'):
        with pytest.raises(ValueError):
            mcp_hub.db_query(runtime, context, sql)


def test_base_context_names_user_and_speakers(tmp_path):
    runtime = home(tmp_path)
    text = mcp_hub.base_context(runtime, {'user_id': 'u', 'sn': 'SN1'})
    assert 'Сейчас:' in text and 'Аня' in text and '«Кухня»' in text and 'Спальня' not in text


def chat_runtime(tmp_path, replies):
    runtime = home(tmp_path)
    runtime.settings['device_context'] = {'sn': 'SN1', 'user_id': 'u', 'state': {'huge': 'x' * 100}}
    calls = []

    async def provider(kind, payload):
        calls.append(json.loads(json.dumps(payload)))
        message = replies[min(len(calls), len(replies)) - 1]
        return json.dumps({'choices': [{'message': message}], 'usage': {}}).encode(), 'application/json'
    runtime.provider = provider
    return runtime, calls


def tool_call(name, arguments='{}'):
    return {'content': '', 'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {'name': name, 'arguments': arguments}}]}


async def test_chat_runs_builtin_tool_in_loop_with_base_context(tmp_path):
    runtime, calls = chat_runtime(tmp_path, [tool_call('speakers_list'), {'content': 'Есть колонка Кухня'}])
    answer = await runtime.chat('какие у меня колонки')
    assert answer['text'] == 'Есть колонка Кухня' and len(calls) == 2
    system = calls[0]['messages'][0]['content']
    assert 'Аня' in system and 'huge' not in system  # базовый контекст есть, служебный state — нет
    assert {t['function']['name'] for t in calls[0]['tools']} >= {'speakers_list', 'speaker_command', 'db_query'}
    tool_message = next(m for m in calls[1]['messages'] if m['role'] == 'tool')
    assert tool_message['tool_call_id'] == 'c1' and 'SN1' in tool_message['content'] and 'SN2' not in tool_message['content']
    assert runtime.tool_log[-1]['name'] == 'speakers_list'


async def test_chat_confirm_tool_is_returned_not_run(tmp_path):
    runtime, calls = chat_runtime(tmp_path, [tool_call('ha__light')])
    mcp_hub.save_config(runtime, {'mcpServers': {'ha': {'type': 'http', 'url': 'http://ha.invalid/mcp'}}})
    runtime.settings['tools'] = [{'name': 'ha__light', 'server_id': 'ha', 'server_tool': 'light',
                                  'enabled': True, 'mode': 'confirm'}]
    answer = await runtime.chat('включи свет')
    assert len(calls) == 1 and answer['tool_calls'][0]['id'] == 'c1' and answer['tool_runs'] == []


async def test_speaker_command_needs_live_session_and_owner(tmp_path):
    runtime = home(tmp_path)
    context = {'sn': 'SN1', 'user_id': 'u'}
    result = await mcp_hub.call(runtime, 'speaker_command', {'action': 'sound_louder'}, context)
    assert result['isError'] and 'не в сети' in result['content'][0]['text']
    sent = []

    async def send(data):
        sent.append(data)
    runtime.sessions['s'] = {'connection_id': 's', 'device_sn': 'SN1', 'user_id': 'u', 'send': send}
    result = await mcp_hub.call(runtime, 'speaker_command', {'action': 'sound_louder'}, context)
    assert not result['isError'] and sent and sent[0].startswith(b'AAPI')
    result = await mcp_hub.call(runtime, 'speaker_command', {'action': 'sound_louder', 'sn': 'SN2'}, context)
    assert result['isError']  # чужая колонка


async def test_external_http_server_sse_with_headers(tmp_path):
    async def handle(request):
        if request.headers.get('Authorization') != 'Bearer t':
            return web.Response(status=401)
        body = await request.json()
        if 'id' not in body:
            return web.Response(status=202)
        result = {'initialize': {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                                 'serverInfo': {'name': 'ha', 'version': '1'}},
                  'tools/list': {'tools': [{'name': 'ha_get_state', 'inputSchema': {'type': 'object'}}]},
                  'tools/call': {'content': [{'type': 'text', 'text': 'on'}]}}[body['method']]
        data = json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result})
        return web.Response(text='event: message\ndata: ' + data + '\n\n', content_type='text/event-stream')
    app = web.Application()
    app.router.add_post('/private_x', handle)
    runner, url = await listen(app)
    runtime = Runtime(tmp_path)
    try:
        mcp_hub.save_config(runtime, {'mcpServers': {'ha': {'type': 'http', 'url': url + '/private_x',
                                                            'headers': {'Authorization': 'Bearer t'}}}})
        tools = await mcp_hub.list_tools(runtime, mcp_hub.server_of(runtime, 'ha'), 'ha')
        runtime.settings['tools'] = mcp_hub.merge_tools([], 'ha', tools)
        runtime.settings['tools'][0]['enabled'] = True
        assert 'ha__ha_get_state' in [t['function']['name'] for t in mcp_hub.model_tools(runtime)]
        assert mcp_hub.result_text(await mcp_hub.call(runtime, 'ha__ha_get_state', {})) == 'on'
        mcp_hub.save_config(runtime, {'mcpServers': {'ha': {'type': 'http', 'url': url + '/private_x', 'disabled': True}}})
        assert 'ha__ha_get_state' not in [t['function']['name'] for t in mcp_hub.model_tools(runtime)]
        mcp_hub.save_config(runtime, {'mcpServers': {'ha': {'type': 'http', 'url': url + '/private_x'}}})
        with pytest.raises(ValueError, match='401'):
            await mcp_hub.call(runtime, 'ha__ha_get_state', {})
    finally:
        await runner.cleanup()


STDIO_SERVER = r'''
import json
from mcp.server.fastmcp import FastMCP

server = FastMCP("echo")


@server.tool()
def echo(x: int) -> str:
    return "echo:" + json.dumps({"x": x})


server.run()
'''


async def test_stdio_server(tmp_path):
    script = tmp_path / 'server.py'
    script.write_text(STDIO_SERVER, encoding='utf-8')
    runtime = Runtime(tmp_path)
    mcp_hub.save_config(runtime, {'mcpServers': {'echo': {'command': sys.executable, 'args': [str(script)]}}})
    try:
        tools = await mcp_hub.list_tools(runtime, mcp_hub.server_of(runtime, 'echo'), 'echo')
        assert tools[0]['name'] == 'echo'
        runtime.settings['tools'] = mcp_hub.merge_tools([], 'echo', tools)
        runtime.settings['tools'][0]['enabled'] = True
        result = await mcp_hub.call(runtime, 'echo__echo', {'x': 1})
        assert mcp_hub.result_text(result) == 'echo:{"x": 1}'
    finally:
        for item in mcp_hub._STDIO.values():
            item.close()
        mcp_hub._STDIO.clear()


@pytest.mark.asyncio
async def test_web_tools_builtin_and_local_addresses_denied(tmp_path):
    runtime = home(tmp_path)
    names = {t['name'] for t in mcp_hub.builtin_tools()}
    assert {'web_search', 'web_fetch'} <= names
    result = await mcp_hub.call(runtime, 'web_fetch', {'url': 'http://127.0.0.1:8090/'})
    assert result['isError'] and 'локальные' in result['content'][0]['text']
    result = await mcp_hub.call(runtime, 'web_fetch', {'url': 'file:///etc/passwd'})
    assert result['isError']
    from server_emulator.agent import policy
    assert policy.tool_risk(runtime, 'web_search') == 'read'
