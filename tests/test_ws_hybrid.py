"""091/097/098: маршрут «Вместе» — фаза 3, промах уходит Яндексу ГОЛОСОМ.

Подменный uniproxy (aiohttp, AAPI): приносит SynchronizeStateRequest колонки, на
VoiceInput (пересланный голосовой ход) отвечает InputStartAck + AliceResponse (Ref =
MessageId фразы) + кадром звука; умеет прислать Push и рвать соединение. Проверяются:
релей голосового хода (VoiceInput + аудио + синтез Close), ответы ветки колонке — только
цепочка ответа (без чужих InputStartAck/Push), отказ ветки — прежний путь и recent;
sync-first, журнал ветки по «Записи», релеи протокольных ответов колонки (StreamControl
по потоку ветки, Ack по её кадру), RequestStat — не релеится, TextInput-промах — прежний
путь (текстовая пересылка удалена как мёртвая), сценарий в «Вместе» отвечает сам.
"""
import asyncio
import pathlib
import struct
import tempfile

import pytest
from aiohttp import web

from tests.conftest import listen

from server_emulator.runtime import Runtime
from server_emulator.schema import load_type

SN = "SN-H"
PCM_FORMAT = "audio/x-pcm;bit=16;rate=16000;channels=1"
PCM_CHUNK = b"\x00\x01" * 320  # 20 мс s16le 16 кГц


def _types():
    return (load_type("server_emulator/schemas", "alice-api.pb", "NAlice.NAliceApi.TClientMessage"),
            load_type("server_emulator/schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage"))


class StubUniproxy:
    """Подменный uniproxy: собирает кадры, на VoiceInput отвечает по сценарию теста."""

    def __init__(self, answer=True, audio=False, push=False, fail=False):
        self.answer, self.audio, self.push, self.fail = answer, audio, push, fail
        self.frames = []  # (direction, raw)
        self.voices = []  # MessageId пересланных VoiceInput
        self.closes = []  # (StreamId, Reason) StreamControl Close от «колонки»/релея
        self.opened = 0

    async def handle(self, request):
        client_type, server_type = _types()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.opened += 1
        if self.fail:
            await ws.close()
            return ws
        async for packet in ws:
            if packet.type != web.WSMsgType.BINARY:
                continue
            raw = packet.data
            self.frames.append(("client", raw))
            if not raw.startswith(b"AAPI"):
                continue  # аудиокадр колонки — не protobuf
            body = raw[4:]
            message = client_type.FromString(body)
            which = message.WhichOneof("Message")
            if which == "StreamControl":
                if message.StreamControl.HasField("Close"):
                    self.closes.append((message.StreamControl.StreamId, message.StreamControl.Close.Reason))
                continue
            if which != "Event" or message.Event.WhichOneof("Event") != "VoiceInput":
                continue
            mid = message.Event.Header.MessageId
            self.voices.append(mid)
            if not self.answer:
                continue
            ack = server_type()
            ack.Event.Header.RefMessageId = mid  # протокольный кадр ветки — колонке не идёт
            out = b"AAPI" + ack.SerializeToString()
            self.frames.append(("server", out))
            await ws.send_bytes(out)
            reply = server_type()
            reply.Event.Header.RefMessageId = mid
            reply.Event.Header.MessageId = "srv-mid"  # Ack колонки будет релеен ветке
            reply.Event.AliceResponse.Response.Cards.add(Type="simple_text", Text="Ответ Яндекса")
            out = b"AAPI" + reply.SerializeToString()
            self.frames.append(("server", out))
            await ws.send_bytes(out)
            if self.audio:  # бинарный кадр звука ветки (не AAPI)
                await ws.send_bytes(b"\x01sound-bytes")
                self.frames.append(("server", b"\x01sound-bytes"))
            if self.push:
                push = server_type()
                push.Event.Push.SetInParent()
                out = b"AAPI" + push.SerializeToString()
                self.frames.append(("server", out))
                await ws.send_bytes(out)
            stream = server_type()  # поток ветки (TTS): его Close колонки релеится назад
            stream.StreamControl.StreamId = 4
            stream.StreamControl.Close.SetInParent()
            out = b"AAPI" + stream.SerializeToString()
            self.frames.append(("server", out))
            await ws.send_bytes(out)
        return ws


