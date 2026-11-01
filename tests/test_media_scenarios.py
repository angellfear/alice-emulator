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
        # вся библиотека вперемешку: любой из двух файлов первым
        assert play["metadata"]["title"] in {"song one", "smooth"}
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
        # очередь запомнена: вся библиотека, порядок вперемешку
        state = runtime.media_state["SN-T"]
        assert sorted(track["name"] for track in state["queue"]) == ["smooth", "song one"]
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
    await reply_for(runtime, "включи музыку джаз", sn="SN-T")
    # что играет / пауза на треке из папки Джаз
    report = await reply_for(runtime, "что играет", sn="SN-T")
    assert "smooth" in " ".join(cards_of(report))
    report = await reply_for(runtime, "пауза", sn="SN-T")
    play = next(payload for name, payload in directives_of(report) if name == "audio_play")
    assert play["stream"]["url"].endswith("smooth.mp3") and play["set_pause"] is True
    # вся библиотека вперемешку: следующий/предыдущий ходят по очереди из двух файлов
    await reply_for(runtime, "включи музыку", sn="SN-T")
    report = await reply_for(runtime, "следующий трек", sn="SN-T")
    play = next(payload for name, payload in directives_of(report) if name == "audio_play")
    assert play["metadata"]["title"] in {"song one", "smooth"}
    report = await reply_for(runtime, "предыдущий трек", sn="SN-T")
    play = next(payload for name, payload in directives_of(report) if name == "audio_play")
    assert play["metadata"]["title"] in {"song one", "smooth"}
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
    titles = {track["name"] for track in runtime.media_state["SN-T"]["queue"]}
    assert play["metadata"]["title"] in titles and runtime.media_state["SN-T"]["index"] == 1
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


def _id3_file(path, title, artist, album="", genre=""):
    """mp3-заглушка с ID3v2.4-тегами (кодировка utf-8)."""
    def frame(fid, text):
        payload = b"\x03" + text.encode("utf-8")
        return fid + len(payload).to_bytes(4, "big") + b"\x00\x00" + payload
    frames = frame(b"TIT2", title) + frame(b"TPE1", artist)
    if album:
        frames += frame(b"TALB", album)
    if genre:
        frames += frame(b"TCON", genre)
    path.write_bytes(b"ID3\x04\x00\x00" + bytes(((len(frames) >> 21) & 0x7F, (len(frames) >> 14) & 0x7F,
                                                 (len(frames) >> 7) & 0x7F, len(frames) & 0x7F)) + frames)


def _payload_of(report):
    play = next((payload for name, payload in directives_of(report) if name == "audio_play"), None)
    assert play is not None, report.get("speech")
    return play


async def test_search_by_tags_and_stems(tmp_path):
    """Поиск по тегам и путям с склонением по основе; название трека — из тега."""
    runtime = Runtime(tmp_path)
    music = tmp_path / "media" / "music" / "Тутси"
    music.mkdir(parents=True)
    _id3_file(music / "hit.mp3", "Самый, самый", "Тутси", album="Лучшее", genre="Поп")
    (tmp_path / "media" / "music" / "Майкл Джексон" / "Beat It.mp3").parent.mkdir(parents=True)
    (tmp_path / "media" / "music" / "Майкл Джексон" / "Beat It.mp3").write_bytes(b"ID3" + b"\x00" * 10)
    try:
        report = await reply_for(runtime, "включи музыку тутси", sn="SN-T")
        play = _payload_of(report)
        assert play["metadata"]["title"] == "Самый, самый" and play["metadata"]["subtitle"] == "Тутси"
        report = await reply_for(runtime, "включи майкла джексона", sn="SN-T")
        play = _payload_of(report)
        assert play["stream"]["url"].endswith("Beat%20It.mp3")
        report = await reply_for(runtime, "включи музыку металлика", sn="SN-T")
        assert "В библиотеке нет музыки с названием «металлика»" in " ".join(cards_of(report))
    finally:
        await runtime.close()


async def test_play_modes_repeat_and_favorites(tmp_path):
    """«на повторе» — очередь из одного трека; «нравится» — избранное; «мою музыку» его играет;
    дизлайк исключает трек из выдачи."""
    runtime = Runtime(tmp_path)
    make_library(tmp_path)
    (tmp_path / "media" / "music" / "Белые розы.mp3").write_bytes(SONG)
    try:
        report = await reply_for(runtime, "включи песню белые розы на повторе", sn="SN-T")
        play = _payload_of(report)
        assert play["metadata"]["title"] == "Белые розы"
        state = runtime.media_state["SN-T"]
        assert len(state["queue"]) == 1  # один трек на повторе
        # лайк текущего трека и включение избранного
        await reply_for(runtime, "лайк", sn="SN-T")
        report = await reply_for(runtime, "включи мою музыку", sn="SN-T")
        assert "В избранном" not in " ".join(cards_of(report))
        state = runtime.media_state["SN-T"]
        assert [track["name"] for track in state["queue"]] == ["Белые розы"]
        # дизлайк: трек пропускается и больше не предлагается
        report = await reply_for(runtime, "не нравится", sn="SN-T")
        assert report["handled"]
        import json as _json
        marks = _json.loads((tmp_path / "media" / "likes.json").read_text(encoding="utf-8"))
        assert marks["SN-T"]["music"]["dislike"] == ["Белые розы.mp3"]
        assert marks["SN-T"]["music"]["like"] == []  # «не нравится» сняло лайк
        report = await reply_for(runtime, "включи мою музыку", sn="SN-T")
        assert "В избранном" in " ".join(cards_of(report))
    finally:
        await runtime.close()


