"""Режим «Интеллект» (server_emulator/intelligence.py): scenarios / model / agent."""
import json

import pytest

from server_emulator import intelligence, mcp_hub
from server_emulator.runtime import Runtime


def setup(tmp_path, level):
    runtime = Runtime(tmp_path)
    runtime.settings['model']['stub_text'] = ''
    runtime.settings['intelligence'] = level
    calls = []

    async def provider(kind, payload):
        calls.append(json.loads(json.dumps(payload)))
        return json.dumps({'choices': [{'message': {'content': 'ok'}}], 'usage': {}}).encode(), 'application/json'
    runtime.provider = provider
    return runtime, calls


async def test_scenarios_level_never_calls_model(tmp_path):
    runtime, calls = setup(tmp_path, 'scenarios')
    answer = await runtime.chat('расскажи анекдот')
    assert calls == [] and answer['source'] == 'scenarios' and answer['text'] == 'Я пока этого не умею.'
    await runtime.close()


async def test_scenarios_level_uses_standard_reply(tmp_path):
    runtime, _ = setup(tmp_path, 'scenarios')
    for reply in runtime.settings['standard_replies']:
        if reply['case'] == 'not_understood':
            reply['text'] = 'Такой команды нет.'
    assert (await runtime.chat('что-то'))['text'] == 'Такой команды нет.'
    await runtime.close()


async def test_model_level_single_call_without_mcp_tools(tmp_path, monkeypatch):
    runtime, calls = setup(tmp_path, 'model')
    monkeypatch.setattr(mcp_hub, 'model_tools', lambda runtime: [{'type': 'function', 'function': {'name': 'x'}}])
    answer = await runtime.chat('привет')
    assert answer['text'] == 'ok' and len(calls) == 1 and 'tools' not in calls[0]
    assert 'thread' not in answer  # не LangGraph
    await runtime.close()


async def test_agent_level_keeps_tools(tmp_path, monkeypatch):
    runtime, calls = setup(tmp_path, 'agent')
    monkeypatch.setenv('EMULATOR_AGENT_ENGINE', 'legacy')
    monkeypatch.setattr(mcp_hub, 'model_tools', lambda runtime: [{'type': 'function', 'function': {'name': 'x'}}])
    await runtime.chat('привет')
    assert calls[0]['tools'][0]['function']['name'] == 'x'
    await runtime.close()


@pytest.mark.parametrize('engine,expected', [('graph', 'agent'), ('legacy', 'model')])
def test_migration_from_agent_engine(engine, expected):
    settings = {'agent': {'engine': engine}}
    assert intelligence.migrate(settings) and settings['intelligence'] == expected
    assert not intelligence.migrate(settings)


def test_validation_rejects_unknown_level(tmp_path):
    runtime = Runtime(tmp_path)
    value = runtime.public()
    value['intelligence'] = 'genius'
    with pytest.raises(ValueError):
        runtime.settings_service.validate(value, runtime.settings)
