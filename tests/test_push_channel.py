"""Локальный push-канал xiva (quasar-realtime) для «Эмуляции»: подпись подписки
локальным ключом (POST /push_subscribe → sign/ts, тот же ключ проверяет WS),
кадры по образцу живого обмена и долгожительство соединения (таймеры в тестах
сокращены)."""
import asyncio
import json

from aiohttp import ClientSession, WSMsgType, web

from server_emulator import push_channel
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen


def subscription_query(runtime, sn="SN-P", user="1002003", sign=None, ts=None):
    """Query WS-подписки по образцу: значения — из ответа /push_subscribe."""
    answer = push_channel.subscribe(runtime.quasar.key)
    return ("?service=quasar-realtime,messenger-prod&client=q"
            f"&session={sn}&sign={sign or answer['sign']}&ts={ts or answer['ts']}&user={user}")


async def test_subscribe_sign_roundtrip(tmp_path):
    """Подпись подписки: валидна локальным ключом; чужой ключ, изменённый знак
    или другой ts отбраковываются."""
    runtime = Runtime(tmp_path)
    try:
        answer = push_channel.subscribe(runtime.quasar.key)
        assert answer["status"] == "ok"
        assert len(answer["sign"]) == 32 and len(answer["ts"]) == 10
        assert push_channel.verify(runtime.quasar.key, answer["sign"], answer["ts"])
        assert not push_channel.verify(bytes(32), answer["sign"], answer["ts"])
        tampered = answer["sign"][:-1] + ("0" if answer["sign"][-1] != "0" else "1")
        assert not push_channel.verify(runtime.quasar.key, tampered, answer["ts"])
        stale = str(int(answer["ts"]) + 60)
        assert not push_channel.verify(runtime.quasar.key, answer["sign"], stale)
        assert not push_channel.verify(runtime.quasar.key, "", answer["ts"])
        assert not push_channel.verify(runtime.quasar.key, answer["sign"], None)
    finally:
        await runtime.close()


async def test_ws_frames_per_sample(tmp_path):
    """Кадры открытия по образцу: ping (server-interval-sec 60), затем subscribed
    (service messenger-prod, uid = user подписки)."""
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url + "/v2/subscribe/websocket" + subscription_query(runtime),
                                         headers={"Host": "alica.lan"}) as ws:
                ping = json.loads((await asyncio.wait_for(ws.receive(), 3)).data)
                assert ping == {"operation": "ping", "server-interval-sec": 60, "message": ""}
                subscribed = json.loads((await asyncio.wait_for(ws.receive(), 3)).data)
                assert subscribed["service"] == "messenger-prod" and subscribed["uid"] == "1002003"
                assert subscribed["subscription-id"]
                assert subscribed["operation"] == "subscribed" and subscribed["event"] == "subscribed"
                assert subscribed["message"] == ""
                assert ws.closed is False
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_ws_bad_sign_closes(tmp_path):
    """Подписка с чужой подписью: кадр "bad sign" и закрытие — как у настоящего сервиса."""
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        query = subscription_query(runtime, sign="0" * 32, ts="1700000000")
        async with ClientSession() as client:
            async with client.ws_connect(url + "/v2/subscribe/websocket" + query,
                                         headers={"Host": "alica.lan"}) as ws:
                first = await asyncio.wait_for(ws.receive(), 3)
                assert first.type == WSMsgType.TEXT and first.data == "bad sign"
                assert (await asyncio.wait_for(ws.receive(), 3)).type == WSMsgType.CLOSE
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_ws_connection_lives(tmp_path, monkeypatch):
    """Соединение живёт: 60 пингов по сокращённому интервалу (0,05 с) — час
    протокольного времени, больше 3 минут жизни прежнего канала."""
    monkeypatch.setattr(push_channel, "PING_INTERVAL", 0.05)
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url + "/v2/subscribe/websocket" + subscription_query(runtime),
                                         headers={"Host": "alica.lan"}) as ws:
                first = json.loads((await asyncio.wait_for(ws.receive(), 3)).data)
                assert first["operation"] == "ping" and first["server-interval-sec"] == 0.05
                subscribed = json.loads((await asyncio.wait_for(ws.receive(), 3)).data)
                assert subscribed["operation"] == "subscribed"
                pings = 0
                while pings < 60:
                    frame = json.loads((await asyncio.wait_for(ws.receive(), 3)).data)
                    assert frame["operation"] == "ping"
                    pings += 1
                assert ws.closed is False
                await ws.send_str("station-frame")  # входящее дренируется, канал не рвётся
                assert json.loads((await asyncio.wait_for(ws.receive(), 3)).data)["operation"] == "ping"
    finally:
        await runner.cleanup()
        await runtime.close()
