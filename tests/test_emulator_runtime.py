import asyncio
import base64
import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

from aiohttp import ClientSession, web

from server_emulator.runtime import DEFAULT, Runtime
from server_emulator.server import create_app
from tests.conftest import listen


async def test_standalone_persistence_and_admin_guards(tmp_path):
    runtime = Runtime(tmp_path)
    settings = runtime.public()
    settings['model']['api_key'] = 'private-key'
    await runtime.save_settings(settings)
    assert runtime.public()['model']['api_key'] == '__KEEP__'
    await runtime.save_settings(runtime.public())
    assert Runtime(tmp_path).settings['model']['api_key'] == 'private-key'
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url+'/config', headers={'Host': 'quasar.yandex.net'}) as response:
                assert response.status == 404  # маршрут без builtin/эндпоинта — честный 404
            async with client.get(url+'/emulator/') as response:
                assert 'Проверка цепочки' in await response.text()
            async with client.put(url+'/emulator/api/settings', json=settings) as response:
                assert response.status == 403
            async with client.put(url+'/emulator/api/settings', json=settings, headers={'X-Emulator': '1', 'Origin': 'https://bad.example'}) as response:
                assert response.status == 403
            async with client.get(url+'/emulator/api/settings') as response:
                assert 'private-key' not in await response.text()
    finally:
        await runner.cleanup()


async def test_model_speech_and_mcp_with_local_providers(tmp_path):
    requests = []
    async def chat(request):
        body = await request.json()
        requests.append(body)
        return web.json_response({'choices': [{'message': {'content': 'Привет', 'tool_calls': []}}]})
    async def stt(request):
        fields = await request.post()
        assert fields['model'] == 'transcriber'
        return web.json_response({'text': 'распознано'})
    async def tts(request):
        assert (await request.json())['input'] == 'Привет'
        response = web.StreamResponse(headers={'Content-Type': 'audio/wav'})
        await response.prepare(request)
        await response.write(b'RIFF')
        await asyncio.sleep(.01)
        await response.write(b'rest')
        await response.write_eof()
        return response
    async def mcp(request):
        body = await request.json()
        method = body['method']
        if method == 'notifications/initialized':
            return web.Response(status=202)
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'device', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'light', 'inputSchema': {'type': 'object', 'properties': {}}}]}
        else:
            assert body['params'] == {'name': 'light', 'arguments': {}}
            result = {'content': [{'type': 'text', 'text': 'done'}]}
        return web.json_response({'jsonrpc': '2.0', 'id': body['id'], 'result': result})
    provider_app = web.Application()
    provider_app.router.add_post('/chat', chat)
    provider_app.router.add_post('/stt', stt)
    provider_app.router.add_post('/tts', tts)
    provider_app.router.add_post('/mcp', mcp)
    runner, url = await listen(provider_app)
    runtime = Runtime(tmp_path)
    config = runtime.public()
    config['model'].update(url=url+'/chat', model='test')
    config['stt'].update(url=url+'/stt', model='transcriber')
    config['tts'].update(url=url+'/tts')
    config['device_context'] = {'room': 'кухня'}
    config['tools'] = [{'name': 'light', 'enabled': True, 'inputSchema': {'type': 'object', 'properties': {}}}, {'name': 'disabled', 'enabled': False}]
    await runtime.save_settings(config)
    try:
        assert (await runtime.chat('Привет'))['text'] == 'Привет'
        names = [t['function']['name'] for t in requests[0]['tools']]
        assert names[:6] == ['speakers_list', 'speaker_command', 'db_query', 'web_search', 'web_fetch', 'light']  # встроенные всегда + включённые
        assert all(n.startswith('scenario_') for n in names[6:])  # graph: инструменты сценариев
        assert 'кухня' in requests[0]['messages'][0]['content']
        data, _ = await runtime.provider('stt', audio=b'audio')
        assert json.loads(data)['text'] == 'распознано'
        data, _ = await runtime.provider('tts', {'input': 'Привет'})
        assert data == b'RIFFrest'
        assert (await runtime.mcp({'url': url+'/mcp'}, 'tools/list', {}))['tools'][0]['name'] == 'light'
        assert (await runtime.mcp({'url': url+'/mcp'}, 'tools/call', {'name': 'light', 'arguments': {}}))['content'][0]['text'] == 'done'
    finally:
        await runner.cleanup()


async def test_settings_form_keeps_secrets_and_ignores_default_route_mode(tmp_path):
    """TASK-064: маршрута по умолчанию нет — форма настроек его не сохраняет, секреты __KEEP__ целы."""
    runtime = Runtime(tmp_path)
    runtime.settings['model']['api_key'] = 'keep-secret'
    stale_form = runtime.public()
    stale_form['default_route_mode'] = 'hybrid'
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession(headers={'X-Emulator': '1'}) as client:
            stale_form['system_prompt'] = 'edited prompt'
            async with client.put(url+'/emulator/api/settings', json=stale_form) as response:
                assert response.status == 200
                assert 'default_route_mode' not in await response.json()
            assert runtime.settings['model']['api_key'] == 'keep-secret'
            assert runtime.settings['system_prompt'] == 'edited prompt'
            assert 'default_route_mode' not in Runtime(tmp_path).settings
    finally:
        await runner.cleanup()


