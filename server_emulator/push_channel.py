"""Локальный push-канал xiva (quasar-realtime) для маршрута «Эмуляция».

Подписка POST /push_subscribe: sign/ts подписывает локальный ключ
(quasar-secret.json — QuasarBackend.key); тот же ключ станция предъявляет
в query при открытии WS /v2/subscribe/websocket, сервер канала проверяет
подпись при подключении. Кадры — точные по образцу живого обмена настоящего
канала (мост к push.yandex.ru): сразу после апгрейда ping
(server-interval-sec 60) и один subscribed (service messenger-prod,
subscription-id — 40 hex-символов), дальше ping раз в 60 секунд; входящие
кадры читаются, пишутся в журнал трафика (по опции «Запись») и дренируются.

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
import secrets
import time

from aiohttp import WSMsgType, web

PING_INTERVAL = 60.0  # server-interval-sec образца; тесты сокращают подменой
SUBSCRIPTION_TTL = 28800  # ts ответа = срок действия подписки: у настоящего канала now + 8 часов


def subscribe(key):
    """Ответ POST /push_subscribe: ts — срок действия подписки (у настоящего канала
    now + 8 часов; pushd станции держит соединение до этого срока), sign — подпись
    этого ts локальным ключом QuasarBackend."""
    ts = str(int(time.time()) + SUBSCRIPTION_TTL)
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
    # компактный JSON — байт-в-байт как у настоящего канала
    return json.dumps({"operation": "ping", "server-interval-sec": seconds, "message": ""},
                      separators=(",", ":"))


def _subscribed_frame(uid):
    """Кадр subscribed ровно как у настоящего канала: один, для messenger-prod
    (независимо от списка сервисов подписки), subscription-id — 40 hex-символов."""
    return json.dumps({"uid": uid, "service": "messenger-prod",
                       "subscription-id": secrets.token_hex(20),
                       "operation": "subscribed", "event": "subscribed",
                       "message": ""}, separators=(",", ":"))


async def serve(request, runtime, ping_interval=None, tap=None):
    """WS /v2/subscribe/websocket «Эмуляции»: подпись query проверяется локальным
    ключом (невалидная — кадр "bad sign" и закрытие), затем ping и subscribed
    по образцу настоящего канала, ping раз в интервал; кадры в обе стороны
    пишутся в журнал (tap — WsTap диспетчера) и дренируются."""
    interval = PING_INTERVAL if ping_interval is None else ping_interval
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    if not verify(runtime.quasar.key, request.query.get('sign'), request.query.get('ts')):
        if tap is not None:
            tap.frame("server_to_client", False, b"bad sign")
        await ws.send_str('bad sign')
        await ws.close()
        return ws

    async def pinger():
        try:
            while True:
                await asyncio.sleep(interval)
                frame = _ping_frame(interval)
                if tap is not None:
                    tap.frame("server_to_client", False, frame.encode())
                await ws.send_str(frame)
        except (ConnectionResetError, RuntimeError):
            pass  # соединение закрыто — пинги больше не нужны

    pings = asyncio.create_task(pinger())
    try:
        # кадры открытия — сразу после апгрейда, как у настоящего канала
        for text in [_ping_frame(interval), _subscribed_frame(request.query.get('user') or '')]:
            if tap is not None:
                tap.frame("server_to_client", False, text.encode())
            await ws.send_str(text)
        async for frame in ws:  # кадры колонки — в журнал и в дренаж
            if frame.type in (WSMsgType.TEXT, WSMsgType.BINARY) and tap is not None:
                data = frame.data if isinstance(frame.data, bytes) else frame.data.encode()
                tap.frame("client_to_server", frame.type == WSMsgType.BINARY, data)
    finally:
        pings.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pings
    return ws
