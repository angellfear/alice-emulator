import asyncio
import struct
from pathlib import Path
from aiohttp import web, WSMsgType
import numpy as np
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

async def test_spotter_audio_before_spotter_end_reaches_asr_and_server_endpoint_finishes(tmp_path):
    chunks=[]
    async def asr(request):
        ws=web.WebSocketResponse();await ws.prepare(request)
        async for packet in ws:
            if packet.type==WSMsgType.BINARY:
                chunks.append(bytes(packet.data))
                if sum(map(len,chunks))>=34000:  # весь ход: (1000+16000)/2 сэмплов f32
                    await ws.send_json({'text':'привет','final':True})
                    await ws.close();break
                await ws.send_json({'text':'привет','final':False})
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
        wake=b'\x22\x11'*1000  # слово-активация: кадры до SpotterEnd, слышный тон 0x1122
        phrase=b'\0\0'*16000   # фраза после SpotterEnd: секунда тишины
        await c.receive_audio(struct.pack('>I',1)+wake)
        assert c.channels[1]['size']==len(wake)  # кадры до SpotterEnd считаются, не выбрасываются
        m=client();m.StreamControl.StreamId=1;m.StreamControl.SpotterEnd.SetInParent()
        await c.receive(m,'StreamControl')
        assert any(e.Event.WhichOneof('Event')=='SpotterValidation' and e.Event.SpotterValidation.Result==1 for e in events)
        await c.receive_audio(struct.pack('>I',1)+phrase)
        m=client();m.StreamControl.StreamId=1;m.StreamControl.Close.Reason=0  # колонка закрыла микрофон
        await c.receive(m,'StreamControl')
        await asyncio.wait_for(answer.wait(),3)
        floats=np.frombuffer(b''.join(chunks),dtype='<f4')
        assert 8460<=len(floats)<=8540  # (1000+16000)/2 сэмплов 8 кГц: дошло всё, ничего не потеряно
        assert abs(floats[:100]-4386/32768).max()<0.05  # начало ASR — будильное слово, а не тишина
        assert not c.channels
        recognition=[e.Event.AsrResult for e in events if e.Event.WhichOneof('Event')=='AsrResult']
        assert sum(x.EndOfUtt for x in recognition)==1
        assert recognition[0].Recognition[0].Words[0].Value=='привет'
    finally:
        await c.shutdown();await r.close();await runner.cleanup()


async def test_spotter_wake_audio_reaches_asr_and_server_endpoint_finishes_without_client_close(tmp_path):
    received=0;packets=0;first=asyncio.Event()
    async def asr(request):
        nonlocal received,packets
        ws=web.WebSocketResponse();await ws.prepare(request)
        async for packet in ws:
            if packet.type==WSMsgType.BINARY:
                received+=len(packet.data);packets+=1;first.set()
                if packets>=2:  # будильное слово + фраза после SpotterEnd — финал
                    await ws.send_json({'text':'привет','final':True})
                    await ws.close();break
                await ws.send_json({'text':'привет','final':False})
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
        await c.receive_audio(struct.pack('>I',1)+b'\x22\x11'*8000)  # будильное слово (0,5 с) до SpotterEnd
        await asyncio.wait_for(first.wait(),3)
        assert received>0  # кадры до SpotterEnd дошли до ASR — начало фразы не теряется
        m=client();m.StreamControl.StreamId=1;m.StreamControl.SpotterEnd.SetInParent()
        await c.receive(m,'StreamControl')
        assert any(e.Event.WhichOneof('Event')=='SpotterValidation' and e.Event.SpotterValidation.Result==1 for e in events)
        await c.receive_audio(struct.pack('>I',1)+b'\0\0'*8000)  # фраза после SpotterEnd
        await asyncio.wait_for(answer.wait(),3)
        assert not c.channels
        assert any(e.WhichOneof('Message')=='StreamControl' and e.StreamControl.Close.Reason==2 for e in events)
        recognition=[e.Event.AsrResult for e in events if e.Event.WhichOneof('Event')=='AsrResult']
        assert sum(x.EndOfUtt for x in recognition)==1
        assert recognition[0].Recognition[0].Words[0].Value=='привет'
    finally:
        await c.shutdown();await r.close();await runner.cleanup()


async def test_wake_word_is_stripped_from_recognized_text(tmp_path):
    stripped=[]
    async def asr(request):
        ws=web.WebSocketResponse();await ws.prepare(request)
        async for packet in ws:
            if packet.type==WSMsgType.BINARY:
                await ws.send_json({'text':'Алиса, тумбочка зебра','final':True})
                await ws.close();break
        return ws
    app=web.Application();app.router.add_get('/asr/stream',asr)
    runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    r=Runtime(tmp_path);r.settings.update(aapi_voice=True,aapi_tts=False)
    r.settings['stt']['url']=f'http://127.0.0.1:{runner.addresses[0][1]}/asr/stream'
    async def chat(text,*args,**kwargs):
        stripped.append(text)
        return {'text':'Не поняла','tool_calls':[]}
    r.chat=chat
    root=Path(__file__).resolve().parents[1]/'server_emulator/schemas'
    client=load_type(root,'alice-api.pb','NAlice.NAliceApi.TClientMessage');server=load_type(root,'alice-api.pb','NAlice.NAliceApi.TServerMessage')
    sent=[];final_text=asyncio.Event();answered=asyncio.Event()
    async def send(raw):
        sent.append(raw)
        if raw.startswith(b'AAPI'):
            m=server.FromString(raw[4:])
            if m.Event.WhichOneof('Event')=='AsrResult' and m.Event.AsrResult.EndOfUtt:
                final_text.set()
            if m.Event.WhichOneof('Event')=='AliceResponse':
                answered.set()
    c=Conversation(r,server,send)
    try:
        m=client();m.Event.Header.MessageId='input';m.Event.Header.StreamId=1
        m.Event.VoiceInput.Header.RequestId='req';m.Event.VoiceInput.Format='audio/x-pcm;bit=16;rate=16000;channels=1'
        m.Event.VoiceInput.EnableSpotterValidation=True
        await c.receive(m,'VoiceInput')
        await c.receive_audio(struct.pack('>I',1)+b'\0\0'*16000)
        m=client();m.StreamControl.StreamId=1;m.StreamControl.Close.Reason=0
        await c.receive(m,'StreamControl')
        await asyncio.wait_for(answered.wait(),3)
        results=[server.FromString(raw[4:]).Event.AsrResult for raw in sent
                 if raw.startswith(b'AAPI') and server.FromString(raw[4:]).Event.WhichOneof('Event')=='AsrResult']
        final=[x for x in results if x.EndOfUtt]
        assert len(final)==1 and final[0].AggregatedRecognition.RecognizedText=='тумбочка зебра'
        assert stripped==['тумбочка зебра']  # модель и сценарии получают реплику без wake-слова
    finally:
        await c.shutdown();await r.close();await runner.cleanup()
