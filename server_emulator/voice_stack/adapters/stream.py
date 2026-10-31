"""Потоковое распознавание по WebSocket: float32 PCM 8 кГц, JSON {text, final} (tools/builder_asr.py)."""
import asyncio
import json

from aiohttp import WSMsgType

TITLE = "Потоковый сервер (WebSocket)"
DESCRIPTION = "Частичные результаты во время речи. URL оканчивается на /asr/stream."
SERVICES = {
    "asr": {
        "fields": [{"key": "url", "label": "URL", "type": "text"},
                   {"key": "model", "label": "Модель", "type": "text"},
                   {"key": "language", "label": "Язык", "type": "text"}],
        "defaults": {"url": "http://127.0.0.1:5003/asr/stream", "model": "t-one-russian", "language": "ru"},
    },
}
MATCH = {"asr": r"/asr/stream$"}


async def transcribe(runtime, config, audio, filename):
    raise ValueError("Потоковый ASR принимает только живой звук устройства")


async def recognize_stream(runtime, config, chunks, fmt, on_partial=None):
    from ...audio_stream import transcode
    async with asyncio.timeout(120):
        async with runtime.lifecycle.client_session().ws_connect(config['url'], heartbeat=20, max_msg_size=65536) as ws:
            async def upload():
                try:
                    async for chunk in transcode(chunks, fmt, 'f32_8000'):
                        # Bound frame size independently of provider callback size.
                        for pos in range(0,len(chunk),3200):
                            await ws.send_bytes(chunk[pos:pos+3200])
                    await ws.send_str('Done')
                except BaseException:
                    await ws.close()
                    raise
            sender = asyncio.create_task(upload())
            last = None
            try:
                async for packet in ws:
                    if packet.type == WSMsgType.TEXT:
                        result = json.loads(packet.data)
                        text = result.get('text','')
                        if result.get('final'):
                            # ASR endpoint can finish before device closes its microphone.
                            if not sender.done():sender.cancel()
                            await asyncio.gather(sender,return_exceptions=True)
                            return {'text':text}
                        if text and text != last and on_partial:
                            await on_partial(text)
                        last = text
                    elif packet.type == WSMsgType.ERROR:
                        raise ValueError('ASR WebSocket error')
                await sender
                raise ValueError('ASR closed without final recognition')
            finally:
                sender.cancel()
                await asyncio.gather(sender,return_exceptions=True)
