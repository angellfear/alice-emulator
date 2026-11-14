"""Потоковое распознавание по WebSocket: float32 PCM 8 кГц, JSON {text, final} (tools/builder_asr.py)."""
import asyncio
import json

from aiohttp import WSMsgType

TITLE = "Потоковый сервер (WebSocket)"
DESCRIPTION = "Частичные результаты во время речи. URL оканчивается на /asr/stream."
YES_NO = [{"value": "yes", "label": "Включено"}, {"value": "no", "label": "Выключено"}]
SERVICES = {
    "asr": {
        "fields": [{"key": "url", "label": "URL", "type": "text"},
                   {"key": "model", "label": "Модель", "type": "text"},
                   {"key": "language", "label": "Язык", "type": "text"},
                   {"key": "normalize", "label": "Нормализация громкости", "type": "select", "options": YES_NO,
                    "group": "advanced", "help": "Микрофон колонки тихий: перед распознаванием звук усиливается до ровного уровня"}],
        "defaults": {"url": "http://127.0.0.1:5003/asr/stream", "model": "t-one-russian", "language": "ru",
                     "normalize": "yes"},
    },
}
MATCH = {"asr": r"/asr/stream$"}


async def transcribe(runtime, config, audio, filename):
    raise ValueError("Потоковый ASR принимает только живой звук устройства")


async def _recognize_one(runtime, config, chunks, fmt, on_partial=None):
    from ...audio_stream import transcode
    async with asyncio.timeout(120):
        async with runtime.lifecycle.client_session().ws_connect(config['url'], heartbeat=20, max_msg_size=65536) as ws:
            async def upload():
                try:
                    async for chunk in transcode(chunks, fmt, 'f32_8000', level=config.get('normalize', 'yes') == 'yes'):
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


def _is_stream_start(frame):
    """Кадр канала начинается со страницы BOS Ogg (начало логического потока)."""
    return frame[:4] == b'OggS' and len(frame) > 5 and bool(frame[5] & 2)


async def recognize_stream(runtime, config, chunks, fmt, on_partial=None):
    """Колонка шлёт в одном канале сначала поток со словом активации, затем новый логический
    поток (BOS, страницы с нуля) со всей репликой. Распознаётся последний поток: слово
    активации не попадает в текст обрывками («са», «лиса»). PCM и прочие форматы — один поток."""
    if fmt not in ('audio/opus', 'audio/ogg'):
        return await _recognize_one(runtime, config, chunks, fmt, on_partial)
    result = asyncio.get_running_loop().create_future()
    current = {'generation': 0, 'queue': None, 'task': None, 'frames': 0}

    async def run(generation, queue):
        async def frames():
            while (item := await queue.get()) is not None:
                yield item

        async def partial(text):
            if generation == current['generation'] and on_partial:
                await on_partial(text)
        try:
            value = await _recognize_one(runtime, config, frames(), fmt, partial)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — ошибку отдаёт только актуальный поток
            if generation == current['generation'] and not result.done():
                result.set_exception(exc)
            return
        if generation == current['generation'] and not result.done():
            result.set_result(value)

    def start():
        if current['task']:
            current['task'].cancel()
        current['generation'] += 1
        current['queue'] = asyncio.Queue()
        current['frames'] = 0
        current['task'] = asyncio.create_task(run(current['generation'], current['queue']))

    async def pump():
        start()
        async for frame in chunks:
            if _is_stream_start(frame) and current['frames']:
                start()
            current['frames'] += 1
            current['queue'].put_nowait(frame)
        current['queue'].put_nowait(None)

    feeder = asyncio.create_task(pump())
    try:
        return await result
    finally:
        feeder.cancel()
        if current['task']:
            current['task'].cancel()
        await asyncio.gather(feeder, current['task'], return_exceptions=True)
