"""Sherpa Piper PCM streaming HTTP service, callback-driven and cancellable."""
import asyncio
import concurrent.futures
import os
from pathlib import Path
import threading
import numpy as np
import sherpa_onnx
from aiohttp import web

async def main():
    root=Path(os.environ.get('TTS_MODEL_DIR','/root/alice-sherpa-models/vits-piper-ru_RU-irina-medium'))
    config=sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
        vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=str(next(root.glob('*.onnx'))),
            tokens=str(root/'tokens.txt'),data_dir=str(root/'espeak-ng-data')),num_threads=int(os.environ.get('TTS_THREADS','2')),provider=os.environ.get('TTS_PROVIDER','cpu')),
        max_num_sentences=1)
    capacity=max(1,min(8,int(os.environ.get('TTS_WORKERS','2'))))
    workers=asyncio.Queue(maxsize=capacity)
    for _ in range(capacity):
        await workers.put(await asyncio.to_thread(sherpa_onnx.OfflineTts,config))
    waiting=0
    completed=0
    max_waiting=max(1,int(os.environ.get('TTS_MAX_QUEUE','8')))
    async def health(request):
        return web.json_response({'status':'ok','model':'piper-'+os.environ.get('TTS_VOICE','irina'),'format':'f32le','sample_rate':22050,'callback':True,
            'capacity':capacity,'active':capacity-workers.qsize(),'waiting':waiting,'completed':completed})
    async def speech(request):
        nonlocal waiting,completed
        data=await request.json();text=data.get('input')
        if not isinstance(text,str) or not text.strip() or len(text)>2000:
            raise web.HTTPBadRequest(text='input: 1..2000 characters')
        if waiting>=max_waiting:raise web.HTTPTooManyRequests(text='TTS queue full',headers={'Retry-After':'1'})
        waiting+=1
        try:
            tts=await asyncio.wait_for(workers.get(),timeout=60)
        except TimeoutError:
            raise web.HTTPGatewayTimeout(text='TTS queue timeout')
        finally:
            waiting-=1
        try:
            loop=asyncio.get_running_loop();queue=asyncio.Queue(maxsize=8);cancel=threading.Event()
            def put(value):
                future=asyncio.run_coroutine_threadsafe(queue.put(value),loop)
                while not cancel.is_set():
                    try:future.result(timeout=.1);return True
                    except concurrent.futures.TimeoutError:pass
                future.cancel();return False
            def callback(samples,progress):
                return 1 if put(np.asarray(samples,dtype='<f4').tobytes()) else 0
            def generate():
                try:tts.generate(text,sid=0,speed=1.0,callback=callback)
                finally:put(None)
            task=asyncio.create_task(asyncio.to_thread(generate))
            response=web.StreamResponse(headers={'Content-Type':'audio/pcm','X-Audio-Format':'f32le',
                'X-Audio-Sample-Rate':str(tts.sample_rate),'X-Audio-Channels':'1'})
            try:
                await response.prepare(request)
                async with asyncio.timeout(90):
                    while True:
                        chunk=await queue.get()
                        if chunk is None:break
                        await response.write(chunk)
                    await asyncio.shield(task)
                await response.write_eof();completed+=1;return response
            finally:
                cancel.set()
                await asyncio.shield(task)
        finally:
            workers.put_nowait(tts)
    app=web.Application(client_max_size=32768)
    app.router.add_get('/health',health);app.router.add_post('/v1/audio/speech',speech)
    runner=web.AppRunner(app);await runner.setup();await web.TCPSite(runner,os.environ.get('HOST','0.0.0.0'),int(os.environ.get('PORT','5004'))).start()
    try:await asyncio.Event().wait()
    finally:await runner.cleanup()

if __name__=='__main__':asyncio.run(main())
