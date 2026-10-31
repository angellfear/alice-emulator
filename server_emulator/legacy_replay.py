"""Isolated HTTP fixture replay + minimal typed UniProxy WS control scaffold."""
import argparse
import hashlib
import json
import logging
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web, WSMsgType

from .archive import Archive
from .schema import load_type

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("alice-emulator")


# Служебные события с каркасным ответом облака: событие колонки → ответ сервера
CONTROL_REPLIES = {"SynchronizeStateRequest": "SynchronizeStateResponse", "RequestStat": "RequestStatAck"}


def control_response(data):
    """Only protocol scaffolding; no ASR, TTS or assistant simulation."""
    if not data.startswith(b"AAPI"):
        return None
    client = load_type(ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TClientMessage").FromString(data[4:])
    if client.WhichOneof("Message") != "Event":
        return None
    kind = client.Event.WhichOneof("Event")
    if kind not in CONTROL_REPLIES:
        return None
    server = load_type(ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")()
    reply = getattr(server.Event, CONTROL_REPLIES[kind])
    reply.SetInParent()
    if kind == "SynchronizeStateRequest":
        # Каркас реального ответа облака (архив 10-20..10-23, прокси-эра):
        # {SessionId: uuid, ConnectionData: {}}. Пустой ответ станция считает
        # сбросом состояния и зажигает кольцо активации на каждом соединении.
        reply.SessionId = str(uuid.uuid4())
        reply.ConnectionData.SetInParent()
    server.Event.Header.MessageId = uuid.uuid4().hex
    server.Event.Header.RefMessageId = client.Event.Header.MessageId
    return b"AAPI" + server.SerializeToString()


def create_app(config, archive):
    routes = {}
    for event in archive.events():
        if event.get("kind") != "http" or config["response_role"] not in event.get("payloads", {}):
            continue
        target = urlsplit(event.get("upstream", ""))
        if not target.hostname:
            continue
        key = (target.hostname, event["method"], target.path)
        routes.setdefault(key, []).append(event)
    app = web.Application(client_max_size=16 * 1024**2)

    async def health(request):
        return web.json_response({"mode": "offline-scaffold", "routes": len(routes), "upstream": False})

    async def inventory(request):
        return web.json_response({"items": [{"host": k[0], "method": k[1], "path": k[2], "fixtures": len(v)} for k, v in sorted(routes.items())]})

    async def websocket(request):
        ws = web.WebSocketResponse(max_msg_size=16 * 1024**2, heartbeat=30)
        await ws.prepare(request)
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                data = message.data
                LOG.info("WS binary bytes=%s framing=%s", len(data), "AAPI" if data.startswith(b"AAPI") else "channel")
                try:
                    reply = control_response(data)
                except Exception as exc:
                    LOG.info("WS decode error=%s", type(exc).__name__)
                    await ws.close(code=1003, message=b"Invalid AAPI control frame")
                    break
                if reply:
                    await ws.send_bytes(reply)
            elif message.type == WSMsgType.TEXT:
                LOG.info("WS text bytes=%s (handler not implemented)", len(message.data.encode()))
        return ws

    async def replay(request):
        if request.path == "/uni.ws" and request.headers.get("Upgrade", "").lower() == "websocket":
            return await websocket(request)
        host = request.host.split(":", 1)[0].lower()
        host = config.get("aliases", {}).get(host, host)
        candidates = routes.get((host, request.method, request.path), [])
        data = await request.read()
        if config.get("strict_body", True):
            digest = hashlib.sha256(data).hexdigest()
            candidates = [e for e in candidates if (e["payloads"].get("request_sent") or e["payloads"].get("request_original") or {}).get("hash") == digest]
        if config.get("match_query"):
            candidates = [e for e in candidates if urlsplit(e["upstream"]).query == request.query_string]
        if not candidates:
            return web.json_response({"error": "fixture_not_found", "host": host, "method": request.method, "path": request.path}, status=404)
        event = candidates[-1]
        ref = event["payloads"][config["response_role"]]
        # Keep wire bytes + encoding together. Never replay cookies or hop headers.
        original_headers = event.get("headers", {}).get(config["response_role"], [])
        headers = {k: v for k, v in original_headers if k.lower() in ("content-type", "content-encoding", "retry-after")}
        headers["X-Emulator-Fixture"] = event["id"]
        LOG.info("HTTP %s %s fixture=%s", request.method, request.path, event["id"])
        return web.Response(status=event["status"], body=archive.payload(ref["hash"]), headers=headers)

    async def cleanup(app):
        archive.close()

    app.on_cleanup.append(cleanup)
    app.router.add_get("/_emulator/health", health)
    app.router.add_get("/_emulator/routes", inventory)
    app.router.add_route("*", "/{path:.*}", replay)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    database = (args.config.resolve().parent / config["database"]).resolve()
    logging.basicConfig(level=logging.INFO)
    # aiohttp default access logging includes query parameters; log paths ourselves.
    web.run_app(create_app(config, Archive(database)), host=config["host"], port=config["port"], access_log=None)


if __name__ == "__main__":
    main()
