"""Локальный push-канал xiva (quasar-realtime) для «Эмуляции»: подпись подписки
локальным ключом (POST /push_subscribe → sign/ts, тот же ключ проверяет WS),
кадры по образцу живого обмена и долгожительство соединения (таймеры в тестах
сокращены)."""
import asyncio
import json
import time

from aiohttp import ClientSession, WSMsgType, web

from server_emulator import push_channel
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen
from tests.test_device_route_modes import alias_upstream, registry, start_upstream


def subscription_query(runtime, sn="SN-P", user="1002003", sign=None, ts=None):
    """Query WS-подписки по образцу: значения — из ответа /push_subscribe."""
    answer = push_channel.subscribe(runtime.quasar.key)
    return ("?service=quasar-realtime,messenger-prod&client=q"
            f"&session={sn}&sign={sign or answer['sign']}&ts={ts or answer['ts']}&user={user}")


async def test_subscribe_sign_roundtrip(tmp_path):
    """Подпись подписки: ts — срок действия подписки (у настоящего канала now + 8 часов:
    pushd станции держит соединение до этого срока, меньший ts даёт петлю переподписок);
    знак валиден локальным ключом, чужой ключ, изменённый знак или другой ts отбраковываются."""
    runtime = Runtime(tmp_path)
    try:
        answer = push_channel.subscribe(runtime.quasar.key)
        assert answer["status"] == "ok"
        assert len(answer["sign"]) == 32 and len(answer["ts"]) == 10
        assert int(answer["ts"]) - int(time.time()) == push_channel.SUBSCRIPTION_TTL
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
    """Кадры открытия по образцу живого обмена: ping (server-interval-sec 60), затем
    один subscribed для messenger-prod — независимо от списка сервисов подписки
    (uid = user подписки, subscription-id — 40 hex-символов)."""
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
                assert len(subscribed["subscription-id"]) == 40
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
                assert subscribed["operation"] == "subscribed" and subscribed["service"] == "messenger-prod"
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


async def test_subscribe_follows_route_mode(tmp_path):
    """Подписка на push-канал следует правилу маршрута: в «Эмуляции» — локальный
    знак (его проверяет локальный WS), в «Прозрачном» — настоящий сервис, иначе
    мост WS получает чужой знак и петля переподписок."""
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-PS", "user_id": "u", "device_ids": ["ps-id"]})
    calls = []
    upstream_runner, port = await start_upstream(calls)
    alias_upstream(runtime, port)
    runtime.identities.mutate({"operation": "route_mode", "sn": "SN-PS", "mode": "hybrid"})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/push_subscribe?device_id=ps-id&platform=yandexstation_2",
                                   headers={"Host": "quasar.yandex.net",
                                            "X-YaQuasarDeviceId": "ps-id"}) as response:
                assert response.status == 200
                assert await response.json() == {"from": "upstream",
                                                "path": "/push_subscribe?device_id=ps-id&platform=yandexstation_2"}
        assert ("http", "/push_subscribe?device_id=ps-id&platform=yandexstation_2") in calls

        runtime.identities.mutate({"operation": "route_mode", "sn": "SN-PS", "mode": "emulate"})
        async with ClientSession() as client:
            async with client.post(url + "/push_subscribe?device_id=ps-id&platform=yandexstation_2",
                                   headers={"Host": "quasar.yandex.net",
                                            "X-YaQuasarDeviceId": "ps-id"}) as response:
                assert response.status == 200
                answer = await response.json()
                assert answer["status"] == "ok"
                assert push_channel.verify(runtime.quasar.key, answer["sign"], answer["ts"])
        assert len(calls) == 1  # в «Эмуляции» подписка до заглушки не дошла
    finally:
        await upstream_runner.cleanup()
        await runner.cleanup()
        await runtime.close()


async def test_local_ws_journaled(tmp_path):
    """Соединение «Эмуляции» пишется в журнал по опции «Запись» устройства: open,
    кадры (ping, subscribed, кадр колонки) и close — хост push.yandex.ru, источник
    emulator. Ничего не пишется без опции или без опознанного устройства."""
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-PJ", "user_id": "u", "device_ids": ["pj-id"]})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url + "/v2/subscribe/websocket" + subscription_query(runtime, sn="SN-PJ"),
                                         headers={"Host": "alica.lan"}) as ws:
                await asyncio.wait_for(ws.receive(), 3)  # ping
                await asyncio.wait_for(ws.receive(), 3)  # subscribed
        await runtime.upstream.journal.flush()
        items = (await runtime.upstream.journal.list(limit=500))["items"]
        assert not [i for i in items if i["kind"] == "ws"]  # запись off — журнала нет

        runtime.identities.mutate({"operation": "record", "sn": "SN-PJ", "mode": "on"})
        async with ClientSession() as client:
            async with client.ws_connect(url + "/v2/subscribe/websocket" + subscription_query(runtime, sn="SN-PJ"),
                                         headers={"Host": "alica.lan"}) as ws:
                await asyncio.wait_for(ws.receive(), 3)  # ping
                await asyncio.wait_for(ws.receive(), 3)  # subscribed
                await ws.send_str('{"operation":"pong"}')
                await ws.close()
        for _ in range(60):
            await runtime.upstream.journal.flush()
            items = (await runtime.upstream.journal.list(limit=500))["items"]
            if any(i["kind"] == "ws" and i["event"] == "close" for i in items):
                break
            await asyncio.sleep(0.05)
        rows = [i for i in items if i["kind"] == "ws"]
        assert rows and all(i["host"] == "push.yandex.ru" and i["source"] == "emulator" for i in rows)
        assert all(i["device_sn"] == "SN-PJ" for i in rows)
        events = [i["event"] for i in rows]
        assert "open" in events and "close" in events
        # кадры обеих сторон: пинг/subscribed сервера и кадр колонки
        assert any(i["direction"] == "server_to_client" and i.get("response_size", 0) > 0 for i in rows)
        assert any(i["direction"] == "client_to_server" and i.get("request_size", 0) > 0 for i in rows)
    finally:
        await runner.cleanup()
        await runtime.close()
