"""Sherpa online Russian ASR: binary float32 PCM at 8000Hz, WS partial/final."""
import asyncio
import os
from pathlib import Path
import numpy as np
import sherpa_onnx
import soxr
from aiohttp import web, WSMsgType

async def main():
    root=Path(os.environ.get("ASR_MODEL_DIR","/root/alice-sherpa-models/sherpa-onnx-streaming-t-one-russian-2025-09-08"))
    async def create_recognizer():
        return await asyncio.to_thread(sherpa_onnx.OnlineRecognizer.from_t_one_ctc,
        tokens=str(root/"tokens.txt"),model=str(root/"model.onnx"),num_threads=int(os.environ.get("ASR_THREADS","2")),provider=os.environ.get("ASR_PROVIDER","cpu"),sample_rate=8000,
        enable_endpoint_detection=True,rule1_min_trailing_silence=3.0,rule2_min_trailing_silence=.8,rule3_min_utterance_length=30)
    capacity=max(1,min(8,int(os.environ.get("ASR_WORKERS","2"))))
    workers=asyncio.Queue(maxsize=capacity)
    for _ in range(capacity):await workers.put(await create_recognizer())
    waiting=0
    async def health(request):
        return web.json_response({"status":"ok","engine":"sherpa-onnx","model":"t-one-russian","sample_rate":8000,"format":"f32le","partial":True,"endpoint":"silero-vad","trailing_silence":.8,"capacity":capacity,"active":capacity-workers.qsize(),"waiting":waiting})
    async def recognize_stream(request,recognizer):
        ws=web.WebSocketResponse(max_msg_size=64000,heartbeat=30)
        await ws.prepare(request)
        stream=recognizer.create_stream();last="";total=0
        vad_config=sherpa_onnx.VadModelConfig()
        vad_config.silero_vad.model=str(root.parent/'silero_vad.onnx')
        vad_config.silero_vad.min_silence_duration=.8
        vad_config.silero_vad.min_speech_duration=.15
        vad_config.num_threads=1
        vad=sherpa_onnx.VoiceActivityDetector(vad_config,buffer_size_in_seconds=30)
        vad_resample=soxr.ResampleStream(8000,16000,1,dtype='float32')
        def decode():
            while recognizer.is_ready(stream):recognizer.decode_stream(stream)
            return recognizer.get_result(stream)
        async for packet in ws:
            if packet.type==WSMsgType.BINARY:
                if len(packet.data)%4:await ws.close(code=1003);break
                total+=len(packet.data)
                if total>8000*4*90:await ws.close(code=1009);break
                samples=np.frombuffer(packet.data,dtype="<f4")
                stream.accept_waveform(8000,samples)
                vad_samples=vad_resample.resample_chunk(samples)
                if len(vad_samples):vad.accept_waveform(vad_samples)
                text=await asyncio.to_thread(decode)
                if text!=last:
                    last=text;await ws.send_json({"text":text,"final":False})
                if not vad.empty() or (recognizer.is_endpoint(stream) and text.strip()):
                    stream.accept_waveform(8000,np.zeros(8000,dtype=np.float32))
                    stream.input_finished()
                    text=await asyncio.to_thread(decode)
                    await ws.send_json({"text":text,"final":True});await ws.close();break
            elif packet.type==WSMsgType.TEXT and packet.data=="Done":
                stream.accept_waveform(8000,np.zeros(8000,dtype=np.float32))
                stream.input_finished()
                text=await asyncio.to_thread(decode)
                await ws.send_json({"text":text,"final":True});await ws.close();break
        return ws
    async def recognize(request):
        nonlocal waiting
        if waiting>=8:raise web.HTTPTooManyRequests(text='ASR queue full')
        waiting+=1
        try:
            recognizer=await asyncio.wait_for(workers.get(),timeout=30)
        except TimeoutError:
            raise web.HTTPGatewayTimeout(text='ASR queue timeout')
        finally:waiting-=1
        try:
            async with asyncio.timeout(95):
                return await recognize_stream(request,recognizer)
        finally:workers.put_nowait(recognizer)
    app=web.Application();app.router.add_get("/health",health);app.router.add_get("/asr/stream",recognize)
    runner=web.AppRunner(app);await runner.setup();await web.TCPSite(runner,os.environ.get("HOST","0.0.0.0"),int(os.environ.get("PORT","5003"))).start()
    try:await asyncio.Event().wait()
    finally:await runner.cleanup()

if __name__=="__main__":asyncio.run(main())