def test_emulator_folder_runs_standalone_without_parent_schemas(tmp_path):
    source = Path(__file__).resolve().parents[1]/'server_emulator'
    destination = tmp_path/'independent'
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns('state', '__pycache__'))
    result = subprocess.run([sys.executable, str(destination/'server.py'), '--help'], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert '--state' in result.stdout


async def test_static_device_images_webp_png_and_sandboxed(tmp_path, monkeypatch):
    """Каталог картинок устройств (скачиваются с веб-версии умного дома): WebP/PNG
    из каталога картинок отдаётся, всё постороннее (выход за папку, не-картинка,
    несуществующий файл) — 404. Каталог подменён синтетическим."""
    monkeypatch.setenv('ALICA_IMAGES_DIR', str(tmp_path / 'devices'))
    (tmp_path / 'devices').mkdir()
    (tmp_path / 'devices' / 'iot_station_fiero.webp').write_bytes(b'RIFF' + bytes([8, 0, 0, 0]) + b'WEBPVP8 ')
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + '/emulator/img/devices/iot_station_fiero.webp') as response:
                assert response.status == 200 and response.content_type == 'image/webp'
                assert len(await response.read()) > 0
            for bad in ('img/devices/../../runtime.py', 'img/devices/iot_station_fiero.txt',
                        'img/devices/iot_station_no_such.webp', 'img/other/thing.webp'):
                async with client.get(url + '/emulator/' + bad) as response:
                    assert response.status == 404, bad
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_settings_revision_stores_scenario_stubs_only(tmp_path):
    """RF-08: ревизия настроек в БД хранит сценарии как {id, enabled} — тела
    живут в файлах state/scenarios; в памяти Runtime тело сохраняется и
    восстанавливается из файла при следующем старте."""
    runtime = Runtime(tmp_path)
    scenario = {"format_version": 1, "id": "user.revision", "version": "1.0.0",
                "triggers": [{"kind": "phrase", "name": "тест ревизии"}], "slots": {},
                "flow": [{"id": "done", "op": "finish"}], "enabled": True}
    await runtime.save_settings({**runtime.public(), "scenarios": [scenario]})
    revision = runtime.store.load_settings()
    assert revision["scenarios"] == [{"id": "user.revision", "enabled": True}]
    assert runtime.settings["scenarios"][0]["flow"] == scenario["flow"]  # в памяти тело живо
    assert (runtime.scenario_dir / "user.revision.json").exists()  # файл с телом
    await runtime.close()
    reopened = Runtime(tmp_path)
    assert reopened.settings["scenarios"][0] == scenario  # тело восстановлено из файла
    await reopened.close()


async def test_settings_revision_stubs_without_files_are_dropped(tmp_path):
    """RF-08: файлы удалены вручную, в ревизии БД только заглушки — сценариев
    нет (файлы источник правды), заглушки не выгружаются в файлы обратно."""
    runtime = Runtime(tmp_path)
    await runtime.save_settings({**runtime.public(),
                                 "scenarios": [{"id": "user.gone", "triggers": [], "flow": []}]})
    assert (runtime.scenario_dir / "user.gone.json").exists()
    for path in runtime.scenario_dir.glob("*.json"):
        path.unlink()
    await runtime.close()
    reopened = Runtime(tmp_path)
    assert reopened.settings["scenarios"] == []
    assert reopened.scenario_dir.exists() and not list(reopened.scenario_dir.glob("*.json"))
    await reopened.close()


async def test_builtin_py_scenario_copy_via_api_with_code(tmp_path):
    """TASK-038: копия встроенного py-сценария — GET ?script=<id> даёт код, PUT {scenario, script}
    сохраняет копию с кодом и она отвечает на фразу; без кода сервер копию не принимает."""
    from server_emulator.scenario_engine.library import SCENARIOS
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    headers = {'X-Emulator': '1'}
    try:
        async with ClientSession() as client:
            async with client.get(url + '/emulator/api/scenarios?script=device.volume') as response:
                code = (await response.json())['code']
            assert code and 'handle' in code  # код встроенного py-сценария доступен редактору
            scenario = copy.deepcopy({k: v for k, v in SCENARIOS['device.volume'].items() if not k.startswith('_')})
            scenario['id'] = 'user.device.volume'
            scenario['triggers'] = [{'kind': 'phrase', 'phrases': ['копия сценария громкости']}]
            async with client.put(url + '/emulator/api/scenarios', headers=headers, json={'scenario': scenario}) as response:
                assert response.status == 400  # без кода копия py-сценария не создаётся
                assert 'нет кода' in (await response.json())['error']
            async with client.put(url + '/emulator/api/scenarios', headers=headers,
                                  json={'scenario': scenario, 'script': code}) as response:
                assert response.status == 200 and (await response.json())['saved'] == 'user.device.volume'
            assert (runtime.scenario_dir / 'user.device.volume.py').read_text(encoding='utf-8') == code
            runtime.settings['aapi_model'] = False
            async with client.post(url + '/emulator/api/scenarios', headers=headers,
                                   json={'text': 'копия сценария громкости'}) as response:
                report = await response.json()
            assert report['handled'] and report['scenario'] == 'user.device.volume'
    finally:
        await runner.cleanup()
        await runtime.close()
