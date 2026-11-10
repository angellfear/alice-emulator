"""Ядро поставщика музыки (music/*.py) и стек директорий виджета.

Поставщик — тестовое расширение (music/fake.py через реальный rebuild_extension_registries,
как в тестах actions/datasets); механика реестра — на внешних расширениях, встроенные
плагины выключаются хелпером _disable_builtin_providers. Настоящий Spotify/Яндекс в
тестах не дёргается. Форма стека — по расшифрованным образцам настоящего сервера
(захват 2026-11-02-music, server_emulator/state/captures/): полный стек «включи музыку» —
tts_play_placeholder + draw_animation(player_play) + show_audio_player_screen +
apply_player_playback_queue (TApplyMusicPlatformQueueDirective) +
set_audio_player_sequence_state + audio_play; «дальше» (колбэк @@mm_stack_engine_get_next →
provide music.stack_next) — тройка очередь+состояние+audio_play без экрана.
"""
import asyncio
import datetime
import json
import pytest
from pathlib import Path

from google.protobuf import json_format

from server_emulator import music, providers
from server_emulator.aapi_handlers import route as callback_route
from server_emulator.extensions import EMULATOR_VERSION
from server_emulator.runtime import Runtime
from aiohttp import ClientSession

from tests.conftest import listen
from server_emulator.sdk import SDK_VERSION, ScenarioContext
from server_emulator.schema import load_type, schemas_dir, station_file
from server_emulator.server import create_app
from tests.conftest import types

CLIENT, SERVER = types()

QUEUE_PROTO = "NAlice.TMusicPlatformQueueCapability.TApplyMusicPlatformQueueDirective"
SEQSTATE_PROTO = "NAlice.TAudioPlayerCapability.TSetSequenceStateDirective"
SCREEN_PROTO = "NAlice.TAudioPlayerScreenCapability.TShowAudioPlayerScreenDirective"
ANIM_PROTO = "NAlice.TAnimationCapability.TDrawAnimationDirective"

TRACKS = [
    {"id": "t1", "title": "Хороший день", "artist": "Тестовый Артист", "album": "Тестовый альбом",
     "duration_ms": 180000, "cover_url": "https://example.com/cover1.jpg",
     "stream": {"url": "https://example.com/t1.mp3", "format": "mp3"}},
    {"id": "t2", "title": "Вторая песня", "artist": "Другой Артист", "duration_ms": 200000,
     "stream": {"url": "https://example.com/t2.mp3", "format": "mp3"}},
] + [{"id": f"t{i}", "title": f"Трек {i}", "artist": "Тестовый Артист", "duration_ms": 1000 * i,
      "stream": {"url": f"https://example.com/t{i}.mp3", "format": "mp3"}} for i in range(3, 8)]

NEXT_TRACK = {"id": "t9", "title": "Следующий трек", "artist": "Тестовый Артист",
              "duration_ms": 190000, "stream": {"url": "https://example.com/t9.mp3", "format": "mp3"}}


@pytest.fixture(autouse=True)
def _reset_captured_oauth():
    """Кеш захваченного OAuth — глобальный по модулю: тесты стартуют с чистого."""
    music._CAPTURED.pop("oauth", None)
    yield
    music._CAPTURED.pop("oauth", None)


def _disable_builtin_providers(runtime):
    """Мир без встроенных поставщиков (механика реестра здесь — на внешних расширениях;
    сами встроенные — tests/test_builtin_plugins.py, перенесённые поставщики —
    tests/test_music_spotify.py / tests/test_music_yandex.py)."""
    state = runtime.settings.setdefault("extensions", {})
    for ext_id in list(state["installed"]):
        if ext_id.startswith("builtin."):
            state["enabled"][ext_id] = False
    runtime.extensions.load_enabled()


def _provider_module(marker, provider_id="fake", service=None):
    """Модуль music/*.py: каждый вызов пишет в marker (JSON-список), треки — TRACKS."""
    path = str(marker).replace("\\", "/")
    tracks = json.dumps(TRACKS, ensure_ascii=False)
    service_part = f', "service": "{service}"' if service else ""
    return f'''import json
from pathlib import Path
MARKER = Path("{path}")
TRACKS = {tracks}
NEXT = {json.dumps(NEXT_TRACK, ensure_ascii=False)}

def _note(event):
    calls = json.loads(MARKER.read_text(encoding="utf-8")) if MARKER.exists() else []
    calls.append(event)
    MARKER.write_text(json.dumps(calls, ensure_ascii=False), encoding="utf-8")

async def search(ctx, query, kind):
    _note(["search", query, kind])
    return {{"tracks": TRACKS, "start": 0, "context": {{"type": "search", "id": "q"}}}}

async def neighbor(ctx, track_id, context, step):
    _note(["neighbor", track_id, context, step])
    return NEXT if step > 0 else None

async def rate(ctx, track_id, like):
    _note(["rate", track_id, like])
    return True

PROVIDER = {{"id": "{provider_id}", "title": "Поставщик для теста"{service_part}}}
'''


MUSIC_PHRASES = {"entries": [
    {"id": "play", "sentences": ["включи музыку [{query}]", "(включи|поставь|запусти) музыку [{query}]",
                                 "давай послушаем музыку [{query}]", "(включи|поставь) (песню|трек) {query}"],
     "handler": "core:media.music"},
    {"id": "podcast", "sentences": ["(включи|поставь) (подкаст|подкасты) [{query}]"],
     "handler": "core:media.music"},
    {"id": "audiobook", "sentences": ["(включи|поставь) аудиокнигу {query}"], "handler": "core:media.music"},
    {"id": "tales", "sentences": ["(включи|поставь) (сказку|сказки) {query}"], "handler": "core:media.music"},
    {"id": "next", "sentences": ["дальше", "(следующий|следующая|следующую) [(трек|песня|песню)]"],
     "handler": "core:media.music"},
    {"id": "prev", "sentences": ["(предыдущий|предыдущая|предыдущую) [(трек|песня|песню)]"],
     "handler": "core:media.music"},
    {"id": "continue", "sentences": ["(продолжи|продолжить|продолжай) [(музыку|играть)]"],
     "handler": "core:media.music"},
    {"id": "search", "sentences": ["(включи|запусти|поставь|послушай) {query}"],
     "handler": "core:media.music", "search": True}]}


