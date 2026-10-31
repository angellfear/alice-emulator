"""A02: reference fixtures decode, correlate, stay sanitized and replay on a fake device."""
import asyncio
import hashlib
import json
import re
import struct
from pathlib import Path

import pytest
from aiohttp import ClientSession, WSMsgType
from google.protobuf import json_format

from server_emulator.runtime import Runtime
from server_emulator.schema import load_type
from server_emulator.server import create_app

FIXTURES = Path(__file__).parent / "fixtures" / "emulator"
SCHEMAS = Path(__file__).resolve().parents[1] / "server_emulator" / "schemas"
NAMES = sorted(p.stem for p in FIXTURES.glob("*.json"))
SECRETS = ("Cookie", "Authorization", "Bearer ", "PRIVATE KEY", "BEGIN CERTIFICATE")
LONG_B64 = re.compile(r"[A-Za-z0-9+/]{200,}")
OAUTH_LIKE = re.compile(r"\b\d+\.\d+\.\d+\.\d+\.[0-9a-zA-Z_.-]{60,}")
CREDENTIAL_KEYS = ("OAuthToken", "ApiKey", "ICookie", "YexpCookie", "SaeCookie", "MegamindCookies",
                   "AccessToken", "RefreshToken")


def types():
    return (load_type(SCHEMAS, "alice-api.pb", "NAlice.NAliceApi.TClientMessage"),
            load_type(SCHEMAS, "alice-api.pb", "NAlice.NAliceApi.TServerMessage"))


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", NAMES)
def test_control_frames_roundtrip_through_descriptor(name):
    client_t, server_t = types()
    fixture = load(name)
    control = [m for m in fixture["messages"] if m["kind"] == "control" and "message" in m]
    assert control, "в fixture нет ни одного декодированного управляющего фрейма"
    for m in control:
        cls = client_t if m["dir"] == "c2s" else server_t
        parsed = cls()
        json_format.ParseDict(m["message"], parsed, descriptor_pool=cls.DESCRIPTOR.file.pool)
        # Повторная сериализация и разбор: санитизация не сломала wire-структуру.
        reparsed = cls.FromString(parsed.SerializeToString())
        assert json_format.MessageToDict(reparsed, preserving_proto_field_name=True,
                                         descriptor_pool=cls.DESCRIPTOR.file.pool) == m["message"]


@pytest.mark.parametrize("name", NAMES)
def test_correlation_links_and_order(name):
    fixture = load(name)
    ids, refs = set(), set()
    last_t = -10**9
    for m in fixture["messages"]:
        assert m["t_ms"] >= last_t, "нарушен порядок сообщений"
        last_t = m["t_ms"]
        if m["kind"] != "control" or "message" not in m:
            continue
        header = m["message"].get("Event", {}).get("Header", {})
        if header.get("MessageId"):
            ids.add(header["MessageId"])
        if header.get("RefMessageId"):
            refs.add((m["label"], header["RefMessageId"]))
    external = set(fixture.get("external_references", []))
    for label, ref in refs:
        assert ref in ids or ref in external, f"{label}: ссылка {ref} ни на сообщение эпизода, ни на объявленную внешнюю"
    if fixture["name"] == "cancel-request":
        cancel = next(m for m in fixture["messages"] if m.get("label") == "CancelRequest")
        target = cancel["message"]["Event"]["CancelRequest"]["RequestMessageId"]
        assert target in ids, "CancelRequest должен ссылаться на отменяемое сообщение внутри эпизода"


@pytest.mark.parametrize("name", NAMES)
def test_no_secrets_and_no_raw_audio(name):
    fixture = load(name)
    text = json.dumps(fixture, ensure_ascii=False)
    for marker in SECRETS:
        assert marker not in text
    assert not LONG_B64.search(text), "остался длинный base64-блоб"
    assert not OAUTH_LIKE.search(text), "остался похожий на живой OAuth-токен"
    for key in CREDENTIAL_KEYS:
        assert not re.search(rf'"{key}":\s*"(?!<redacted)[^"]+"', text), f"поле {key} не закрыто"
    for m in fixture["messages"]:
        if m["kind"] == "audio":
            assert m.get("synthetic"), "аудио должно быть заменено синтетическим маркером"
    assert fixture["schema"]["sha256"] == hashlib.sha256((Path(__file__).resolve().parents[1] / fixture["schema"]["file"]).read_bytes()).hexdigest()


async def test_fake_device_replays_client_side_without_upstream(tmp_path):
    """Все c2s-фреймы fixture воспроизводятся fake-device клиентом на живом эмуляторе.

    Один fixture = одно WS-соединение одного устройства: смена DeviceId внутри
    соединения закрывается эмулятором осознанно (1008).
    """
    runtime = Runtime(tmp_path)
    runner = __import__("aiohttp").web.AppRunner(create_app(runtime=runtime))
    await runner.setup()
    from aiohttp import web
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    client_t, _ = types()
    try:
        async with ClientSession() as client:
            for name in NAMES:
                async with client.ws_connect(f"http://127.0.0.1:{port}/uni.ws") as ws:
                    for m in load(name)["messages"]:
                        if m.get("connection"):  # второе соединение reconnect — за пределами этого теста
                            continue
                        if m["kind"] == "control" and m["dir"] == "c2s":
                            parsed = json_format.ParseDict(m["message"], client_t(),
                                                           descriptor_pool=client_t.DESCRIPTOR.file.pool)
                            await ws.send_bytes(b"AAPI" + parsed.SerializeToString())
                        elif m["kind"] == "audio":
                            await ws.send_bytes(struct.pack(">I", m["channel"]) + b"\0" * min(m["bytes"], 128))
                        try:
                            got = await ws.receive(timeout=0.01)  # осушаем входящие, тишина не ошибка
                            if got.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
                                pytest.fail(f"{name}: эмулятор закрыл соединение во время replay "
                                            f"({got.type}, код {ws.close_code}) — вероятен 1011/1008")
                        except asyncio.TimeoutError:
                            pass
                    await ws.close()
    finally:
        await runner.cleanup()
