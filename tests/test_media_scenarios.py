"""TASK-071: медиа-сценарии — «включи музыку/сказку», радио, управление плеером,
поставщик колбэков плеера, раздача /media с Range, API библиотеки."""
import asyncio
import datetime

from aiohttp import ClientSession, web

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from server_emulator.media import MediaLibrary, Player
from tests.conftest import listen

COVER = bytes(range(256)) * 8
SONG = b"ID3" + b"music-bytes" * 64


def make_library(tmp_path):
    music = tmp_path / "media" / "music"
    (music / "Джаз").mkdir(parents=True)
    (music / "song one.mp3").write_bytes(SONG)
    (music / "Джаз" / "smooth.mp3").write_bytes(b"ID3jazz" * 32)
    (music / "song one.jpg").write_bytes(COVER)
    (tmp_path / "media" / "tales").mkdir()
    (tmp_path / "media" / "tales" / "kolobok.mp3").write_bytes(b"ID3tale" * 32)


async def ctx_for(runtime, text, sn="SN-T", media_base="http://127.0.0.1/media"):
    ctx = {'input': {'message_id': 'm1', 'request_id': 'rq', 'dialog_id': 'dg', 'sn': sn, 'text': text},
           'clock': {'now': datetime.datetime.now()}, 'sequence': next(runtime.sequence)}
    ctx.update(runtime._scenario_context(sn=sn))
    ctx['media_base'] = media_base
    return ctx


async def reply_for(runtime, phrase, sn="SN-T"):
    ctx = await ctx_for(runtime, phrase, sn)
    report = await runtime.dialog.handle_text(phrase, ctx, "dg", lambda data: None, deliver=False)
    return report


def directives_of(report):
    """(имя, payload-словарь) директив первого сообщения отчёта."""
    from google.protobuf import json_format
    found = []
    for message in report.get("messages") or []:
        if message.WhichOneof("Message") != "Event":
            continue
        for directive in message.Event.AliceResponse.Response.Directives:
            found.append((directive.Name, json_format.MessageToDict(directive.Payload)))
    return found


def _spoken(message):
    """Реплика ответа: текст карточки, если она есть, иначе озвучка (OutputSpeech)."""
    alice = message.Event.AliceResponse
    cards = [card.Text for card in alice.Response.Cards]
    spoken = alice.VoiceResponse.OutputSpeech.Text if alice.HasField("VoiceResponse") else ""
    return [text for text in (cards or [spoken]) if text]


def cards_of(report):
    texts = []
    for message in report.get("messages") or []:
        if message.WhichOneof("Message") == "Event" and message.Event.WhichOneof("Event") == "AliceResponse":
            texts += _spoken(message)
    return texts


def directives_of_sent(sent):
    """(имя, payload) директив из отправленных байт AAPI."""
    from google.protobuf import json_format
    from tests.conftest import types
    _client, server = types()
    found = []
    for raw in sent:
        if not raw.startswith(b"AAPI"):
            continue
        message = server.FromString(raw[4:])
        if message.WhichOneof("Message") == "Event" and message.Event.WhichOneof("Event") == "AliceResponse":
            for directive in message.Event.AliceResponse.Response.Directives:
                found.append((directive.Name, json_format.MessageToDict(directive.Payload)))
    return found


def test_library_scan_search_and_art(tmp_path):
    make_library(tmp_path)
    library = MediaLibrary(tmp_path)
    assert [name for name, _size in library.list("music")] == ["song one.mp3", "Джаз/smooth.mp3"]
    assert library.folders("music") == ["Джаз"]
    assert [(name) for name, _s in library.search("music", "smooth")] == ["Джаз/smooth.mp3"]
    assert [name for name, _s in library.search("music", "SMOOTH")] == ["Джаз/smooth.mp3"]  # регистр
    art = library.art_for("music", "song one.mp3")
    assert art is not None and art.name == "song one.jpg"
    assert library.art_for("music", "Джаз/smooth.mp3") is None
    assert library.resolve("music", "../settings.json") is None  # выход за папку закрыт
    assert library.resolve("music", "song one.mp3") is not None


