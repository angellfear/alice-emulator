import asyncio
import json
from aiohttp import ClientSession
from server_emulator.runtime import Runtime, ROOT
from server_emulator.server import create_app
from server_emulator.schema import load_type
from tests.conftest import listen


def setup_registry(runtime):
    for owner in ('a','b'):
        runtime.identities.mutate({'operation':'user','id':owner,'name':owner})
        runtime.identities.mutate({'operation':'device','sn':'SN-'+owner,'user_id':owner,'device_ids':['id-'+owner]})
        runtime.identities.mutate({'operation':'state','sn':'SN-'+owner,'state':{'quasar':{'config':{'device_config':{'name':owner}},'tags':[]}}})


async def test_native_http_and_tokens_are_scoped_and_persistent(tmp_path):
    runtime=Runtime(tmp_path);setup_registry(runtime)
    runner,url=await listen(create_app(runtime=runtime))
    try:
        async with ClientSession(headers={'Host':'quasar.local'}) as client:
            async with client.get(url+'/get_sync_info?device_id=id-a') as r:
                assert (await r.json())['config']['device_config']['name']=='a'
            async with client.get(url+'/glagol/device_list?device_id=id-a') as r:
                assert [d['id'] for d in (await r.json())['devices']]==['id-a']
            async with client.get(url+'/get_sync_info?device_id=unknown') as r:
                assert r.status==403
            async with client.get(url+'/glagol/token?device_id=id-a') as r:
                token=(await r.json())['token']
            for device, expected in [('id-a',True),('id-b',False)]:
                async with client.post(url+'/glagol/v2.0/check_token?device_id='+device,data=token) as r:
                    assert (await r.json())['owner']==expected
            assert Runtime(tmp_path).quasar.verify(token.encode(),runtime.identities.resolve('id-a'))
            async with client.post(url+'/update_device_state?device_id=id-a',json={'ip':'192.0.2.1'}) as r:
                assert r.status==200
            assert runtime.identities.resolve('id-a')['state']['networkInfo']=={'ip':'192.0.2.1'}
            assert 'networkInfo' not in runtime.identities.resolve('id-b')['state']
    finally:
        await runner.cleanup()


async def test_push_targets_one_session_and_ack_is_delivery_only(tmp_path):
    runtime=Runtime(tmp_path);setup_registry(runtime)
    ct=load_type(ROOT/'schemas','alice-api.pb','NAlice.NAliceApi.TClientMessage')
    st=load_type(ROOT/'schemas','alice-api.pb','NAlice.NAliceApi.TServerMessage')
    runner,url=await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url+'/uni.ws',headers={'x-uprx-device-id':'id-a'}) as a, client.ws_connect(url+'/uni.ws',headers={'x-uprx-device-id':'id-b'}) as b:
                async with client.get(url+'/emulator/api/identities') as r:
                    sessions=(await r.json())['sessions']
                target=next(s for s in sessions if s['device_sn']=='SN-a')
                value={'connection_id':target['connection_id'],'device_sn':'SN-a','action':'sound_set_level','arguments':{'level':3}}
                async with client.post(url+'/emulator/api/actions',json=value,headers={'X-Emulator':'1'}) as r:
                    assert r.status==200,await r.text()
                    result=await r.json()
                    assert result['execution']=='unconfirmed'
                packet=await asyncio.wait_for(a.receive(),2)
                message=st.FromString(packet.data[4:])
                assert message.Event.Push.Directives[0].Name=='sound_set_level'
                ack=ct();ack.Event.Header.MessageId='ack';ack.Event.Ack.ReceivedHeader.CopyFrom(message.Event.Header)
                await a.send_bytes(b'AAPI'+ack.SerializeToString())
                for _ in range(20):
                    if runtime.action_log[0]['delivery']=='acknowledged':break
                    await asyncio.sleep(.01)
                assert runtime.action_log[0]['delivery']=='acknowledged'
                assert runtime.action_log[0]['execution']=='unconfirmed'
                value['device_sn']='SN-b'
                async with client.post(url+'/emulator/api/actions',json=value,headers={'X-Emulator':'1'}) as r:
                    assert r.status==400
                try:
                    await asyncio.wait_for(b.receive(),.1)
                    raise AssertionError('Other device received a command')
                except asyncio.TimeoutError:
                    pass
    finally:
        await runner.cleanup()