def make_music_extension(runtime, ext_id="fake-music", provider_id="fake", files=None,
                         enabled=True, phrases=None, module="music/fake.py"):
    """Расширение-папка с music/*.py (dev-режим): manifest + записи installed/enabled.
    phrases — файл справочника фраз + объявление contributes.providers (фразы музыки
    переехали из сценария в справочник плагина); None — папка вида без объявления."""
    root = runtime.directory / "ext" / ext_id
    (root / "music").mkdir(parents=True, exist_ok=True)
    manifest = {"id": ext_id, "name": ext_id, "version": "1.0.0", "description": "тест",
                "author": "T", "license": "MIT",
                "requires": {"emulator": f">={EMULATOR_VERSION}", "sdk": f">={SDK_VERSION}"},
                "permissions": {}}
    if phrases is not None:
        files = dict(files or {})
        files["phrases/music.json"] = json.dumps(phrases, ensure_ascii=False)
        manifest["contributes"] = {"providers": [{"kind": "music", "title": "Музыка",
                                                  "module": module,
                                                  "phrases": "phrases/music.json"}]}
    (root / "extension.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    for name, content in (files or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    state = runtime.settings.setdefault("extensions", {})
    state.setdefault("installed", {})[ext_id] = {"version": "1.0.0", "dev": str(root)}
    state.setdefault("enabled", {})[ext_id] = enabled
    return root


async def say(runtime, text, route_mode="emulate", station=None, deliver=False):
    """Реплика колонке в формате voice.py (route_mode/player_owner/station — как в запросе)."""
    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dlg", "sn": "SN-M",
                     "text": text},
           "clock": {"now": datetime.datetime.now()}, "sequence": 1,
           "route_mode": route_mode, "player_owner": None}
    if station:
        ctx["station"] = station
    ctx.update(runtime._scenario_context(sn="SN-M"))
    return await runtime.dialog.handle_text(text, ctx, "dlg", None, deliver=deliver)


async def _collect(sent):
    async def send(data):
        sent.append(data)
    return send


def service_ctx(runtime, event_name, route_mode="emulate"):
    """Контекст колбэка @@mm_stack_engine_get_next (как собирает voice.py)."""
    return {"input": {"message_id": "msg-1", "request_id": "req-1", "dialog_id": "dlg-1",
                      "sn": "SN-M", "text": "", "event_name": event_name,
                      "callback": "", "purpose": "", "frame": ""},
            "clock": {"now": datetime.datetime.now()}, "sequence": 1,
            "route_mode": route_mode, "player_owner": None,
            **runtime._scenario_context(sn="SN-M")}


def _reply_names(message):
    return [d.Name for d in message.Event.AliceResponse.Response.Directives]


def _reply_directive(message, name):
    return next(d for d in message.Event.AliceResponse.Response.Directives if d.Name == name)


def _reply_speech(message):
    alice = message.Event.AliceResponse
    return alice.VoiceResponse.OutputSpeech.Text if alice.HasField("VoiceResponse") else ""


def _station_type(full_name):
    return load_type(schemas_dir(), station_file(), full_name)


def _decoded(message, name, proto):
    raw = _reply_directive(message, name).PayloadRaw
    parsed = _station_type(proto)()
    parsed.ParseFromString(bytes(raw))
    return json_format.MessageToDict(parsed, preserving_proto_field_name=True)


def _payload_of(message, name):
    return json_format.MessageToDict(_reply_directive(message, name).Payload,
                                     preserving_proto_field_name=True)


# --- реестр поставщиков ----------------------------------------------------------------


async def test_provider_registry_from_extension(tmp_path):
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    try:
        assert set(providers.PROVIDERS["music"]) == {"fake"}
        provider = music.selected(runtime.settings)
        assert provider["id"] == "fake" and provider["ext_id"] == "fake-music"
        assert provider["title"] == "Поставщик для теста" and callable(provider["search"])
        assert provider["rate"] is not None
    finally:
        await runtime.close()


async def test_broken_provider_module_disables_extension(tmp_path):
    runtime = Runtime(tmp_path)
    make_music_extension(runtime, files={"music/bad.py": "raise ValueError('битый модуль')\n"},
                          phrases=MUSIC_PHRASES, module="music/bad.py")
    _disable_builtin_providers(runtime)
    try:
        assert "fake-music" not in runtime.extensions.loaded
        assert any("битый модуль" in w["error"] for w in runtime.extensions.warnings)
        assert providers.PROVIDERS.get("music", {}) == {}
    finally:
        await runtime.close()


async def test_provider_name_conflict_disables_second(tmp_path):
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, ext_id="first", files={"music/a.py": _provider_module(marker)}, phrases=MUSIC_PHRASES, module="music/a.py")
    make_music_extension(runtime, ext_id="second", files={"music/b.py": _provider_module(marker)}, phrases=MUSIC_PHRASES, module="music/b.py")
    _disable_builtin_providers(runtime)
    try:
        # оба объявляют id «fake» — второй по алфавиту не включается, первый работает
        assert "second" not in runtime.extensions.loaded
        assert any("уже занято" in w["error"] for w in runtime.extensions.warnings)
        assert music.selected(runtime.settings)["ext_id"] == "first"
    finally:
        await runtime.close()


