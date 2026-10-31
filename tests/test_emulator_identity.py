import copy
import types

import pytest
from server_emulator.identity import IdentityRegistry
from server_emulator.runtime import Runtime, DEFAULT
from server_emulator.server import create_app
from server_emulator.schema import load_type
from server_emulator.runtime import ROOT
from aiohttp import ClientSession
from tests.conftest import listen
import asyncio
import json


def test_multiple_devices_alias_conflicts_and_ownership_reset(tmp_path):
    registry = IdentityRegistry(tmp_path)
    registry.mutate({'operation':'user','id':'alice','name':'Alice','profile':{'system_prompt':'Alice prompt'}})
    registry.mutate({'operation':'user','id':'bob','name':'Bob'})
    for sn, alias in [('SN-A','id-a'),('SN-B','id-b')]:
        registry.mutate({'operation':'device','sn':sn,'user_id':'alice','device_ids':[alias]})
    registry.mutate({'operation':'state','sn':'SN-A','state':{'timers':[123]}})
    assert registry.resolve('id-b')['state'] == {}
    assert registry.settings_for(DEFAULT,registry.resolve('id-a'))['system_prompt'] == 'Alice prompt'
    with pytest.raises(ValueError,match='другому SN'):
        registry.mutate({'operation':'device','sn':'SN-C','device_ids':['id-a']})
    with pytest.raises(ValueError,match='сбросить состояние'):
        registry.mutate({'operation':'device','sn':'SN-A','user_id':'bob','device_ids':['id-a']})
    registry.mutate({'operation':'device','sn':'SN-A','user_id':'bob','device_ids':['id-a'],'reset_state':True})
    assert registry.resolve('id-a')['state'] == {}
    assert registry.settings_for(DEFAULT,registry.resolve('id-a'))['system_prompt'] != 'Alice prompt'
    restored=IdentityRegistry(tmp_path)
    assert restored.resolve('id-b')['user_id']=='alice'
    assert restored.resolve('id-a')['user_id']=='bob'


def test_unknown_device_has_no_owner_profile_and_no_ip_guessing(tmp_path):
    registry=IdentityRegistry(tmp_path)
    assert registry.observe('unknown') is None  # по умолчанию resolve-only: ничего не создаёт
    assert registry.public()['unassigned'] == [] and registry.public()['devices'] == []
    defaults=copy.deepcopy(DEFAULT)
    defaults.update(aapi_model=True,system_prompt='private')
    settings=registry.settings_for(defaults,None)
    assert not settings['aapi_model'] and not settings['tools'] and settings['system_prompt']==''


def test_user_delete_detaches_devices_but_keeps_them(tmp_path):
    registry = IdentityRegistry(tmp_path)
    registry.mutate({'operation':'user','id':'alice','name':'Alice','profile':{'system_prompt':'p'}})
    registry.mutate({'operation':'device','sn':'SN-A','user_id':'alice','device_ids':['id-a']})
    registry.mutate({'operation':'state','sn':'SN-A','state':{'timers':[1]}})
    registry.mutate({'operation':'user_delete','id':'alice'})
    device = registry.public()['devices'][0]
    assert registry.public()['users'] == []
    assert device['user_id'] is None and device['device_ids'] == ['id-a']
    assert device['state'] == {'timers':[1]}  # состояние устройства переживает удаление владельца
    settings = registry.settings_for(copy.deepcopy(DEFAULT), registry.resolve('id-a'))
    assert settings.get('unclaimed') is True
    with pytest.raises(ValueError, match='не найден'):
        registry.mutate({'operation':'user_delete','id':'ghost'})
    restored = IdentityRegistry(tmp_path)
    assert restored.public()['users'] == [] and restored.resolve('id-a')['user_id'] is None


def test_observe_records_activity_for_known_devices(tmp_path):
    registry = IdentityRegistry(tmp_path)
    registry.mutate({'operation':'user','id':'u','name':'U'})
    registry.mutate({'operation':'device','sn':'SN-A','user_id':'u','device_ids':['id-a']})
    registry.observe('id-a', source='header.x-uprx-device-id', ip='192.0.2.107')
    entry = registry.activity['SN-A']
    assert entry['ip'] == '192.0.2.107' and entry['source'] == 'header.x-uprx-device-id' and entry['ts'] > 0
    registry.observe('id-a')  # повторное наблюдение без IP сохраняет последний
    assert registry.activity['SN-A']['ip'] == '192.0.2.107'
    assert registry.public()['devices'][0]['user_id'] == 'u'  # активность не меняет реестр


class _FakeWS:
    def __init__(self):
        self.closed = None

    async def close(self, code=None, message=b''):
        self.closed = (code, message)


