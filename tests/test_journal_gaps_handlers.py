"""TASK-072: инструмент journal_gaps (отчёт по копии журнала, маскирование) и обработчики
пробелов: push_subscribe, галерея, set_device_capability, кнопки экрана плеера."""
import json
import sqlite3
import tempfile
import time

from google.protobuf import json_format

from server_emulator.dispatcher import Dispatcher
from server_emulator.endpoints import find_builtin
from server_emulator.media import MediaLibrary, Player
from server_emulator.runtime import ROOT, Runtime
from server_emulator.schema import load_type
from server_emulator.server import create_app
from tests.conftest import listen
from tests.test_timesync_scenario import ctx_for, dispatch_handle

CLIENT = load_type(ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TClientMessage")
SERVER = load_type(ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")

SCHEMA = """
CREATE TABLE exchanges(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, direction TEXT, event TEXT,
  host TEXT, method TEXT, path TEXT, status INTEGER, device_sn TEXT, source TEXT,
  unhandled INTEGER DEFAULT 0, key TEXT, request_headers TEXT, response_headers TEXT,
  request_body BLOB, response_body BLOB);
"""


def make_journal(tmp_path):
    """Синтетический журнал: HTTP с секретом в ответе, WS-кадры AAPI, AsrResult с фразой."""
    import zstandard

    path = tmp_path / "traffic.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    compress = lambda data: zstandard.compress(data)

    def aapi(message):
        return compress(b"AAPI" + message.SerializeToString())

    db.execute("INSERT INTO exchanges(kind, host, method, path, status, source, unhandled, key,"
               " response_headers, response_body) VALUES('http','quasar.yandex.net','POST',"
               "'/push_subscribe?device_id=C00T10001B6SAK&uuid=6205228',200,'upstream',1,"
               "'POST quasar.yandex.net /push_subscribe','[]',?)",
               (compress(b'{"sign":"a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6","status":"ok","ts":"1793545598"}'),))
    db.execute("INSERT INTO exchanges(kind, host, method, path, status, source, unhandled, response_body)"
               " VALUES('http','rpc.alice.yandex.net','POST','/gproxy/centaur_get_gallery',200,"
               "'emulator',1,'')")
    matched = json_format.ParseDict({"Event": {"Header": {"MessageId": "msg-matched"},
                                               "MatchedUser": {"Header": {"RequestId": "req"}}}}, CLIENT())
    unknown = json_format.ParseDict({"Event": {"Header": {"MessageId": "msg-unknown"},
                                               "ContinueRequest": {"RequestMessageId": "x"}}}, CLIENT())
    asr = json_format.ParseDict({"Event": {"Header": {"RefMessageId": "msg-voice"},
                                           "AsrResult": {"Recognition": [
                                               {"Words": [{"Value": "покажи"}, {"Value": "глазки"}]}]}}},
                                SERVER())
    reply = json_format.ParseDict({"Event": {"Header": {"RefMessageId": "msg-matched"},
                                             "AliceResponse": {"Response": {"Directives": []}}}}, SERVER())
    db.execute("INSERT INTO exchanges(kind, direction, request_body) VALUES('ws','client_to_server',?)",
               (aapi(matched),))
    db.execute("INSERT INTO exchanges(kind, direction, request_body) VALUES('ws','client_to_server',?)",
               (aapi(unknown),))
    db.execute("INSERT INTO exchanges(kind, direction, response_body) VALUES('ws','server_to_client',?)",
               (aapi(asr),))
    db.execute("INSERT INTO exchanges(kind, direction, response_body) VALUES('ws','server_to_client',?)",
               (aapi(reply),))
    db.commit()
    db.close()
    return path


def test_report_sections_and_masking(tmp_path):
    """Отчёт: разделы HTTP/AAPI/фразы; секреты и query маскированы; обработчики размечены."""
    from tools.journal_gaps import build_report
    report = build_report(make_journal(tmp_path))
    assert "## HTTP" in report and "## AAPI: колонка → эмулятор" in report
    assert "## AAPI: Яндекс → колонка (по ref_message_id)" in report and "## Фразы и ответы Алисы" in report
    assert "/push_subscribe?device_id=" not in report  # query адресов не выводится
    assert "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6" not in report and "C00T10001B6SAK" not in report
    assert "sign=***" in report and "status=ok" in report  # ключи видны, значение секрета маске
    assert "| MatchedUser | 1 | есть |" in report
    assert "| ContinueRequest | 1 | есть |" in report
    assert "AliceResponse | 1 | MatchedUser ×1" in report  # ref_message_id → входящее событие
    assert "покажи глазки" in report and "device.led.emotion" in report  # фраза закрыта сценарием


async def test_builtin_gap_handlers(tmp_path):
    """push_subscribe — ответ по образцу (sign/status/ts); галерея — пустой protobuf;
    set_device_capability — принято."""
    runtime = Runtime(tmp_path)
    try:
        dispatcher = Dispatcher(runtime)
        request = type("R", (), {"method": "POST", "host": "quasar.yandex.net",
                                 "remote": "127.0.0.1", "headers": {}, "query_string": "",
                                 "rel_url": "/push_subscribe?x=1"})()
        status, body, headers = await dispatcher.builtin_answer(request, "quasar.yandex.net", "/push_subscribe")
        payload = json.loads(body)
        assert status == 200 and payload["status"] == "ok" and len(payload["sign"]) == 32
        assert payload["ts"] == str(int(time.time()))  # свежая метка, как у Яндекса
        gallery = find_builtin("rpc.alice.yandex.net", "POST", "/gproxy/centaur_get_gallery")
        assert gallery["status"] == 200 and gallery["body"] == b""
        assert gallery["headers"]["Content-Type"] == "application/x-protobuf"
        capability = find_builtin("quasar.yandex.net", "POST", "/set_device_capability")
        assert json.loads(capability["body"]) == {"status": "ok"}
    finally:
        runtime.close()


def directives_sent(sent):
    """Имена директив в отправленных AliceResponse."""
    names = []
    for raw in sent:
        if not raw.startswith(b"AAPI"):
            continue
        message = SERVER.FromString(raw[4:])
        if message.WhichOneof("Message") == "Event" and message.Event.WhichOneof("Event") == "AliceResponse":
            names += [d.Name for d in message.Event.AliceResponse.Response.Directives]
    return names


async def test_player_button_callbacks(tmp_path):
    """Кнопки экрана плеера: player_next → следующий трек очереди, player_liked — тихо,
    player_pause → audio_play с set_pause."""
    runtime = Runtime(tmp_path)
    (tmp_path / "media" / "music").mkdir(parents=True)
    (tmp_path / "media" / "music" / "a.mp3").write_bytes(b"a" * 16)
    (tmp_path / "media" / "music" / "b.mp3").write_bytes(b"b" * 16)
    sent = []

    async def send(data):
        sent.append(data)

    try:
        from server_emulator.sdk import ScenarioContext
        ctx = ctx_for(runtime, purpose="player_next")
        ScenarioContext(runtime, ctx).media.play_library(MediaLibrary(tmp_path), "music")  # очередь: a, b
        report = await dispatch_handle(runtime, "@@mm_typed_callback", ctx, "dg", send)
        assert report["handled"] and "audio_play" in directives_sent(sent)
        assert runtime.media_state["SN-A"]["index"] == 1  # следующий трек очереди
        sent.clear()
        ctx = ctx_for(runtime, purpose="player_liked")
        report = await dispatch_handle(runtime, "@@mm_typed_callback", ctx, "dg", send)
        assert report["handled"] and "audio_play" not in directives_sent(sent)  # тихое подтверждение
        sent.clear()
        ctx = ctx_for(runtime, purpose="player_pause")
        report = await dispatch_handle(runtime, "@@mm_typed_callback", ctx, "dg", send)
        assert report["handled"] and "audio_play" in directives_sent(sent)
    finally:
        await runtime.close()


async def test_push_subscribe_through_http(tmp_path):
    """push_subscribe в эмуляции: 200 по образцу, не промах, «Запись» не пишет."""
    from aiohttp import ClientSession
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-S", "user_id": "u",
                               "device_ids": ["s-id"]})
    runtime.identities.mutate({"operation": "record", "sn": "SN-S", "mode": "on"})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/push_subscribe?device_id=s-id&service=quasar-realtime",
                                   headers={"Host": "quasar.yandex.net"}) as response:
                payload = await response.json()
                assert response.status == 200 and payload["status"] == "ok" and "sign" in payload
        assert runtime.endpoints.misses() == {}
        await runtime.upstream.journal.flush()
        assert runtime.upstream.journal.list()["total"] == 0
    finally:
        await runner.cleanup()
        await runtime.close()