async def test_selected_setting_and_empty_registry(tmp_path):
    runtime = Runtime(tmp_path)
    _disable_builtin_providers(runtime)  # пусто и без встроенных
    try:
        assert music.selected(runtime.settings) is None  # поставщиков нет
        marker = tmp_path / "marker.json"
        make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)}, phrases=MUSIC_PHRASES)
        runtime.extensions.load_enabled()
        runtime.settings["providers"]["music"] = "нет-такого"
        assert music.selected(runtime.settings) is None  # выбранный отключён — источника нет
        runtime.settings["providers"]["music"] = "fake"
        assert music.selected(runtime.settings)["id"] == "fake"
    finally:
        await runtime.close()


# --- полный стек виджета на «включи музыку» ---------------------------------------------


async def test_full_widget_stack_on_play_phrase(tmp_path):
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    try:
        report = await say(runtime, "включи музыку хороший день")
        assert report["handled"], report
        message = report["messages"][0]
        assert _reply_names(message) == [
            "tts_play_placeholder", "draw_animation_directive", "show_audio_player_screen_directive",
            "apply_player_playback_queue_directive", "set_audio_player_sequence_state_directive",
            "audio_play"]
        speech = _reply_speech(message).lower()
        assert speech.startswith("включаю") and "хороший день" in speech
        # анимация — player_play (образец «включи музыку»), не player_pause
        anim = _decoded(message, "draw_animation_directive", ANIM_PROTO)
        assert anim["Animations"][0]["S3Directory"]["Path"] == "animations/player_play"
        # экран плеера — с идентификатором играющего трека
        assert _decoded(message, "show_audio_player_screen_directive", SCREEN_PROTO) == \
            {"TrackId": "t1"}
        # очередь: сущности треков поиска (до 5), текущий — первый, мета с title/artist/cover
        queue = _decoded(message, "apply_player_playback_queue_directive", QUEUE_PROTO)
        entities = queue["MusicPlatformQueue"]["Entities"]
        assert [e["PlaybackEntity"]["TrackEntity"]["Track"]["Id"] for e in entities] == \
            ["t1", "t2", "t3", "t4", "t5"]
        assert queue["MusicPlatformQueue"]["CurrentEntityIndex"] == 0
        meta_records = queue["MusicPlatformQueue"]["MusicPlatformMetaStorage"]["ProviderMeta"][
            "MUSIC_PROVIDER_UNSPECIFIED"]["TrackMetaRecords"]
        assert meta_records["t1"]["TrackMeta"]["title"] == "Хороший день"
        assert meta_records["t1"]["TrackMeta"]["artist_ids"] == ["Тестовый Артист"]
        assert meta_records["t1"]["TrackMeta"]["album_id"] == "Тестовый альбом"
        assert meta_records["t1"]["TrackMeta"]["duration_ms"] == 180000
        assert meta_records["t1"]["TrackMeta"]["image_url"] == "https://example.com/cover1.jpg"
        # sequence_state: CurrentItem — трек, мета очереди — в ProviderTrackMeta
        state = _decoded(message, "set_audio_player_sequence_state_directive", SEQSTATE_PROTO)
        current = state["SequenceState"]["CurrentItem"]["Track"]
        assert current == {"Id": "t1", "Uuid": "t1"}
        track_meta = state["SequenceState"]["MetaStorage"]["ProviderTrackMeta"][
            "MUSIC_PROVIDER_UNSPECIFIED"]["TrackMeta"]
        assert track_meta["t1"]["title"] == "Хороший день" and track_meta["t2"]["title"] == "Вторая песня"
        assert state["SequenceState"]["MetaStorage"]["ProviderArtistMeta"][
            "MUSIC_PROVIDER_UNSPECIFIED"]["ArtistMeta"]["Тестовый Артист"]["name"] == "Тестовый Артист"
        # audio_play: stream/metadata/screen_type по образцу
        play = _payload_of(message, "audio_play")
        assert play["stream"] == {"url": "https://example.com/t1.mp3", "type": "Track",
                                  "format": "MP3", "duration_ms": 180000}
        assert play["metadata"] == {"title": "Хороший день", "subtitle": "Тестовый Артист",
                                    "art_image_url": "https://example.com/cover1.jpg",
                                    "hide_progress_bar": False}
        assert play["screen_type"] == "Music" and play["background_mode"] == "Ducking"
        # поиск получил хвост фразы и kind music
        assert json.loads(marker.read_text(encoding="utf-8"))[0] == ["search", "хороший день", "music"]
    finally:
        await runtime.close()


async def test_play_phrase_without_query_asks_provider(tmp_path):
    """«включи музыку» без запроса — поставщик решает (пустой query), играет первый трек."""
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    try:
        report = await say(runtime, "включи музыку")
        assert report["handled"] and report["messages"]
        assert _reply_speech(report["messages"][0]) == "Включаю."
        assert json.loads(marker.read_text(encoding="utf-8"))[0] == ["search", "", "music"]
        queue = _decoded(report["messages"][0], "apply_player_playback_queue_directive", QUEUE_PROTO)
        assert len(queue["MusicPlatformQueue"]["Entities"]) == 5  # топ-5 поиска
    finally:
        await runtime.close()


