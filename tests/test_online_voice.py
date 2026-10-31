import asyncio
import struct
from pathlib import Path
from aiohttp import web, WSMsgType
import pytest
from server_emulator.runtime import Runtime
from server_emulator.schema import load_type
from server_emulator.voice import Conversation

async def test_partial_aapi_before_close_and_final_once(tmp_path):
    closed=asyncio.Event()
    async def asr(request):
        ws=web.WebSocketResponse();await ws.prepare(request)
        async for packet in ws:
            if packet.type==WSMsgType.BINARY:
                await ws.send_json({'text':'привет','final':False})
            elif packet.type==WSMsgType.TEXT:
                await ws.send_json({'text':'привет мир','final':True});await ws.close()
        closed.set();return ws
    app=web.Application();app.router.add_get('/asr/stream',asr)
    runner=web.AppRunner(app);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    r=Runtime(tmp_path);r.settings.update(aapi_voice=True,aapi_tts=False)
    r.settings['stt']['url']=f'http://127.0.0.1:{runner.addresses[0][1]}/asr/stream'
    async def chat(*args,**kwargs):return {'text':'Здравствуйте','tool_calls':[]}
    r.chat=chat
    root=Path(__file__).resolve().parents[1]/'server_emulator/schemas'
    client=load_type(root,'alice-api.pb','NAlice.NAliceApi.TClientMessage')
    server=load_type(root,'alice-api.pb','NAlice.NAliceApi.TServerMessage')
    partial=asyncio.Event();results=[]
    async def send(raw):
        if raw.startswith(b'AAPI'):
            m=server.FromString(raw[4:])
            if m.Event.WhichOneof('Event')=='AsrResult':
                results.append(m)
                if not m.Event.AsrResult.EndOfUtt:partial.set()
    c=Conversation(r,server,send)
    try:
        m=client();m.Event.Header.MessageId='input';m.Event.Header.StreamId=1
        m.Event.VoiceInput.Header.RequestId='req';m.Event.VoiceInput.Format='audio/x-pcm;bit=16;rate=16000;channels=1'
        await c.receive(m,'VoiceInput')
        await c.receive_audio(struct.pack('>I',1)+b'\0\0'*16000)
        await asyncio.wait_for(partial.wait(),3)
        assert len(c.channels[1]['audio'])==0  # no whole-utterance copy
        assert not any(m.Event.AsrResult.EndOfUtt for m in results)
        m=client();m.StreamControl.StreamId=1;m.StreamControl.Close.Reason=0
        await c.receive(m,'StreamControl');await asyncio.wait_for(c.task,3)
        assert sum(m.Event.AsrResult.EndOfUtt for m in results)==1
        assert all(m.Event.Header.RefMessageId=='input' for m in results)
        await asyncio.wait_for(closed.wait(),3)
    finally:
        await c.shutdown();await r.close();await runner.cleanup()

async def test_spotter_audio_is_skipped_and_server_endpoint_finishes_without_client_close(tmp_path):
    received=0
    async def asr(request):
        nonlocal received
        ws=web.WebSocketResponse();await ws.prepare(request)
        async for packet in ws:
            if packet.type==WSMsgType.BINARY:
                received+=len(packet.data)
                await ws.send_json({'text':'привет','final':False})
                await ws.send_json({'text':'привет','final':True})
                await ws.close();break
        return ws
    app=web.Application();app.router.add_get('/asr/stream',asr)
    runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    r=Runtime(tmp_path);r.settings.update(aapi_voice=True,aapi_tts=False)
    r.settings['stt']['url']=f'http://127.0.0.1:{runner.addresses[0][1]}/asr/stream'
    async def chat(*args,**kwargs):return {'text':'Здравствуйте','tool_calls':[]}
    r.chat=chat
    root=Path(__file__).resolve().parents[1]/'server_emulator/schemas'
    client=load_type(root,'alice-api.pb','NAlice.NAliceApi.TClientMessage');server=load_type(root,'alice-api.pb','NAlice.NAliceApi.TServerMessage')
    events=[];answer=asyncio.Event()
    async def send(raw):
        if raw.startswith(b'AAPI'):
            m=server.FromString(raw[4:]);events.append(m)
            if m.Event.WhichOneof('Event')=='AliceResponse':answer.set()
    c=Conversation(r,server,send)
    try:
        m=client();m.Event.Header.MessageId='input';m.Event.Header.StreamId=1
        m.Event.VoiceInput.Header.RequestId='req';m.Event.VoiceInput.Format='audio/x-pcm;bit=16;rate=16000;channels=1'
        m.Event.VoiceInput.EnableSpotterValidation=True
        await c.receive(m,'VoiceInput')
        await c.receive_audio(struct.pack('>I',1)+b'invalid wakeword bytes')
        assert not received
        m=client();m.StreamControl.StreamId=1;m.StreamControl.SpotterEnd.SetInParent()
        await c.receive(m,'StreamControl')
        assert any(e.Event.WhichOneof('Event')=='SpotterValidation' and e.Event.SpotterValidation.Result==1 for e in events)
        await c.receive_audio(struct.pack('>I',1)+b'\0\0'*16000)
        await asyncio.wait_for(answer.wait(),3)
        assert received>0
        assert not c.channels
        assert any(e.WhichOneof('Message')=='StreamControl' and e.StreamControl.Close.Reason==2 for e in events)
        recognition=[e.Event.AsrResult for e in events if e.Event.WhichOneof('Event')=='AsrResult']
        assert sum(x.EndOfUtt for x in recognition)==1
        assert recognition[0].Recognition[0].Words[0].Value=='привет'
    finally:
        await c.shutdown();await r.close();await runner.cleanup()
