"""Пересылка HTTP и мост WS к настоящим серверам Яндекса (TASK-043).

Механизм без правил: какие запросы пересылать, решает Upstream.route (режимы
устройств, TASK-044). Пересылка идёт собственной клиентской сессией с коннектором
на UpstreamResolver — системный DNS может указывать хосты Яндекса на сам сервер.
TLS к Яндексу — с проверкой сертификата (по умолчанию aiohttp). Тело запроса уже
прочитано диспетчером и передаётся как есть; ответ буферизуется целиком (устройства
просят небольшие JSON/protobuf). Запись в журнал асинхронная — поток ответа диска
не ждёт.
"""
from __future__ import annotations

import asyncio
import time

from aiohttp import ClientSession, ClientTimeout, TCPConnector, WSMsgType, web
from multidict import CIMultiDict
from yarl import URL

from .journal import TrafficJournal
from .record import WsTap

HOP_BY_HOP = frozenset((
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"))
WS_HANDSHAKE = frozenset((
    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-protocol"))

CONNECT_TIMEOUT_S = 10
IDLE_TIMEOUT_S = 60
MAX_RESPONSE_BYTES = 32 * 1024 ** 2
MAX_WS_MESSAGE_BYTES = 16 * 1024 ** 2


class UpstreamError(RuntimeError):
    """Пересылка не удалась (резолв, соединение, лимит) — диспетчер отвечает 502."""


def decoded_json(headers, payload, maximum=8 * 1024 ** 2):
    """Тело ответа как JSON-объект: content-encoding снимается, лимит 8 МиБ.
    Не JSON и не распаковалось — None (TASK-052: quasar_direct_last / fetch)."""
    import json
    import zlib
    if not payload:
        return None
    raw = payload
    encoding = next((v for k, v in headers if str(k).lower() == "content-encoding"), "")
    if encoding.lower() in ("gzip", "deflate"):
        try:
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding.lower() == "gzip" else zlib.MAX_WBITS)
            raw = inflater.decompress(payload, maximum + 1)
            if len(raw) > maximum or not inflater.eof:
                return None
        except zlib.error:
            return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def forward_headers(headers, websocket=False):
    """Заголовки к пересылке: без hop-by-hop и перечисленных в Connection."""
    pairs = list(headers.items()) if hasattr(headers, "items") else [(k, v) for k, v in headers]
    excluded = set(HOP_BY_HOP)
    for key, value in pairs:
        if str(key).lower() == "connection":
            excluded |= {token.strip().lower() for token in str(value).split(",")}
    if websocket:
        excluded |= WS_HANDSHAKE
    return [(k, v) for k, v in pairs if k.lower() not in excluded]


class Forwarder:
    """Пересылка HTTP-обменов и WS-мостов upstream с журналированием."""

    def __init__(self, resolver, journal: TrafficJournal):
        self.resolver = resolver
        self.journal = journal
        self._session = None

    def session(self):
        if self._session is None or self._session.closed:
            connector = TCPConnector(resolver=self.resolver, use_dns_cache=False)
            self._session = ClientSession(
                connector=connector, auto_decompress=False, timeout=ClientTimeout(total=None),
                skip_auto_headers={"User-Agent", "Accept-Encoding", "Content-Type"})
        return self._session

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def serve(self, request, target, journal=True, **context):
        """Выполнить решение маршрута: WS — мост, HTTP — пересылка. Ответ — колонке.
        journal=False — обмен в журнал трафика не пишется (решает опция «Запись» устройства)."""
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.bridge_ws(request, target, journal=journal, **context)
        body = await request.read()
        status, headers, payload = await self.http(request.method, target, request.headers, body,
                                                   journal=journal, **context)
        return web.Response(status=status, body=payload, headers=CIMultiDict(headers))

    async def http(self, method, url, headers, body, journal=True, **context):
        """Переслать один HTTP-обмен наверх; (status, headers, payload) или UpstreamError."""
        start = time.monotonic()
        sent_headers = forward_headers(headers)
        try:
            timeout = ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT_S, sock_read=IDLE_TIMEOUT_S)
            async with self.session().request(method, URL(url, encoded=True),
                                              headers=CIMultiDict(sent_headers), data=body or None,
                                              allow_redirects=False, timeout=timeout) as response:
                payload = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    payload.extend(chunk)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise UpstreamError("Ответ upstream превышает 32 МиБ")
                status, response_headers = response.status, list(response.headers.items())
        except UpstreamError as exc:
            if journal:
                self.journal.write(kind="http", direction="exchange", status=502, error=str(exc)[:300],
                                   duration_ms=(time.monotonic() - start) * 1000, request_size=len(body or b""),
                                   response_size=0, request_headers=sent_headers, request_body=body or b"",
                                   source="upstream", **context)
            raise
        except Exception as exc:  # noqa: BLE001 — наверх всегда уходит UpstreamError
            error = f"{type(exc).__name__}: {exc}"[:300]
            if journal:
                self.journal.write(kind="http", direction="exchange", status=502, error=error,
                                   duration_ms=(time.monotonic() - start) * 1000, request_size=len(body or b""),
                                   response_size=0, request_headers=sent_headers, request_body=body or b"",
                                   source="upstream", **context)
            raise UpstreamError(error) from exc
        if journal:
            self.journal.write(kind="http", direction="exchange", status=status,
                               request_size=len(body or b""), response_size=len(payload),
                               duration_ms=(time.monotonic() - start) * 1000,
                               request_headers=sent_headers, response_headers=response_headers,
                               request_body=body or b"", response_body=bytes(payload),
                               source="upstream", **context)
        return status, forward_headers(response_headers), bytes(payload)

    async def bridge_ws(self, request, target, journal=True, tap=None, **context):
        """Мост WS: соединение колонки ↔ настоящий uniproxy.

        Двунаправленная пересылка текстовых и бинарных кадров, ping/pong, закрытие
        с обеих сторон (одна из задач завершилась — обе стороны закрываются).
        Запись в журнал — через tap (WsTap): опция «Запись» устройства читается на каждом
        кадре; без tap журнал не пишется. journal/context принимаются ради общей подписи
        с serve().
        """
        headers = forward_headers(request.headers, websocket=True)
        protocols = tuple(token.strip() for token in
                          request.headers.get("Sec-WebSocket-Protocol", "").split(",") if token.strip())
        try:
            remote = await asyncio.wait_for(
                self.session().ws_connect(URL(target, encoded=True), headers=CIMultiDict(headers),
                                          protocols=protocols, autoping=False, heartbeat=None,
                                          compress=0, max_msg_size=MAX_WS_MESSAGE_BYTES),
                CONNECT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — наверх всегда уходит UpstreamError
            raise UpstreamError(f"{type(exc).__name__}: {exc}"[:300]) from exc
        local = web.WebSocketResponse(protocols=[remote.protocol] if remote.protocol else (),
                                      autoping=False, heartbeat=None, compress=False,
                                      max_msg_size=MAX_WS_MESSAGE_BYTES)
        try:
            await local.prepare(request)
        except BaseException:
            await remote.close()
            raise
        if tap is None:
            tap = WsTap(self.journal, "upstream", mode=lambda: "off", handled=lambda trigger: False,
                        host=context.get("host"), path=context.get("path"))
        tap.start()

        async def pump(source, destination, direction):
            async for msg in source:
                if msg.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                    binary = msg.type == WSMsgType.BINARY
                    raw = msg.data if binary else str(msg.data).encode()
                    tap.frame(direction, binary, raw)
                    if binary:
                        await destination.send_bytes(msg.data)
                    else:
                        await destination.send_str(msg.data)
                elif msg.type == WSMsgType.PING:
                    await destination.ping(msg.data)
                elif msg.type == WSMsgType.PONG:
                    await destination.pong(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    raise source.exception() or RuntimeError("WebSocket error")
            await destination.close(code=source.close_code or 1000)

        tasks = (asyncio.create_task(pump(local, remote, "client_to_server")),
                 asyncio.create_task(pump(remote, local, "server_to_client")))
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await remote.close()
            await local.close()
            tap.close(remote.close_code)
        return local
