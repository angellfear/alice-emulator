"""Локальный push-канал xiva (quasar-realtime) для маршрута «Эмуляция».

Подписка POST /push_subscribe: sign/ts подписывает локальный ключ
(quasar-secret.json — QuasarBackend.key); тот же ключ станция предъявляет
в query при открытии WS /v2/subscribe/websocket, сервер канала проверяет
подпись при подключении. Кадры — по образцу живого обмена колонки: ping
при открытии (server-interval-sec 60), затем subscribed
(service messenger-prod), дальше ping раз в 60 секунд; входящие кадры
не наблюдались — читаются и игнорируются.

Правило маршрутизации (dispatcher.handle): локальный канал обслуживает
только устройства в «Эмуляции»; «Прозрачно» и «Вместе» мостятся к настоящему
push.yandex.ru — там у устройства свой реальный OAuth и кадры отдаёт
настоящий сервис. Уведомлений канал не доставляет: push эмулятор шлёт
по uniproxy; задача канала — живая подписка, чтобы разрыв realtime станция
не читала как «нет интернета».
"""
import asyncio
import contextlib
import hashlib
import hmac
import json
import time
import uuid

from aiohttp import web

PING_INTERVAL = 60.0  # server-interval-sec образца; тесты сокращают подменой


def subscribe(key):
    """Ответ POST /push_subscribe: sign/ts локальной подписью (ключ QuasarBackend)."""
    ts = str(int(time.time()))
    sign = hmac.new(key, ts.encode(), hashlib.sha256).hexdigest()[:32]
    return {"sign": sign, "status": "ok", "ts": ts}


def verify(key, sign, ts):
    """Подписка query WS совпадает с локальной подписью ts?"""
    if not isinstance(sign, str) or not isinstance(ts, str) or not sign or not ts:
        return False
    expected = hmac.new(key, ts.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, sign)


def _ping_frame(interval):
    seconds = int(interval) if interval == int(interval) else interval
    return json.dumps({"operation": "ping", "server-interval-sec": seconds, "message": ""})


async def serve(request, runtime, ping_interval=None):
    """WS /v2/subscribe/websocket «Эмуляции»: подпись query проверяется локальным
    ключом (невалидная — кадр "bad sign" и закрытие), затем ping и subscribed
    по образцу, ping раз в интервал; входящее дренируется."""
    interval = PING_INTERVAL if ping_interval is None else ping_interval
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    if not verify(runtime.quasar.key, request.query.get('sign'), request.query.get('ts')):
        await ws.send_str('bad sign')
        await ws.close()
        return ws

    async def pinger():
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send_str(_ping_frame(interval))
        except (ConnectionResetError, RuntimeError):
            pass  # соединение закрыто — пинги больше не нужны

    pings = asyncio.create_task(pinger())
    try:
        await ws.send_str(_ping_frame(interval))
        await ws.send_str(json.dumps({"uid": request.query.get('user') or '',
                                      "service": "messenger-prod",
                                      "subscription-id": str(uuid.uuid4()),
                                      "operation": "subscribed", "event": "subscribed",
                                      "message": ""}))
        async for _frame in ws:  # кадры колонка → сервер не наблюдались: дренируем
            pass
    finally:
        pings.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pings
    return ws