def test_device_delete_returns_unbound_on_next_get_sync(tmp_path):
    """Удаление = отвязка + строка уходит из списка. Без игноров: первый же
    /get_sync_info от устройства возвращает его в список — непривязанным."""
    registry = IdentityRegistry(tmp_path)
    registry.mutate({'operation':'user','id':'u','name':'U'})
    registry.mutate({'operation':'device','sn':'SN-A','user_id':'u','device_ids':['id-a']})
    registry.mutate({'operation':'device_delete','sn':'SN-A'})
    assert registry.public()['devices'] == []  # отвязано и удалено, следов не осталось
    # колонка снова стукнулась get_sync — тут же в списке, без владельца
    device = registry.observe('id-a', source='query.device_id', ip='1.2.3.4', register=True)
    assert device is not None and device['user_id'] is None and device['sn'] == 'id-a'
    data = registry.public()
    assert len(data['devices']) == 1 and data['devices'][0]['user_id'] is None
    assert data['unassigned'] == []
    with pytest.raises(ValueError, match='не найден'):
        registry.mutate({'operation':'device_delete','sn':'SN-GHOST'})


async def test_rebind_and_user_delete_drop_live_sessions(tmp_path):
    """Смена привязки/удаление пользователя при живой WS-сессии: не отказ,
    а разрыв 1012 — колонка переподключается и подхватывает новую идентичность."""
    runtime = Runtime(tmp_path)
    for uid, name in (('alice', 'Alice'), ('bob', 'Bob')):
        runtime.identities.mutate({'operation':'user','id':uid,'name':name})
    runtime.identities.mutate({'operation':'device','sn':'SN-A','user_id':'alice','device_ids':['id-a']})
    fake = _FakeWS()
    runtime.sessions['conn'] = {'connection_id':'conn','host':'quasar.yandex.net','ip':'127.0.0.1',
                                'device_sn':'SN-A','user_id':'alice','device_id':'id-a','ws':fake,
                                'runtime': types.SimpleNamespace(settings={})}  # refresh_profiles пишет .settings
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url+'/emulator/api/identities', headers={'X-Emulator':'1'},
                                   json={'operation':'device','sn':'SN-A','name':'SN-A','user_id':'bob',
                                         'device_ids':['id-a'],'profile':{},'reset_state':True}) as response:
                assert response.status == 200
            assert fake.closed == (1012, b'Binding changed, reconnect')
            body = await (await client.get(url+'/emulator/api/identities')).json()
            assert body['devices'][0]['user_id'] == 'bob' and body['sessions'][0]['ip'] == '127.0.0.1'
            async with client.post(url+'/emulator/api/identities', headers={'X-Emulator':'1'},
                                   json={'operation':'user_delete','id':'bob'}) as response:
                assert response.status == 200
            body = await (await client.get(url+'/emulator/api/identities')).json()
            assert [u['id'] for u in body['users']] == ['alice']
            assert body['devices'][0]['user_id'] is None and body['devices'][0]['device_ids'] == ['id-a']
            # удаление устройства при живой сессии — тоже разрыв, а не отказ
            async with client.post(url+'/emulator/api/identities', headers={'X-Emulator':'1'},
                                   json={'operation':'device_delete','sn':'SN-A'}) as response:
                assert response.status == 200
            assert fake.closed == (1012, b'Binding changed, reconnect')
            body = await (await client.get(url+'/emulator/api/identities')).json()
            assert body['devices'] == []
    finally:
        await runner.cleanup()


async def test_two_live_sessions_use_distinct_owner_profiles(tmp_path):
    runtime=Runtime(tmp_path)
    for name in ('alice','bob'):
        runtime.identities.mutate({'operation':'user','id':name,'name':name,'profile':{'system_prompt':name,'aapi_model':True}})
        runtime.identities.mutate({'operation':'device','sn':'SN-'+name,'user_id':name,'device_ids':[name+'-id']})
    async def provider(kind, payload=None, **kwargs):
        assert kind=='model'
        await asyncio.sleep(0.01)
        return json.dumps({'choices':[{'message':{'content':payload['messages'][0]['content'].split('\n')[0]}}]}).encode(),'application/json'
    runtime.provider=provider
    ct=load_type(ROOT/'schemas','alice-api.pb','NAlice.NAliceApi.TClientMessage')
    st=load_type(ROOT/'schemas','alice-api.pb','NAlice.NAliceApi.TServerMessage')
    runner,url=await listen(create_app(runtime=runtime))
    async def exchange(client,name):
        async with client.ws_connect(url+'/uni.ws') as ws:
            sync=ct();sync.Event.Header.MessageId='sync';sync.Event.SynchronizeStateRequest.Application.DeviceId=name+'-id'
            await ws.send_bytes(b'AAPI'+sync.SerializeToString())
            await asyncio.wait_for(ws.receive(),2)
            text=ct();text.Event.Header.MessageId='text';text.Event.TextInput.Request.Event.Text='hello'
            await ws.send_bytes(b'AAPI'+text.SerializeToString())
            for _ in range(3):
                packet=await asyncio.wait_for(ws.receive(),2)
                result=st.FromString(packet.data[4:])
                if result.Event.WhichOneof('Event')=='AliceResponse':
                    return result.Event.AliceResponse.Response.Cards[0].Text
            raise AssertionError('Missing response')
    try:
        async with ClientSession() as client:
            results=await asyncio.gather(exchange(client,'alice'),exchange(client,'bob'))
            assert results==['alice','bob']
            async with client.post(url+'/emulator/api/identities',json={'operation':'user','name':'unauthorized'}) as response:
                assert response.status==403
            async with client.get(url+'/emulator/api/identities') as response:
                assert len((await response.json())['devices'])==2
    finally:
        await runner.cleanup()
