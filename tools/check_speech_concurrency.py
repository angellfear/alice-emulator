"""Real provider concurrency check with synthetic phrases, no captured user speech."""
import asyncio
import json
import time
import numpy as np
import soxr
from aiohttp import ClientSession,WSMsgType


async def main():
    async with ClientSession() as client:
        async def check(index,phrase):
            start=time.monotonic()
            async with client.post('http://192.0.2.201:5004/v1/audio/speech',json={'input':phrase}) as response:
                assert response.status==200,response.status
                audio=np.frombuffer(await response.read(),dtype='<f4')
                rate=int(response.headers['X-Audio-Sample-Rate'])
            samples=soxr.resample(audio,rate,8000).astype('<f4')
            async with client.ws_connect('http://192.0.2.201:5003/asr/stream') as ws:
                async def upload():
                    for pos in range(0,len(samples),800):
                        await ws.send_bytes(samples[pos:pos+800].tobytes())
                        await asyncio.sleep(.03)
                    await ws.send_str('Done')
                task=asyncio.create_task(upload());partials=0;final=''
                try:
                    async with asyncio.timeout(30):
                        async for message in ws:
                            if message.type==WSMsgType.TEXT:
                                item=json.loads(message.data)
                                if item.get('final'):final=item.get('text','');break
                                partials+=1
                finally:
                    task.cancel();await asyncio.gather(task,return_exceptions=True)
                assert final.strip(),'Empty recognition'
                return {'job':index,'text':final,'partials':partials,'seconds':round(time.monotonic()-start,2)}
        results=await asyncio.gather(check(1,'Проверка первого устройства.'),check(2,'Вторая колонка отвечает.'))
        assert results[0]['text']!=results[1]['text'],'Streams mixed'
        print(json.dumps(results,ensure_ascii=False))


if __name__=='__main__':asyncio.run(main())