async def make_stub(**kwargs):
    """Подменный uniproxy на порту: (стаб, base для алиаса upstream)."""
    stub = StubUniproxy(**kwargs)
    app = web.Application()
    app.router.add_get("/uni.ws", stub.handle)
    runner, url = await listen(app)
    return stub, url


def _runtime(tmp_path, stub_base, mode="hybrid"):
    runtime = Runtime(tmp_path)
    runtime.settings["intelligence"] = "scenarios"
    runtime.settings["aliases"] = {"127.0.0.1": stub_base}
    runtime.identities.mutate({"operation": "user", "id": "owner", "name": "O"})
    runtime.identities.mutate({"operation": "device", "sn": SN, "user_id": "owner",
                               "device_ids": ["dev-h"], "route_mode": mode,
                               "profile": {"aapi_model": True, "aapi_voice": True}})
    return runtime


def _client_app(runtime):
    from server_emulator.server import create_app
    return create_app(runtime=runtime)


def _stub_stt(runtime, text="включи фильм дюна"):
    """Распознавание — заглушка: текст известен, сценария нет → промах → ветка."""
    calls = []

    async def fake_provider(kind, payload=None, audio=None, filename="input.wav"):
        calls.append(kind)
        return ('{"text": "%s"}' % text, None)

    runtime.provider = fake_provider
    return calls


async def _voice_exchange(ws, client_type, mid="msg-v"):
    """Голосовой ход «колонки»: VoiceInput + два аудиокадра (4-байтовый канал-префикс +
    PCM) + Close(STOP_RECORDING) — при буферном ASR он запускает распознавание; колонка
    шлёт его после endpoint-решения сервера."""
    voice = client_type()
    voice.Event.Header.MessageId = mid
    voice.Event.Header.StreamId = 7
    voice.Event.VoiceInput.Header.RequestId = "rq-v"
    voice.Event.VoiceInput.Format = PCM_FORMAT
    voice.Event.VoiceInput.Request.Event.Type = 1
    await ws.send_bytes(b"AAPI" + voice.SerializeToString())
    for _ in range(2):
        await ws.send_bytes(struct.pack(">I", 7) + PCM_CHUNK)
    close = client_type()
    close.StreamControl.StreamId = 7
    close.StreamControl.Close.Reason = 2  # STOP_RECORDING
    await ws.send_bytes(b"AAPI" + close.SerializeToString())