async def test_play_without_query_question_listens_and_answer_searches(tmp_path):
    """Поставщик на пустой запрос спрашивает «Что включить?» — вопрос уточнения
    (ShouldListen: колонка слушает), ответ пользователя — запрос поиска."""
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    module = _provider_module(marker).replace(
        '    _note(["search", query, kind])\n',
        '    _note(["search", query, kind])\n'
        '    if not query:\n'
        '        return {"tracks": [], "note": "Что включить? Назовите исполнителя или песню."}\n')
    make_music_extension(runtime, files={"music/fake.py": module}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    try:
        report = await say(runtime, "включи музыку")
        assert report["handled"] and report["messages"]
        question = report["messages"][-1].Event.AliceResponse
        assert question.VoiceResponse.ShouldListen is True
        assert question.VoiceResponse.OutputSpeech.Text.startswith("Что включить?")
        report = await say(runtime, "тестовый исполнитель")
        assert report["handled"], report
        # последним поиском мог закончиться и префетч соседа (recoverCommit, фон)
        assert ["search", "тестовый исполнитель", "music"] in json.loads(marker.read_text(encoding="utf-8"))
        assert "audio_play" in _reply_names(report["messages"][0])
    finally:
        await runtime.close()


async def test_question_answer_over_ws_with_empty_dialog_id(tmp_path):
    """Голосовой тракт: колонка шлёт основной диалог с пустым DialogId — ответ на
    «Что включить?» всё равно доходит до продолжения сценария музыки."""
    import asyncio
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    module = _provider_module(marker).replace(
        '    _note(["search", query, kind])\n',
        '    _note(["search", query, kind])\n'
        '    if not query:\n'
        '        return {"tracks": [], "note": "Что включить?"}\n')
    make_music_extension(runtime, files={"music/fake.py": module}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    runtime.identities.mutate({"operation": "user", "id": "owner", "name": "O"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-W", "user_id": "owner",
                               "device_ids": ["dev-w"], "profile": {"aapi_model": True}})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url + "/uni.ws") as ws:
                sync = CLIENT()
                sync.Event.Header.MessageId = "sync"
                sync.Event.SynchronizeStateRequest.Application.DeviceId = "dev-w"
                await ws.send_bytes(b"AAPI" + sync.SerializeToString())
                await asyncio.wait_for(ws.receive(), 3)

                async def phrase(text, message_id):
                    message = CLIENT()
                    message.Event.Header.MessageId = message_id
                    message.Event.TextInput.Header.RequestId = "rq-" + message_id
                    message.Event.TextInput.Request.Event.Text = text  # DialogId пустой
                    await ws.send_bytes(b"AAPI" + message.SerializeToString())
                    for _ in range(5):
                        packet = await asyncio.wait_for(ws.receive(), 5)
                        if packet.data.startswith(b"AAPI"):
                            reply = SERVER.FromString(packet.data[4:])
                            if reply.Event.WhichOneof("Event") == "AliceResponse":
                                return reply

                question = await phrase("включи музыку", "m1")
                assert question.Event.AliceResponse.VoiceResponse.ShouldListen is True
                answer = await phrase("тестовый исполнитель", "m2")
                assert "audio_play" in _reply_names(answer)
        # последним поиском мог закончиться и префетч соседа (recoverCommit, фон)
        assert ["search", "тестовый исполнитель", "music"] in json.loads(marker.read_text(encoding="utf-8"))
    finally:
        await runner.cleanup()
        await runtime.close()


RESUME_MODULE_PATCH = ('    return NEXT if step > 0 else None',
                       '    return TRACKS[0] if step == 0 else NEXT if step > 0 else None')


@pytest.mark.parametrize("state", ["paused", "stopped"])
async def test_play_without_query_resumes_paused_track(tmp_path, state):
    """«включи музыку» без запроса, плеер колонки на паузе с текущим треком поставщика —
    продолжение с места: очередь + состояние + audio_play с stream.offset_ms (образец
    «продолжи»), без вопроса и без поиска."""
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    module = _provider_module(marker).replace(*RESUME_MODULE_PATCH)
    make_music_extension(runtime, files={"music/fake.py": module}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    station = {"player": {"state": state, "playing": False, "title": "Хороший день",
                          "subtitle": "Тестовый Артист", "played_ms": 65662, "duration_ms": 180000}}
    try:
        report = await say(runtime, "включи музыку", station=station)
        assert report["handled"]
        message = report["messages"][0]
        assert _reply_names(message) == ["tts_play_placeholder", "apply_player_playback_queue_directive",
                                         "set_audio_player_sequence_state_directive", "audio_play"]
        play = _payload_of(message, "audio_play")
        assert play["stream"]["offset_ms"] == 65662 and play["stream"]["url"] == "https://example.com/t1.mp3"
        assert not message.Event.AliceResponse.VoiceResponse.ShouldListen
        calls = json.loads(marker.read_text(encoding="utf-8"))
        assert ["neighbor", None, None, 0] in calls and not any(c[0] == "search" for c in calls)
    finally:
        await runtime.close()


@pytest.mark.parametrize("player", [
    {"state": "paused", "title": "Чужой трек", "played_ms": 1000},  # на паузе не наш трек
    {"state": "idle", "title": "", "played_ms": 0},                  # ничего не играло
])
async def test_play_without_query_no_resume_searches(tmp_path, player):
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    module = _provider_module(marker).replace(*RESUME_MODULE_PATCH)
    make_music_extension(runtime, files={"music/fake.py": module}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    try:
        report = await say(runtime, "включи музыку", station={"player": player})
        assert report["handled"]
        assert json.loads(marker.read_text(encoding="utf-8"))[-1] == ["search", "", "music"]
        assert "offset_ms" not in _payload_of(report["messages"][0], "audio_play")["stream"]
    finally:
        await runtime.close()


async def test_search_kind_by_phrase(tmp_path):
    """«включи сказку/аудиокнигу/подкаст» — kind из реплики, хвост — запрос."""
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    try:
        await say(runtime, "включи сказку Колобок")
        calls = json.loads(marker.read_text(encoding="utf-8"))
        assert calls[-1] == ["search", "колобок", "tales"]
        await say(runtime, "включи аудиокнигу война и мир")
        assert json.loads(marker.read_text(encoding="utf-8"))[-1] == ["search", "война и мир", "audiobook"]
    finally:
        await runtime.close()


async def test_queue_cut_to_five(tmp_path):
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    module = _provider_module(marker).replace("TRACKS, \"start\"", "TRACKS + TRACKS, \"start\"")
    make_music_extension(runtime, files={"music/fake.py": module}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    try:
        report = await say(runtime, "включи музыку")
        queue = _decoded(report["messages"][0], "apply_player_playback_queue_directive", QUEUE_PROTO)
        assert len(queue["MusicPlatformQueue"]["Entities"]) == 5  # очередь режется до MAX_QUEUE
    finally:
        await runtime.close()


async def test_nothing_found_honest_answer(tmp_path):
    """Пустой результат поиска — честная реплика, директив нет."""
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    module = _provider_module(marker).replace('{"tracks": TRACKS', '{"tracks": []')
    make_music_extension(runtime, files={"music/fake.py": module}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    try:
        report = await say(runtime, "включи музыку")
        assert report["handled"] and report["messages"]
        assert _reply_names(report["messages"][0]) == ["tts_play_placeholder"]
        assert "Не нашла" in _reply_speech(report["messages"][0])
    finally:
        await runtime.close()


# --- колбэк @@mm_stack_engine_get_next → neighbor поставщика ----------------------------


def _sent_messages(sent):
    return [SERVER.FromString(raw[4:]) for raw in sent]


async def test_get_next_callback_serves_next_track(tmp_path):
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    try:
        assert callback_route("@@mm_stack_engine_get_next", {}) == "music.stack_next"
        sent = []
        report = await runtime.dialog.handle_event(
            "music.stack_next", service_ctx(runtime, "@@mm_stack_engine_get_next"),
            "dlg-1", await _collect(sent))
        assert report["handled"], report
        [message] = _sent_messages(sent)
        names = _reply_names(message)
        assert names == ["tts_play_placeholder", "apply_player_playback_queue_directive",
                         "set_audio_player_sequence_state_directive", "audio_play"]
        assert "show_audio_player_screen_directive" not in names  # экран уже открыт — как в «дальше»
        assert "draw_animation_directive" not in names
        assert _reply_speech(message) == ""
        queue = _decoded(message, "apply_player_playback_queue_directive", QUEUE_PROTO)
        assert [e["PlaybackEntity"]["TrackEntity"]["Track"]["Id"]
                for e in queue["MusicPlatformQueue"]["Entities"]] == ["t9"]
        play = _payload_of(message, "audio_play")
        assert play["stream"]["url"] == "https://example.com/t9.mp3"
        assert json.loads(marker.read_text(encoding="utf-8"))[-1][:2] == ["neighbor", None]
    finally:
        await runtime.close()


async def test_get_next_without_next_track_stays_silent(tmp_path):
    """neighbor вернул None (дальше нечего) — сценарий отказывается: умолчание маршрута —
    тихое подтверждение (как в образцах x4: только tts_play_placeholder)."""
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    module = _provider_module(marker).replace("return NEXT if step > 0 else None", "return None")
    make_music_extension(runtime, files={"music/fake.py": module}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    try:
        report = await runtime.dialog.handle_event(
            "music.stack_next", service_ctx(runtime, "@@mm_stack_engine_get_next"),
            "dlg-1", await _collect([]))
        assert not report["handled"]
        message = await runtime.dialog.quiet_ack(service_ctx(runtime, "@@mm_stack_engine_get_next"))
        assert _reply_names(message) == ["tts_play_placeholder"]
        assert _reply_speech(message) == ""
    finally:
        await runtime.close()


async def test_get_next_without_provider_falls_back_to_ack(tmp_path):
    runtime = Runtime(tmp_path)
    _disable_builtin_providers(runtime)
    try:
        report = await runtime.dialog.handle_event(
            "music.stack_next", service_ctx(runtime, "@@mm_stack_engine_get_next"),
            "dlg-1", await _collect([]))
        assert not report["handled"]  # сценарий отказался — voice.py отвечает quiet_ack
        message = await runtime.dialog.quiet_ack(service_ctx(runtime, "@@mm_stack_engine_get_next"))
        assert _reply_names(message) == ["tts_play_placeholder"]
    finally:
        await runtime.close()


# --- маршруты честности: «Вместе», «Простой», «Модель» -----------------------------------


async def test_hybrid_route_declines_music(tmp_path):
    """«Вместе»: сценарии музыки отказываются — Яндекс отвечает сам (релей голоса, 098)."""
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    try:
        report = await say(runtime, "включи музыку хороший день", route_mode="hybrid")
        assert not report["handled"]  # фраза уходит релею голоса
        report = await runtime.dialog.handle_event(
            "music.stack_next", service_ctx(runtime, "@@mm_stack_engine_get_next", route_mode="hybrid"),
            "dlg-1", await _collect([]))
        assert not report["handled"]
        assert not (tmp_path / "marker.json").exists()  # поставщик не вызывался
    finally:
        await runtime.close()


    runtime = Runtime(tmp_path)
    runtime.settings["intelligence"] = "scenarios"  # «Простой»
    _disable_builtin_providers(runtime)
    try:
        # мира фраз без плагина нет: «нет плагина — нет фраз», реплика неизвестна
        report = await say(runtime, "включи музыку хороший день")
        assert not report["handled"] and report["reason"] == "no_match", report
    finally:
        await runtime.close()


async def test_no_provider_model_mode_declines(tmp_path):
    """«Модель»: сценарий отказывается (None) — фразу забирает модель."""
    runtime = Runtime(tmp_path)
    runtime.settings["intelligence"] = "model"
    _disable_builtin_providers(runtime)
    try:
        report = await say(runtime, "включи музыку хороший день")
        assert not report["handled"]  # реплика уходит модели
    finally:
        await runtime.close()


async def test_provider_answers_in_every_intelligence_mode(tmp_path):
    """Поставщик есть — играет он во всех режимах «Интеллекта» (модель не участвует)."""
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    try:
        for mode in ("scenarios", "model", "agent"):
            runtime.settings["intelligence"] = mode
            report = await say(runtime, "включи музыку")
            assert report["handled"], mode
            assert "audio_play" in _reply_names(report["messages"][0]), mode
    finally:
        await runtime.close()


# --- интерфейс поставщика: Track, neighbor, rate ------------------------------------------


async def test_rate_and_neighbor_calls(tmp_path):
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    ctx = ScenarioContext(runtime, dict(service_ctx(runtime, "x")))
    try:
        provider = music.selected(runtime.settings)
        track = await music.neighbor(ctx, provider, "t1", {"type": "search"}, -1)
        assert track is None  # neighbor(-1) вернул None — назад нечего
        track = await music.neighbor(ctx, provider, "t1", {"type": "search"}, 1)
        assert track == {"id": "t9", "title": "Следующий трек", "artist": "Тестовый Артист",
                         "duration_ms": 190000,
                         "stream": {"url": "https://example.com/t9.mp3", "format": "mp3"}}
        assert await music.rate(ctx, provider, "t1", True) is True
        calls = json.loads(marker.read_text(encoding="utf-8"))
        assert ["neighbor", "t1", {"type": "search"}, -1] in calls
        assert ["rate", "t1", True] in calls
    finally:
        await runtime.close()


async def test_normalize_track_contract():
    """Track приводится к контракту 089: без id/url не играет, формат — нижний регистр."""
    assert music.normalize_track({"id": "x", "stream": {"url": "http://x"}}) is None
    assert music.normalize_track({"id": "x", "title": "без потока"}) is None
    track = music.normalize_track({"id": " x ", "title": "Т", "artist": "А", "album": "Аль",
                                   "duration_ms": "1500", "cover_url": "http://c",
                                   "stream": {"url": " http://s ", "format": "MP3",
                                              "decode_key": "k"}})
    assert track == {"id": "x", "title": "Т", "artist": "А", "duration_ms": 1500,
                     "stream": {"url": "http://s", "format": "mp3", "decode_key": "k"},
                     "album": "Аль", "cover_url": "http://c"}


# --- захват OAuth аккаунта: реальные обращения колонки к Яндексу -----------------------

async def test_captured_oauth_from_http_header(tmp_path):
    """Authorization: OAuth … от опознанной колонки (заголовки устройства) — в БД
    (meta yandex_oauth) и в кеше ядра для поставщика; от неопознанного клиента токен
    не принимается; мусор вместо токена не затирает захват."""
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-A', 'user_id': 'owner',
                               'device_ids': ['device-id-a']})
    app = create_app(runtime=runtime)
    runner, url = await listen(app)
    try:
        async with ClientSession() as client:
            async with client.get(url + "/unknown/path",
                                  headers={"Host": "quasar.yandex.net",
                                           "Authorization": "OAuth " + "y0_" + "a" * 30}) as r:
                await r.read()
            assert runtime.store.meta("yandex_oauth") is None  # клиент не опознан — нет захвата
            assert music.captured_oauth() == ""
            async with client.get(url + "/unknown/path",
                                  headers={"Host": "quasar.yandex.net", "x-uprx-device-id": "device-id-a",
                                           "Authorization": "OAuth " + "y0_" + "a" * 30}) as r:
                await r.read()
            raw = runtime.store.meta("yandex_oauth")
            assert raw and json.loads(raw)["token"].startswith("y0_")
            assert music.captured_oauth().startswith("y0_")
            async with client.get(url + "/unknown/path",
                                  headers={"Host": "quasar.yandex.net", "x-uprx-device-id": "device-id-a",
                                           "Authorization": "OAuth junk"}) as r:
                await r.read()
            assert music.captured_oauth().startswith("y0_")  # мусор не затирает захват
            async with client.get(url + "/unknown/path?device_id=device-id-a",
                                  headers={"Host": "quasar.yandex.net",
                                           "Authorization": "OAuth 1.test." + "c" * 30}) as r:
                await r.read()
            assert music.captured_oauth().startswith("1.test.")  # токен с точками, устройство по query
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_captured_oauth_from_ws_handshake_of_known_device(tmp_path):
    """X-UPRX-OAUTH-TOKEN в WS-handshake захватывается только от опознанной колонки."""
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-A', 'user_id': 'owner',
                               'device_ids': ['device-id-a']})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.ws_connect(url + "/uni.ws", headers={
                    "x-uprx-device-id": "device-id-unknown", "x-uprx-oauth-token": "y0_" + "b" * 30}):
                pass  # неопознанное устройство: токен не принимается
            assert music.captured_oauth() == ""
            async with client.ws_connect(url + "/uni.ws", headers={
                    "x-uprx-device-id": "device-id-a", "x-uprx-oauth-token": "y0_" + "b" * 30}) as ws:
                await ws.close()
        assert music.captured_oauth().startswith("y0_")
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_captured_oauth_only_for_yandex_provider(tmp_path):
    """Захваченный OAuth видит только поставщик Яндекс.Музыки: другой поставщик
    получает обычный ExtensionContext без captured_oauth."""
    runtime = Runtime(tmp_path)
    make_music_extension(runtime, ext_id="fake-ext",
                         files={"music/fake.py": _provider_module(tmp_path / "fake.json")},
                         phrases=MUSIC_PHRASES)
    make_music_extension(runtime, ext_id="yandex-ext",
                         files={"music/yandex.py": _provider_module(tmp_path / "yandex.json",
                                                                    provider_id="yandex-music")},
                         phrases=MUSIC_PHRASES)
    runtime.extensions.load_enabled()
    try:
        assert music.note_token(runtime, "y0_" + "c" * 30)
        token = music.captured_oauth()
        assert token.startswith("y0_")

        class _Ctx:
            def __init__(self, rt):
                self._rt = rt

            def _services_runtime(self):
                return self._rt

        ctx = _Ctx(runtime)
        fake = music.extension_context(ctx, {"id": "fake", "ext_id": "fake-ext"})
        yandex = music.extension_context(ctx, {"id": "yandex-music", "ext_id": "yandex-ext"})
        assert not isinstance(fake, music._WithSystemValues)
        assert fake.get("captured_oauth") in (None, "")  # другой поставщик токена не видит
        assert isinstance(yandex, music._WithSystemValues)
        assert yandex.get("captured_oauth") == token
    finally:
        await runtime.close()


async def test_builtin_and_external_yandex_both_get_token(tmp_path):
    """Встроенный builtin.yandex-music (service="yandex-music") и внешний поставщик
    с тем же service — два разных поставщика в одном реестре; токен аккаунта
    получают оба (признак сервиса вместо id)."""
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, ext_id="ext-ym",
                         files={"music/yandex.py": _provider_module(marker, provider_id="yandex-music",
                                                                    service="yandex-music")},
                         phrases=MUSIC_PHRASES, module="music/yandex.py")
    runtime.extensions.load_enabled()
    try:
        ids = set(providers.PROVIDERS["music"])
        assert {"builtin.yandex-music", "yandex-music", "builtin.spotify"} <= ids
        records = providers.PROVIDERS["music"]
        assert records["yandex-music"]["origin"] == "extension"
        assert records["builtin.yandex-music"]["origin"] == "builtin"
        assert records["yandex-music"]["service"] == "yandex-music"
        assert music.note_token(runtime, "y0_" + "e" * 30)

        class _Ctx:
            def __init__(self, rt):
                self._rt = rt

            def _services_runtime(self):
                return self._rt

        ctx = _Ctx(runtime)
        for record_id in ("builtin.yandex-music", "yandex-music"):
            record = records[record_id]
            assert music.is_yandex_music(record)
            got = music.extension_context(ctx, record)
            assert isinstance(got, music._WithSystemValues)
            assert got.get("captured_oauth") == music.captured_oauth()
        spotify = music.extension_context(ctx, records["builtin.spotify"])
        assert not isinstance(spotify, music._WithSystemValues)
    finally:
        await runtime.close()


async def test_load_token_clears_cache_for_other_runtime(tmp_path):
    """Кеш захваченного токена — глобальный: гидратация из пустой БД другого Runtime
    чужой токен убирает."""
    first = Runtime(tmp_path / "a")
    try:
        assert music.note_token(first, "y0_" + "d" * 30)
        assert music.captured_oauth().startswith("y0_")
    finally:
        await first.close()
    second = Runtime(tmp_path / "b")  # другая БД — там токена нет
    try:
        music.load_token(second.store)
        assert music.captured_oauth() == ""
    finally:
        await second.close()


async def test_no_provider_bare_template_yields_to_next_candidate(tmp_path):
    """Голый шаблон («запусти спотифай») без поставщика — сценарий уступает: реплику
    отвечает следующий кандидат (device.app.open), а не «Для этого нет источника данных.»."""
    runtime = Runtime(tmp_path)
    _disable_builtin_providers(runtime)  # мир без поставщиков музыки
    try:
        report = await say(runtime, "запусти спотифай")
        assert report["handled"], report
        message = report["messages"][0]
        speech = _reply_speech(message)
        assert speech != "Для этого нет источника данных."
        assert "не найдено" in speech.lower(), speech
    finally:
        await runtime.close()


# --- recoverCommit: подтверждённый переход трека ----------------------------------------


def _commit_ctx(runtime, token, route_mode="emulate"):
    """Контекст колбэка recoverCommit (как собирает voice.py с EVENT_FILTERS)."""
    ctx = service_ctx(runtime, "@@mm_semantic_frame", route_mode=route_mode)
    ctx["input"]["music_type"] = "recoverCommit"
    ctx["input"]["music_token"] = token
    return ctx


def _play_payload(report):
    """audio_play из первого сообщения отчёта (start_reply/step_reply)."""
    [message] = report["messages"]
    return _payload_of(message, "audio_play")


async def test_start_reply_carries_recover_commit_token(tmp_path):
    """audio_play «включи музыку» несёт callbacks.on_started.payload.music с токеном."""
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    try:
        report = await say(runtime, "включи музыку", deliver=False)
        play = _play_payload(report)
        commit = play["callbacks"]["on_started"]["payload"]["music"]
        assert commit["type"] == "recoverCommit" and commit["recoverCommitToken"]
        token = commit["recoverCommitToken"]
        entry = music.take_commit(token)  # токен сохранён вместе с текущим треком
        assert entry is not None and entry["current_id"] == "t1"
        assert entry["ext_id"] == "fake-music"
    finally:
        await runtime.close()


async def test_commit_is_silent_and_consumes_token(tmp_path):
    """Колонка подтверждает старт каждого трека — ответ тихий, трек не переключается,
    токен снимается; соседа заранее не запрашиваем (neighbor сдвигает очередь поставщика)."""
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    try:
        report = await say(runtime, "включи музыку", deliver=False)
        token = _play_payload(report)["callbacks"]["on_started"]["payload"]["music"]["recoverCommitToken"]
        for _ in range(10):
            await asyncio.sleep(0)
        commit_report = await runtime.dialog.handle_event(
            "music.recover_commit", _commit_ctx(runtime, token), "dlg-1", await _collect([]))
        assert not commit_report["handled"]  # умолчание маршрута — тихое подтверждение
        assert music.take_commit(token) is None  # токен снят
        calls = json.loads(marker.read_text(encoding="utf-8"))
        assert not [call for call in calls if call[0] == "neighbor"]
    finally:
        await runtime.close()


async def test_commit_with_foreign_token_stays_silent(tmp_path):
    """Чужой/устаревший токен — сценарий отказывается: умолчание маршрута — тихое
    подтверждение (как у quasar-server: несовпадение игнорируется)."""
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    try:
        report = await runtime.dialog.handle_event(
            "music.recover_commit", _commit_ctx(runtime, "foreign-token"), "dlg-1", await _collect([]))
        assert not report["handled"]
        message = await runtime.dialog.quiet_ack(_commit_ctx(runtime, "foreign-token"))
        assert _reply_names(message) == ["tts_play_placeholder"]
    finally:
        await runtime.close()


async def test_next_prev_continue_phrases_use_provider_queue(tmp_path):
    """При треке в плеере колонки: «дальше» — neighbor(+1) стеком «дальше»; «предыдущий»
    без соседа — «Это первый трек.»; «продолжи» не на паузе — текущий трек поставщика.
    Плеер пуст — фразы не музыке (игры: «дальше» — ход модели)."""
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    try:
        assert not (await say(runtime, "дальше", deliver=False))["handled"]  # плеер пуст
        station = {"player": {"state": "playing", "playing": True, "title": "Трек 1"}}
        report = await say(runtime, "дальше", station=station, deliver=False)
        assert report["handled"] and report["scenario"] == "media.music", report
        [message] = report["messages"]
        assert _reply_names(message) == ["tts_play_placeholder", "apply_player_playback_queue_directive",
                                         "set_audio_player_sequence_state_directive", "audio_play"]
        assert _payload_of(message, "audio_play")["stream"]["url"] == "https://example.com/t9.mp3"
        report = await say(runtime, "предыдущий трек", station=station, deliver=False)
        assert report["scenario"] == "media.music" and report["speech"] == "Это первый трек.", report
        report = await say(runtime, "продолжи", station=station, deliver=False)
        assert report["scenario"] == "media.music", report
        calls = json.loads(marker.read_text(encoding="utf-8"))
        assert [call[3] for call in calls if call[0] == "neighbor"] == [1, -1, 0]
    finally:
        await runtime.close()


# --- кнопки виджета музыки (TMusicTouchTypedCallback) -----------------------------------

# «плей» в виджете .202: Any(NAlice.TMusicTouchTypedCallback{Command{Continue{}}})
WIDGET_PLAY = "CjN0eXBlLmdvb2dsZWFwaXMuY29tL05BbGljZS5UTXVzaWNUb3VjaFR5cGVkQ2FsbGJhY2sSBBICcgA="


def test_widget_play_parsed_as_continue():
    """Кнопка «плей» виджета — music_command=continue; другой колбэк — пусто."""
    from google.protobuf import struct_pb2
    from server_emulator.schema import schemas_ready
    from server_emulator.voice import _music_command
    if not schemas_ready():
        pytest.skip("нет схем протокола")
    payload = struct_pb2.Struct()
    payload.update({"typed_callback_serialized": WIDGET_PLAY})
    assert _music_command(payload) == "continue"
    payload.update({"typed_callback_serialized": ""})
    assert _music_command(payload) == ""


def _touch_ctx(runtime, command, station=None):
    """Контекст колбэка кнопки виджета (как собирает voice.py с EVENT_FILTERS)."""
    ctx = service_ctx(runtime, "@@mm_typed_callback")
    ctx["input"]["music_command"] = command
    if station:
        ctx["station"] = station
    return ctx


async def test_widget_buttons_follow_voice_commands(tmp_path):
    """«далее» — neighbor(+1) стеком «дальше», без речи; «назад» без соседа и «плей»
    без текущего трека (neighbor 0) — тихое подтверждение."""
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)}, phrases=MUSIC_PHRASES)
    _disable_builtin_providers(runtime)
    station = {"player": {"state": "playing", "playing": True, "title": "Трек 1"}}
    try:
        sent = []
        report = await runtime.dialog.handle_event(
            "music.touch", _touch_ctx(runtime, "next", station), "dlg-1", await _collect(sent))
        assert report["handled"], report
        [message] = _sent_messages(sent)
        assert _reply_names(message) == ["tts_play_placeholder", "apply_player_playback_queue_directive",
                                         "set_audio_player_sequence_state_directive", "audio_play"]
        assert not _reply_speech(message)
        report = await runtime.dialog.handle_event(
            "music.touch", _touch_ctx(runtime, "prev", station), "dlg-1", await _collect([]))
        assert not report["handled"]
        report = await runtime.dialog.handle_event(  # текущего трека у тестового поставщика нет
            "music.touch", _touch_ctx(runtime, "continue", station), "dlg-1", await _collect([]))
        assert not report["handled"]
        calls = json.loads(marker.read_text(encoding="utf-8"))
        assert [call[3] for call in calls if call[0] == "neighbor"] == [1, -1, 0]
    finally:
        await runtime.close()