async def test_play_music_scenario_and_range_serving(tmp_path):
    """«включи музыку» → директива audio_play с адресом /media/…; файл с Range → 206."""
    runtime = Runtime(tmp_path)
    make_library(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        report = await reply_for(runtime, "включи музыку")
        assert report["handled"] and report["scenario"] == "media.music"
        directives = directives_of(report)
        play = next(payload for name, payload in directives if name == "audio_play")
        assert play["stream"]["type"] == "Track" and play["stream"]["format"] == "MP3"
        assert play["metadata"]["title"] == "song one"
        assert play["metadata"]["art_image_url"].endswith("/media/music/song%20one.jpg")
        # файл раздаётся; Range — частичный ответ
        async with ClientSession() as client:
            async with client.get(url + "/media/music/song%20one.mp3") as response:
                assert response.status == 200
                assert response.headers["Content-Type"] == "audio/mpeg"
                assert await response.read() == SONG
            async with client.get(url + "/media/music/song%20one.mp3",
                                  headers={"Range": "bytes=0-9"}) as response:
                assert response.status == 206
                assert await response.read() == SONG[:10]
            async with client.get(url + "/media/../settings.json") as response:
                assert response.status == 404  # выход за папку закрыт
        # очередь запомнена: «следующий трек» — второй файл
        state = runtime.media_state["SN-T"]
        assert [track["name"] for track in state["queue"]] == ["song one", "smooth"]
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_play_with_query_folder_and_empty_library(tmp_path):
    runtime = Runtime(tmp_path)
    make_library(tmp_path)
    report = await reply_for(runtime, "включи музыку джаз")
    play = next(payload for name, payload in directives_of(report) if name == "audio_play")
    assert play["metadata"]["title"] == "smooth" and play["metadata"]["subtitle"] == "Джаз"
    # сказки есть; запрос без совпадений — честный ответ
    report = await reply_for(runtime, "включи сказку про дракона")
    assert "нет" in " ".join(cards_of(report))
    # пустая библиотека сказок — честный ответ
    empty = Runtime(tmp_path / "empty")
    try:
        report = await reply_for(empty, "включи сказку", sn="SN-E")
        assert "state/media/tales" in " ".join(cards_of(report))
    finally:
        await empty.close()


async def test_player_control_and_radio(tmp_path):
    runtime = Runtime(tmp_path)
    make_library(tmp_path)
    runtime.settings["media"] = {"radio": [{"name": "Джаз ФМ", "url": "http://stream.example/jazz"}]}
    await reply_for(runtime, "включи музыку", sn="SN-T")
    # следующий/предыдущий/что играет
    report = await reply_for(runtime, "следующий трек", sn="SN-T")
    play = next(payload for name, payload in directives_of(report) if name == "audio_play")
    assert play["metadata"]["title"] == "smooth"
    report = await reply_for(runtime, "предыдущий трек", sn="SN-T")
    play = next(payload for name, payload in directives_of(report) if name == "audio_play")
    assert play["metadata"]["title"] == "song one"
    report = await reply_for(runtime, "что играет", sn="SN-T")
    assert "song one" in " ".join(cards_of(report))
    report = await reply_for(runtime, "пауза", sn="SN-T")
    play = next(payload for name, payload in directives_of(report) if name == "audio_play")
    assert play["stream"]["url"].endswith("song%20one.mp3") and play["set_pause"] is True
    # радио по имени; неизвестная станция — честный ответ
    report = await reply_for(runtime, "включи радио джаз", sn="SN-T")
    play = next(payload for name, payload in directives_of(report) if name == "audio_play")
    assert play["stream"]["url"] == "http://stream.example/jazz" and play["stream"]["type"] == "Radio"
    report = await reply_for(runtime, "включи радио рок", sn="SN-T")
    assert "нет в списке" in " ".join(cards_of(report))


async def test_playback_provider_advances_queue(tmp_path):
    """Колбэки плеера (трек закончился / колонка просит следующий) → следующий трек."""
    runtime = Runtime(tmp_path)
    make_library(tmp_path)
    await reply_for(runtime, "включи музыку", sn="SN-T")
    ctx = await ctx_for(runtime, "", sn="SN-T")
    sent = []

    async def send(data):
        sent.append(data)

    report = await runtime.dialog.handle_event("media.playback_ended", ctx, "dg", send)
    assert report["handled"]
    play = next(payload for name, payload in directives_of_sent(sent) if name == "audio_play")
    assert play["metadata"]["title"] == "smooth"  # очередь перешла на второй
    sent.clear()
    report = await runtime.dialog.handle_event("media.next_requested", ctx, "dg", send)
    assert report["handled"] and not directives_of_sent(sent)  # очередь кончилась — тихий ответ


async def test_media_api_upload_delete_and_radio(tmp_path):
    """API библиотеки: загрузка файла (X-Filename), удаление, список радио в настройках."""
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    headers = {"X-Emulator": "1"}
    try:
        async with ClientSession() as client:
            async with client.post(url + "/emulator/api/media?kind=music&folder=Рок",
                                   headers={**headers, "X-Filename": "hit song.mp3"}, data=b"MUSIC" * 32) as response:
                assert response.status == 200
                assert (await response.json())["saved"] == "Рок/hit song.mp3"
            async with client.post(url + "/emulator/api/media?kind=music",
                                   headers={**headers, "X-Filename": "../evil.mp3"}, data=b"x") as response:
                assert response.status == 400  # имя чистится: ../ не проходит
            async with client.get(url + "/emulator/api/media", headers=headers) as response:
                body = await response.json()
                assert body["music"]["count"] == 1 and body["music"]["folders"] == ["Рок"]
                assert body["tales"]["count"] == 0 and body["radio"] == []
            async with client.put(url + "/emulator/api/media", headers=headers,
                                  json={"radio": [{"name": "Джаз", "url": "http://s.example/j"}]}) as response:
                assert (await response.json())["saved"] is True
            async with client.delete(url + "/emulator/api/media?kind=music&name=Рок/hit%20song.mp3",
                                     headers=headers) as response:
                assert (await response.json())["deleted"] == "Рок/hit song.mp3"
            assert not list((runtime.directory / "media" / "music").rglob("*.mp3"))  # файл и папка удалены
    finally:
        await runner.cleanup()
        await runtime.close()