async def test_voice_miss_relays_and_answer_reaches_device(tmp_path):
    """Фаза 3: промах — ветке уходит голосовой ход (sync → VoiceInput → аудио → синтез
    Close(7, STOP_RECORDING)), ответ Яндекса (AliceResponse + звук) — колонке; чужие
    InputStartAck и Push — нет; модель и «Без ответа» — мимо."""
    stub, stub_base = await make_stub(audio=True, push=True)
    runtime = _runtime(tempfile.mkdtemp(), stub_base)
    calls = _stub_stt(runtime)

    async def chat(text, **kwargs):
        raise AssertionError("модель не должна вызываться, пока наверх есть связь")

    runtime.chat = chat
    runner, url = await listen(_client_app(runtime))
    try:
        client_type, server_type = _types()
        from aiohttp import ClientSession
        async with ClientSession() as client:
            async with client.ws_connect(url + "/uni.ws",
                                         headers={"x-uprx-device-id": "dev-h"}) as ws:
                sync = client_type()
                sync.Event.Header.MessageId = "sync-id"
                sync.Event.SynchronizeStateRequest.Application.DeviceId = "dev-h"
                sync.Event.SynchronizeStateRequest.OAuthToken = "test-token"
                await ws.send_bytes(b"AAPI" + sync.SerializeToString())
                await asyncio.sleep(0.2)
                await _voice_exchange(ws, client_type)
                got_binary = got_alice = got_push = False
                ack_count = 0
                while True:
                    try:
                        packet = await asyncio.wait_for(ws.receive(), 6)
                    except asyncio.TimeoutError:
                        break  # поток опустел — конец обмена
                    data = packet.data
                    if not data.startswith(b"AAPI"):
                        got_binary = data == b"\x01sound-bytes"
                        continue
                    reply = server_type.FromString(data[4:])
                    which = reply.Event.WhichOneof("Event")
                    if which == "AliceResponse":
                        got_alice = True
                        assert "Ответ Яндекса" in reply.Event.AliceResponse.Response.Cards[0].Text
                    elif which == "InputStartAck" and reply.Event.Header.RefMessageId == "msg-v":
                        ack_count += 1  # свой InputStartAck эмулятора; веткин не должен пройти
                    if which == "Push":
                        got_push = True
                assert calls == ["stt"]  # свой ASR отработал, промах ушёл ветке
                assert got_alice and got_binary, "ответ и звук ветки — колонке"
                assert ack_count == 1, "InputStartAck — только свой; веткин не пересылается"
                assert not got_push, "Push Яндекса колонке не пересылается"
                assert stub.voices == ["msg-v"] and stub.frames[0][0] == "client"
                # ветка получила ход целиком: sync, VoiceInput, аудио ×2, синтез Close(7, 2)
                kinds = []
                for direction, raw in stub.frames:
                    if direction != "client":
                        continue
                    if not raw.startswith(b"AAPI"):
                        kinds.append("audio")
                        continue
                    m = client_type.FromString(raw[4:])
                    if m.WhichOneof("Message") == "StreamControl":
                        kinds.append(("close", m.StreamControl.StreamId, m.StreamControl.Close.Reason))
                    elif m.Event.WhichOneof("Event") == "VoiceInput":
                        kinds.append("voice")
                    elif m.Event.WhichOneof("Event") == "SynchronizeStateRequest":
                        kinds.append("sync")
                assert kinds[0] == "sync" and "voice" in kinds
                assert kinds.count("audio") == 2 and ("close", 7, 2) in kinds
                assert "включи фильм дюна" not in str(getattr(runtime, "phrase_misses", {}))
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_no_answer_falls_back_to_previous_path(tmp_path):
    """Отказ ветки (нет ответа) — прежний путь и запись в recent."""
    stub, stub_base = await make_stub(answer=False)
    runtime = _runtime(tempfile.mkdtemp(), stub_base)
    _stub_stt(runtime)
    seen = {"called": False}

    async def chat(text, **kwargs):
        seen["called"] = True
        return {"text": "Ответ модели", "tool_calls": [], "usage": {}, "source": "stub"}

    runtime.chat = chat
    runner, url = await listen(_client_app(runtime))
    try:
        client_type, server_type = _types()
        from aiohttp import ClientSession
        async with ClientSession() as client:
            async with client.ws_connect(url + "/uni.ws",
                                         headers={"x-uprx-device-id": "dev-h"}) as ws:
                sync = client_type()
                sync.Event.Header.MessageId = "sync-id"
                sync.Event.SynchronizeStateRequest.Application.DeviceId = "dev-h"
                await ws.send_bytes(b"AAPI" + sync.SerializeToString())
                await asyncio.sleep(0.2)
                await _voice_exchange(ws, client_type)
                for _ in range(4):
                    try:
                        await asyncio.wait_for(ws.receive(), 13)  # 10 с таймаут ветки + запас
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                # recent — до закрытия соединения: scoped-копии чистятся при выходе
                recents = [str(item) for s in runtime.sessions.values()
                           for item in ((s.get("runtime") or {}).recent or [])] + [str(i) for i in runtime.recent]
        assert seen["called"], "после отказа ветки работает прежний путь (модель)"
        assert any("Вместе: Яндекс не ответил вовремя" in item for item in recents), recents
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_textinput_miss_skips_branch(tmp_path):
    """TextInput-промах (голосового хода нет) — ветка не открывается: текстовая пересылка
    удалена как мёртвая (097), фраза уходит прежнему пути."""
    stub, stub_base = await make_stub()
    runtime = _runtime(tempfile.mkdtemp(), stub_base)
    runner, url = await listen(_client_app(runtime))
    try:
        client_type, server_type = _types()
        from aiohttp import ClientSession
        async with ClientSession() as client:
            async with client.ws_connect(url + "/uni.ws",
                                         headers={"x-uprx-device-id": "dev-h"}) as ws:
                sync = client_type()
                sync.Event.Header.MessageId = "sync-id"
                sync.Event.SynchronizeStateRequest.Application.DeviceId = "dev-h"
                await ws.send_bytes(b"AAPI" + sync.SerializeToString())
                await asyncio.sleep(0.2)
                message = client_type()
                message.Event.Header.MessageId = "msg-t"
                message.Event.TextInput.Header.RequestId = "rq"
                message.Event.TextInput.Request.Event.Text = "кто такой пушкин"
                await ws.send_bytes(b"AAPI" + message.SerializeToString())
                await asyncio.sleep(1.0)
        assert stub.opened == 0, "текстовая пересылка удалена — ветка не открывается"
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_scenario_answers_itself_in_hybrid(tmp_path):
    """Свой сценарий в «Вместе» отвечает сам — ветка не открывается."""
    stub, stub_base = await make_stub()
    tmp = tempfile.mkdtemp()
    runtime = _runtime(tmp, stub_base)
    scenario_dir = pathlib.Path(tmp) / "scenarios"
    scenario_dir.mkdir(exist_ok=True)
    scenario_dir.joinpath("user.test.check.json").write_text(
        '{"format_version": 2, "id": "user.test.check", "version": "1.0.0", "category": "custom",'
        ' "priority": 100, "triggers": [{"kind": "phrase", "phrases": ["проверка сценариев"]}],'
        ' "flow": [{"id": "r", "op": "compose", "reply": {"text": "Сценарий работает"}},'
        ' {"id": "f", "op": "finish"}]}', encoding="utf-8")
    runtime.settings_service.load_scenario_files(runtime.settings)  # файл до каталога — как в бою
    runner, url = await listen(_client_app(runtime))
    try:
        client_type, server_type = _types()
        from aiohttp import ClientSession
        async with ClientSession() as client:
            async with client.ws_connect(url + "/uni.ws",
                                         headers={"x-uprx-device-id": "dev-h"}) as ws:
                sync = client_type()
                sync.Event.Header.MessageId = "sync-id"
                sync.Event.SynchronizeStateRequest.Application.DeviceId = "dev-h"
                await ws.send_bytes(b"AAPI" + sync.SerializeToString())
                await asyncio.sleep(0.2)
                message = client_type()
                message.Event.Header.MessageId = "msg-s"
                message.Event.TextInput.Header.RequestId = "rq"
                message.Event.TextInput.Request.Event.Text = "проверка сценариев"
                await ws.send_bytes(b"AAPI" + message.SerializeToString())
                answered = False
                for _ in range(6):
                    try:
                        packet = await asyncio.wait_for(ws.receive(), 3)
                    except asyncio.TimeoutError:
                        break
                    if isinstance(packet.data, int) or not packet.data.startswith(b"AAPI"):
                        continue
                    reply = server_type.FromString(packet.data[4:])
                    if reply.Event.WhichOneof("Event") == "AliceResponse":
                        cards = [c.Text for c in reply.Event.AliceResponse.Response.Cards]
                        if "Сценарий работает" in cards:
                                answered = True
                assert answered and stub.voices == []
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_branch_frames_journaled_per_record_option(tmp_path):
    """Кадры ветки «Вместе» пишутся в журнал трафика по опции «Запись» устройства:
    включена — кадры с sn и source=hybrid; выключена — ничего. Дефект 097: HybridSession
    создавался без sn — record_mode(None) = «off», ветка не писалась никогда."""
    for record in ("on", "off"):
        stub, stub_base = await make_stub()
        runtime = _runtime(tempfile.mkdtemp(), stub_base)
        runtime.identities.mutate({"operation": "record", "sn": SN, "mode": record})
        _stub_stt(runtime)
        runner, url = await listen(_client_app(runtime))
        try:
            client_type, server_type = _types()
            from aiohttp import ClientSession
            async with ClientSession() as client:
                async with client.ws_connect(url + "/uni.ws",
                                             headers={"x-uprx-device-id": "dev-h"}) as ws:
                    sync = client_type()
                    sync.Event.Header.MessageId = "sync-id"
                    sync.Event.SynchronizeStateRequest.Application.DeviceId = "dev-h"
                    await ws.send_bytes(b"AAPI" + sync.SerializeToString())
                    await asyncio.sleep(0.2)
                    await _voice_exchange(ws, client_type)
                    await asyncio.sleep(1.0)
            rows = runtime.upstream.journal.list(source="hybrid")["items"]
            if record == "on":
                assert rows, "ветка должна писаться при «Запись: Включено»"
                assert {"client_to_server", "server_to_client"} <= {r["direction"] for r in rows}
                assert all(r["device_sn"] == SN for r in rows)
                voice = next(r for r in rows if r["message_id"] == "msg-v")
                full = runtime.upstream.journal.item(voice["id"])
                client_type, _ = _types()
                replayed = client_type.FromString(full["request_body"][4:])
                assert replayed.Event.WhichOneof("Event") == "VoiceInput"
                assert replayed.Event.VoiceInput.Format == PCM_FORMAT
            else:
                assert rows == [], "при выключенной записи ветка не пишется"
        finally:
            await runner.cleanup()
            await runtime.close()


