"""Generate reproducible Alice wake-word WAVs and test the real speech pipeline.

Run from project root: python -m tools.voice_test_corpus
Playback (Windows, default output): python -m tools.voice_test_corpus --play
"""
import argparse
import asyncio
import json
from pathlib import Path
import struct
import tempfile
import time
import uuid
import wave

from aiohttp import ClientSession
import numpy as np

from server_emulator.audio_stream import transcode
from server_emulator.runtime import Runtime, ROOT
from server_emulator.schema import load_type
from server_emulator.voice import Conversation

PHRASES=[('01_privet','привет'),('02_time','сколько сейчас времени'),
         ('03_weather','какая сегодня погода'),('04_joke','расскажи короткую шутку'),
         ('05_timer','поставь таймер на одну минуту'),('06_cancel_timer','отмени таймер')]
OUTPUT=Path('artifacts/voice-tests')


async def generate():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    async with ClientSession() as client:
        async def synth(text):
            async with client.post('http://192.0.2.201:5004/v1/audio/speech',json={'input':text}) as r:
                if r.status!=200:raise RuntimeError(f'TTS HTTP {r.status}')
                rate=int(r.headers['X-Audio-Sample-Rate'])
                return np.frombuffer(await r.read(),dtype='<f4').copy(),rate
        wake,rate=await synth('Алиса!')
        manifest=[]
        for name,command in PHRASES:
            speech,speech_rate=await synth(command+'.')
            assert speech_rate==rate
            # Gap below the 0.8s endpoint threshold; keep wake and command distinct.
            command_offset=len(wake)+round(.25*rate)+round(.25*rate)
            audio=np.concatenate([np.zeros(round(.25*rate)),wake,np.zeros(round(.25*rate)),speech,np.zeros(round(1.2*rate))])
            pcm_data=(np.clip(audio,-1,1)*32767).astype('<i2').tobytes()
            path=OUTPUT/(name+'.wav')
            with wave.open(str(path),'wb') as w:
                w.setnchannels(1);w.setsampwidth(2);w.setframerate(rate);w.writeframes(pcm_data)
            manifest.append({'file':path.name,'text':'Алиса, '+command+'.','command':command,
                             'rate':rate,'seconds':round(len(audio)/rate,3),'command_offset_samples':command_offset})
        for original,name in [(manifest[0],'07_privet_clear'),(manifest[2],'08_weather_clear')]:
            raw,_=pcm(OUTPUT/original['file'])
            samples=np.frombuffer(raw,dtype='<i2').astype(np.float32)/32768
            offset=original['command_offset_samples']
            wake_samples=samples[round(.25*rate):offset-round(.25*rate)]
            command_samples=samples[offset:-round(1.2*rate)]
            gain=min(.95/max(float(abs(command_samples).max()),.001),.12/max(float(np.sqrt(np.mean(command_samples**2))),.001))
            samples=np.concatenate([np.zeros(round(.25*rate)),wake_samples,np.zeros(round(.65*rate)),command_samples*gain,np.zeros(round(1.5*rate))])
            with wave.open(str(OUTPUT/(name+'.wav')),'wb') as w:
                w.setnchannels(1);w.setsampwidth(2);w.setframerate(rate);w.writeframes((samples*32767).astype('<i2').tobytes())
            manifest.append({**original,'file':name+'.wav','seconds':round(len(samples)/rate,3),
                             'command_offset_samples':round(.25*rate)+len(wake_samples)+round(.65*rate),'command_gain':round(gain,3)})
        (OUTPUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        (OUTPUT/'README.txt').write_text('Тестовые фразы Piper / Ирина. WAV PCM16 mono 22050 Hz.\nКаждая начинается со слова «Алиса». Воспроизводите по одному файлу через динамики, ожидая завершения ответа.\n05_timer может создать таймер в режиме реального сервера; 06_cancel_timer отменяет его.\nСинтетический голос не гарантирует срабатывание аппаратного споттера.\n',encoding='utf-8')
        return manifest


def pcm(path,offset=0):
    with wave.open(str(path),'rb') as w:
        rate=w.getframerate();w.setpos(offset);return w.readframes(w.getnframes()-offset),rate


async def test(manifest):
    with tempfile.TemporaryDirectory(prefix='alice-voice-corpus-') as directory:
        runtime=Runtime(Path(directory))
        runtime.settings.update(aapi_voice=True,aapi_tts=True)
        runtime.settings['model']['stub_text']='Проверка завершена. Я вас слышу.'
        runtime.settings['stt']['url']='http://192.0.2.201:5003/asr/stream'
        runtime.settings['tts'].update(url='http://192.0.2.201:5004/v1/audio/speech',response_format='pcm_f32le')
        client=load_type(ROOT/'schemas','alice-api.pb','NAlice.NAliceApi.TClientMessage')
        server=load_type(ROOT/'schemas','alice-api.pb','NAlice.NAliceApi.TServerMessage')
        async def run(item):
            started=time.monotonic();events=[];audio=[];done=asyncio.Event()
            async def send(raw):
                if raw.startswith(b'AAPI'):
                    m=server.FromString(raw[4:]);events.append(m)
                    if m.WhichOneof('Message')=='StreamControl' and m.StreamControl.StreamId%2==0:done.set()
                else:audio.append(raw[4:])
            conv=Conversation(runtime,server,send)
            try:
                message=client();message.Event.Header.MessageId=uuid.uuid4().hex;message.Event.Header.StreamId=1
                voice=message.Event.VoiceInput;voice.Header.RequestId=uuid.uuid4().hex
                raw,rate=pcm(OUTPUT/item['file'],item['command_offset_samples'])
                source_format=f'audio/x-pcm;bit=16;rate={rate};channels=1'
                voice.Format='audio/opus'
                voice.EnableSpotterValidation=True
                await conv.receive(message,'VoiceInput')
                # Protocol boundary is explicit; the emulator does not itself run the device wake-word detector.
                boundary=client();boundary.StreamControl.StreamId=1;boundary.StreamControl.SpotterEnd.SetInParent()
                await conv.receive(boundary,'StreamControl')
                async def chunks():
                    for pos in range(0,len(raw),2048):yield raw[pos:pos+2048]
                wire=b''.join([part async for part in transcode(chunks(),source_format,'opus')])
                for pos in range(0,len(wire),1024):
                    if not conv.channels:break
                    await conv.receive_audio(struct.pack('>I',1)+wire[pos:pos+1024])
                    await asyncio.sleep(.003)
                try:await asyncio.wait_for(done.wait(),12)
                except TimeoutError:
                    raise RuntimeError({'file':item['file'],'errors':runtime.recent,'events':[(m.WhichOneof('Message'),m.Event.WhichOneof('Event'),m.StreamControl.Close.Reason) for m in events], 'tasks':[(k,v['asr_task'].done()) for k,v in conv.channels.items()]})
                if conv.task:await conv.task
                results=[m.Event.AsrResult for m in events if m.Event.WhichOneof('Event')=='AsrResult']
                finals=[r for r in results if r.EndOfUtt]
                text=finals[-1].Recognition[0].Normalized if finals and finals[-1].Recognition else ''
                response_count=sum(m.Event.WhichOneof('Event')=='AliceResponse' for m in events)
                failed=[m.StreamControl.Close.Reason for m in events if m.WhichOneof('Message')=='StreamControl' and m.StreamControl.Close.Reason==1]
                assert len(finals)==1 and text.strip() and response_count==1 and audio and not failed
                async def output():
                    for part in audio:yield part
                decoded=b''.join([part async for part in transcode(output(),'audio/opus','pcm16')])
                assert len(decoded)>3200
                return {'file':item['file'],'recognized':text,'partials':sum(not r.EndOfUtt for r in results),
                        'responses':response_count,'tts_bytes':sum(map(len,audio)), 'seconds':round(time.monotonic()-started,3),
                        'pipeline_passed':True,'asr_exact':text.casefold().strip()==item['command'].casefold().strip()}
            finally:await conv.shutdown()
        try:
            results=[]
            for pos in range(0,len(manifest),2):results.extend(await asyncio.gather(*(run(i) for i in manifest[pos:pos+2])))
            assert not runtime.recent,runtime.recent
            return results
        finally:await runtime.close()


def play(manifest):
    import winsound
    # Only harmless prompts here: timer examples remain available for deliberate playback.
    records=[]
    for item in manifest[:4]:
        start=time.time();winsound.PlaySound(str((OUTPUT/item['file']).resolve()),winsound.SND_FILENAME)
        records.append({'file':item['file'],'started':start,'ended':time.time()})
        print(json.dumps(records[-1]),flush=True)
        time.sleep(10)
    (OUTPUT/'playback.json').write_text(json.dumps(records,indent=2),encoding='utf-8')


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--play',action='store_true');args=parser.parse_args()
    if args.play:
        play(json.loads((OUTPUT/'manifest.json').read_text(encoding='utf-8')));return
    manifest=json.loads((OUTPUT/'manifest.json').read_text(encoding='utf-8')) if (OUTPUT/'manifest.json').exists() else await generate();results=await test(manifest)
    (OUTPUT/'pipeline-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'files':len(manifest),'results':results},ensure_ascii=False))


if __name__=='__main__':asyncio.run(main())
