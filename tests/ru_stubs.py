"""Общие помощники тестов облачных речевых адаптеров: заглушки API поставщиков на локальном aiohttp, без интернета."""
import struct

from aiohttp import ClientSession, web


class StubRuntime:
    """Минимум Runtime для адаптеров: общий HTTP-клиент."""

    def __init__(self):
        self.session = ClientSession()
        self.lifecycle = self

    def client_session(self):
        return self.session

    async def close(self):
        await self.session.close()


class Stub:
    """Локальный сервер-заглушка: handlers {(метод, путь): async def (request, calls)}; calls — записи запросов."""

    def __init__(self, handlers):
        self.calls = []
        app = web.Application()
        for (method, path), handler in handlers.items():
            app.router.add_route(method, path, self._wrap(handler))
        self.runner = web.AppRunner(app)

    def _wrap(self, handler):
        async def run(request):
            body = await request.read()
            call = {"path": request.path, "query": dict(request.query), "headers": dict(request.headers), "body": body}
            self.calls.append(call)
            return await handler(request, call, len(self.calls))
        return run

    async def __aenter__(self):
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        return self

    async def __aexit__(self, *exc):
        await self.runner.cleanup()


def wav(seconds=1.0, rate=16000):
    data = b"\0\0" * int(seconds * rate)
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    return b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + \
        b"data" + struct.pack("<I", len(data)) + data


def pcm(seconds=0.5, rate=24000):
    return b"\x10\x00" * int(seconds * rate)


async def ogg_opus(seconds=1.0):
    """Ogg Opus из тишины — как звук устройства."""
    import numpy as np
    from server_emulator.audio_stream import transcode

    async def chunks():
        yield np.zeros(int(16000 * seconds), dtype="<i2").tobytes()
    return b"".join([page async for page in transcode(chunks(), "audio/x-pcm;bit=16;rate=16000;channels=1", "opus")])


def json_response(data, status=200):
    return web.json_response(data, status=status)