async def test_column_protocol_replies_relayed_to_branch(tmp_path):
    """Релей (097): StreamControl по потоку ветки и Ack по кадру ветки уходят в Яндекс —
    без них её диалог «подвисает». Чужой поток (9) — не релеится."""
    stub, stub_base = await make_stub()
    runtime = _runtime(tempfile.mkdtemp(), stub_base)
    _stub_stt(runtime)
    runner, url = await listen(_client_app(runtime))
    try:
        client_type, server_type = _types()
        from aiohttp import ClientSession
        async with ClientSession() as client:
            async with client.ws_connect(url + "/uni.ws",
                                         headers={"x-uprx-device-id": "dev-h"}) as ws:
                sync = client_type()
                sync.Event.Header.MessageId = "sync-id"
                sync.Event.SynchronizeStateRequest.Application.DeviceId = "dev-h"
                await ws.send_bytes(b"AAPI" + sync.SerializeToString())
                await asyncio.sleep(0.2)
                await _voice_exchange(ws, client_type)
                await asyncio.sleep(0.8)  # ветка ответила: srv-mid в branch_messages, поток 4
                ack = client_type()
                ack.Event.Ack.ReceivedHeader.MessageId = "srv-mid"
                await ws.send_bytes(b"AAPI" + ack.SerializeToString())
                close_own = client_type()
                close_own.StreamControl.StreamId = 4
                close_own.StreamControl.Close.SetInParent()
                await ws.send_bytes(b"AAPI" + close_own.SerializeToString())
                close_other = client_type()
                close_other.StreamControl.StreamId = 9
                close_other.StreamControl.Close.SetInParent()
                await ws.send_bytes(b"AAPI" + close_other.SerializeToString())
                await asyncio.sleep(0.5)
        relayed = []
        for direction, raw in stub.frames:
            if direction != "client" or not raw.startswith(b"AAPI"):
                continue
            m = client_type.FromString(raw[4:])
            if m.WhichOneof("Message") == "StreamControl":
                relayed.append(("stream", m.StreamControl.StreamId))
            elif m.Event.WhichOneof("Event") == "Ack":
                relayed.append(("ack", m.Event.Ack.ReceivedHeader.MessageId))
        assert ("ack", "srv-mid") in relayed, "Ack по кадру ветки — ветке"
        assert ("stream", 4) in relayed, "Close потока ветки — ветке"
        assert ("stream", 9) not in relayed, "чужой поток — не релеится"
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_request_stat_not_relayed(tmp_path):
    """RequestStat не релеится ветке: колонка шлёт его по завершении хода, а в «Вместе»
    он уходил сразу после заглушки — мегамайнда отменяла отложенный ответ (дефект 097)."""
    stub, stub_base = await make_stub()
    runtime = _runtime(tempfile.mkdtemp(), stub_base)
    _stub_stt(runtime)
    runner, url = await listen(_client_app(runtime))
    try:
        client_type, server_type = _types()
        from aiohttp import ClientSession
        async with ClientSession() as client:
            async with client.ws_connect(url + "/uni.ws",
                                         headers={"x-uprx-device-id": "dev-h"}) as ws:
                sync = client_type()
                sync.Event.Header.MessageId = "sync-id"
                sync.Event.SynchronizeStateRequest.Application.DeviceId = "dev-h"
                await ws.send_bytes(b"AAPI" + sync.SerializeToString())
                await asyncio.sleep(0.2)
                await _voice_exchange(ws, client_type)
                await asyncio.sleep(0.8)
                stat = client_type()
                stat.Event.RequestStat.SetInParent()
                await ws.send_bytes(b"AAPI" + stat.SerializeToString())
                await asyncio.sleep(0.5)
        relayed = [client_type.FromString(raw[4:]) for direction, raw in stub.frames
                   if direction == "client" and raw.startswith(b"AAPI")]
        assert any(m.WhichOneof("Message") == "Event"
                   and m.Event.WhichOneof("Event") == "VoiceInput" for m in relayed)
        assert not any(m.WhichOneof("Message") == "Event"
                       and m.Event.WhichOneof("Event") == "RequestStat" for m in relayed), \
            "RequestStat не релеится"
    finally:
        await runner.cleanup()
        await runtime.close()