async def test_player_speed_restart_another_and_seek(tmp_path):
    """Скорость (playback_speed из образца), «начни сначала», «другая версия»;
    перемотка честно недоступна — в образце директивы нет поля позиции."""
    runtime = Runtime(tmp_path)
    make_library(tmp_path)
    (tmp_path / "media" / "music" / "smooth remix.mp3").write_bytes(b"ID3remix" * 8)
    try:
        await reply_for(runtime, "включи музыку джаз", sn="SN-T")
        report = await reply_for(runtime, "замедли", sn="SN-T")
        assert _payload_of(report)["playback_speed"] == 0.75
        report = await reply_for(runtime, "поставь скорость 2", sn="SN-T")
        assert _payload_of(report)["playback_speed"] == 2.0
        report = await reply_for(runtime, "какая скорость воспроизведения", sn="SN-T")
        assert "2.0" in " ".join(cards_of(report))
        report = await reply_for(runtime, "начни сначала", sn="SN-T")
        assert _payload_of(report)["stream"]["url"].endswith("smooth.mp3")
        report = await reply_for(runtime, "включи другую версию", sn="SN-T")
        assert _payload_of(report)["stream"]["url"].endswith("smooth%20remix.mp3")
        report = await reply_for(runtime, "перемотай на 10 минут вперед", sn="SN-T")
        assert "Перемотку" in " ".join(cards_of(report))
    finally:
        await runtime.close()


async def test_sounds_scenario(tmp_path):
    """Звуки природы: файл из state/media/sounds играет; пустая папка — честный ответ."""
    runtime = Runtime(tmp_path)
    sounds = tmp_path / "media" / "sounds"
    sounds.mkdir(parents=True)
    (sounds / "Шум дождя.mp3").write_bytes(SONG)
    try:
        report = await reply_for(runtime, "включи шум дождя", sn="SN-T")
        play = _payload_of(report)
        assert play["stream"]["url"].endswith("sounds/%D0%A8%D1%83%D0%BC%20%D0%B4%D0%BE%D0%B6%D0%B4%D1%8F.mp3")
    finally:
        await runtime.close()
    empty = Runtime(tmp_path / "empty")
    try:
        report = await reply_for(empty, "поставь пение птиц", sn="SN-E")
        assert "state/media/sounds" in " ".join(cards_of(report))
    finally:
        await empty.close()


async def test_least_played_and_dislike_filter(tmp_path):
    """«что-нибудь новенькое» — давно не игранное; дизлайк не предлагается."""
    from server_emulator import media as media_mod

    runtime = Runtime(tmp_path)
    library = MediaLibrary(tmp_path)
    (tmp_path / "media" / "music").mkdir(parents=True)
    (tmp_path / "media" / "music" / "a.mp3").write_bytes(SONG)
    (tmp_path / "media" / "music" / "b.mp3").write_bytes(SONG)
    media_mod.note_played(runtime, "u1", "music", "a.mp3")
    assert media_mod.least_played(library, runtime, "u1", "music")[0] == "b.mp3"
    # дизлайк исключается из выдачи play_library
    media_mod.mark_track(library, "u1", "music", "a.mp3", "dislike")
    found = library.search("music", "")
    assert [posix for posix, _size in found if posix != "a.mp3"] == ["b.mp3"]
    reply = runtime.media_state and None
    player = Player(runtime, "SN-T", base="http://127.0.0.1/media")
    assert player.play_library(library, "music", query="a", user="u1") is None
    await runtime.close()


async def test_repeat_current_track(tmp_path):
    """«зациклить трек» — очередь из одного текущего трека."""
    runtime = Runtime(tmp_path)
    make_library(tmp_path)
    try:
        await reply_for(runtime, "включи музыку джаз", sn="SN-T")
        report = await reply_for(runtime, "зациклить трек", sn="SN-T")
        assert _payload_of(report)["metadata"]["title"] == "smooth"
        assert [track["name"] for track in runtime.media_state["SN-T"]["queue"]] == ["smooth"]
    finally:
        await runtime.close()
