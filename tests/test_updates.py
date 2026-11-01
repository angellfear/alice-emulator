import json
from aiohttp import ClientSession, web
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from server_emulator.updates import Updates
from tests.conftest import listen


async def test_deny_still_fetches_real_urls_and_persists_each_attempt(tmp_path):
    runtime=Runtime(tmp_path)
    value=runtime.updates.settings();value['policy']='deny';runtime.updates.save(value)
    calls=[]
    async def upstream(url,method,headers,body):
        calls.append((url,method,body))
        return 200,{'Content-Type':'application/json'},json.dumps({'updates':[{'url':'https://cdn.example/ota.zip?sig=signed','type':'ota','version_code':123}]}).encode()
    runtime.update_fetch=upstream
    runner,url=await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            for _ in range(2):
                async with client.post(url+'/tv/api/v4/updates?device_id=device',headers={'Host':'quasar.yandex.net'},json={'installed_apps':[]}) as response:
                    assert response.status==200
                    assert await response.json()=={'updates':[]}
            assert len(calls)==2 and calls[0][1]=='POST'
            assert 'device_id=device' in calls[0][0]
            inventory=Updates(tmp_path).inventory()
            assert len(inventory['attempts'])==2
            assert inventory['urls'][0]['hits']==2
            assert inventory['urls'][0]['url'].endswith('?sig=signed')
            async with client.get(url+'/allow_update',headers={'Host':'quasar.yandex.net'}) as response:
                assert await response.json()=={'allow':False}
    finally:await runner.cleanup()


async def test_custom_by_sn_failure_logged_and_dynamic_endpoint(tmp_path):
    runtime=Runtime(tmp_path)
    runtime.identities.mutate({'operation':'user','id':'u','name':'U'})
    runtime.identities.mutate({'operation':'device','sn':'SN1','user_id':'u','device_ids':['id1']})
    config=runtime.updates.settings();config['policy']='deny';config['devices']={'SN1':'custom'};config['custom_devices']={'SN1':{'updates':[{'url':'https://mine.example/ota.zip','version_code':99}]}}
    config['endpoints'].append({'host':'custom.example','path':'/updates2','method':'POST','kind':'list'})
    runtime.updates.save(config)
    async def fail(*args):raise OSError('credential must not be recorded')
    runtime.update_fetch=fail
    runner,url=await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url+'/updates2',headers={'Host':'custom.example','X-YaQuasarDeviceId':'id1'}) as response:
                assert (await response.json())['updates'][0]['version_code']==99
            inventory=runtime.updates.inventory('SN1')
            assert inventory['attempts'][0]['error']=='OSError'
            assert not inventory['urls'] # custom OTA must not be presented as upstream discovery
            async with client.post(url+'/emulator/api/updates',json=config) as response:assert response.status==403
            async with client.get(url+'/emulator/api/updates?sn=SN1') as response:assert len((await response.json())['attempts'])==1
    finally:await runner.cleanup()


async def test_allow_preserves_status_and_loop_is_stopped(tmp_path):
    runtime=Runtime(tmp_path)
    config=runtime.updates.settings();config['policy']='allow';runtime.updates.save(config)
    async def upstream(*args):return 429,{'Content-Type':'application/json','Retry-After':'10'},b'{"error":"limit"}'
    runtime.update_fetch=upstream
    runner,url=await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url+'/tv/api/v4/updates',headers={'Host':'quasar.yandex.net'}) as response:
                assert response.status==429 and response.headers['Retry-After']=='10'
            async with client.post(url+'/tv/api/v4/updates',headers={'Host':'quasar.yandex.net','X-Alice-Update-Probe':'1'}) as response:assert response.status==508
    finally:await runner.cleanup()


async def test_platform_policy_between_global_and_device(tmp_path):
    from server_emulator.updates import platform_of
    assert platform_of({'sn':'C00T10001B6SAK'})=='chiron'
    assert platform_of({'sn':'zzz','model':'yandexmidi'})=='yandexmidi'
    assert platform_of({'sn':'C00T10001B6SAK','profile':{'platform':'magritte'}})=='magritte'
    assert platform_of(None)==''
    runtime=Runtime(tmp_path)
    runtime.identities.mutate({'operation':'user','id':'u','name':'U'})
    runtime.identities.mutate({'operation':'device','sn':'C00T10001B6SAK','user_id':'u','device_ids':['idc']})
    runtime.identities.mutate({'operation':'device','sn':'X00T10001MP7PK','user_id':'u','device_ids':['idx']})
    config=runtime.updates.settings();config['policy']='deny';config['platforms']={'chiron':'custom'}
    config['custom_platforms']={'chiron':{'updates':[{'url':'https://p.example/ota.zip','version_code':7}]}}
    runtime.updates.save(config)
    async def upstream(*args):return 200,{'Content-Type':'application/json'},b'{"updates":[]}'
    runtime.update_fetch=upstream
    runner,url=await listen(create_app(runtime=runtime))
    async def check(client,device_id):
        async with client.post(url+'/tv/api/v4/updates',headers={'Host':'quasar.yandex.net','X-YaQuasarDeviceId':device_id}) as response:
            return await response.json()
    try:
        async with ClientSession() as client:
            assert (await check(client,'idc'))['updates'][0]['version_code']==7  # платформа перекрывает общую
            assert await check(client,'idx')=={'updates':[]}  # другая платформа — общая deny
            config=runtime.updates.settings();config['devices']={'C00T10001B6SAK':'deny'};runtime.updates.save(config)
            assert await check(client,'idc')=={'updates':[]}  # устройство перекрывает платформу
            async with client.get(url+'/emulator/api/updates?platform=chiron') as response:
                body=await response.json()
            assert len(body['attempts'])==2 and {a['device_sn'] for a in body['attempts']}=={'C00T10001B6SAK'}
            assert body['device_platforms']['X00T10001MP7PK']=='yandexstation_2'
    finally:await runner.cleanup()


def test_catalog_platform_by_device_or_cdn_path():
    from server_emulator.updates import url_platforms
    devices=[{'sn':'S1','model':'yandexmidi'}]
    assert url_platforms('https://cdn.example/x/ota.zip',['S1'],devices)==['yandexmidi']  # по устройству из реестра
    assert url_platforms('https://cdn.example/x/ota.zip',['X00T10001MP7PK'],[])==['yandexstation_2']  # по SN вне реестра
    assert url_platforms('https://cdn.example/chiron/ota/user/u/1.zip',[],[])==['chiron']  # по пути CDN
    assert url_platforms('https://cdn.example/ota.zip',[],[])==[]
