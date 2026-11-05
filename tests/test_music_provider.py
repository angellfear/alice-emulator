"""TASK-099: ядро поставщика музыки (music/*.py) и стек директорий виджета.

Поставщик — тестовое расширение (music/fake.py через реальный rebuild_extension_registries,
как в тестах actions/datasets). Настоящий Spotify/Яндекс в тестах не дёргается.
Форма стека — по расшифрованным образцам настоящего сервера (захват 2026-11-02-music,
server_emulator/state/captures/): полный стек «включи музыку» — tts_play_placeholder +
draw_animation(player_play) + show_audio_player_screen + apply_player_playback_queue
(TApplyMusicPlatformQueueDirective) + set_audio_player_sequence_state + audio_play;
«дальше» (колбэк @@mm_stack_engine_get_next → provide music.stack_next) — тройка
очередь+состояние+audio_play без экрана.
"""
import datetime
import json
from pathlib import Path

from google.protobuf import json_format

from server_emulator import music
from server_emulator.aapi_handlers import route as callback_route
from server_emulator.extensions import EMULATOR_VERSION
from server_emulator.runtime import Runtime
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


def _provider_module(marker, provider_id="fake"):
    """Модуль music/*.py: каждый вызов пишет в marker (JSON-список), треки — TRACKS."""
    path = str(marker).replace("\\", "/")
    tracks = json.dumps(TRACKS, ensure_ascii=False)
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

PROVIDER = {{"id": "{provider_id}", "title": "Поставщик для теста"}}
'''


def make_music_extension(runtime, ext_id="fake-music", provider_id="fake", files=None,
                         enabled=True):
    """Расширение-папка с music/*.py (dev-режим): manifest + записи installed/enabled."""
    root = runtime.directory / "ext" / ext_id
    (root / "music").mkdir(parents=True, exist_ok=True)
    manifest = {"id": ext_id, "name": ext_id, "version": "1.0.0", "description": "тест",
                "author": "T", "license": "MIT",
                "requires": {"emulator": f">={EMULATOR_VERSION}", "sdk": f">={SDK_VERSION}"},
                "permissions": {}}
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
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)})
    runtime.extensions.load_enabled()
    try:
        assert set(music.PROVIDERS) == {"fake"}
        provider = music.selected(runtime.settings)
        assert provider["id"] == "fake" and provider["ext_id"] == "fake-music"
        assert provider["title"] == "Поставщик для теста" and callable(provider["search"])
        assert provider["rate"] is not None
    finally:
        await runtime.close()


async def test_broken_provider_module_disables_extension(tmp_path):
    runtime = Runtime(tmp_path)
    make_music_extension(runtime, files={"music/bad.py": "raise ValueError('битый модуль')\n"})
    runtime.extensions.load_enabled()
    try:
        assert "fake-music" not in runtime.extensions.loaded
        assert any("битый модуль" in w["error"] for w in runtime.extensions.warnings)
        assert music.PROVIDERS == {}
    finally:
        await runtime.close()


async def test_provider_name_conflict_disables_second(tmp_path):
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, ext_id="first", files={"music/a.py": _provider_module(marker)})
    make_music_extension(runtime, ext_id="second", files={"music/b.py": _provider_module(marker)})
    runtime.extensions.load_enabled()
    try:
        # оба объявляют id «fake» — второй по алфавиту не включается, первый работает
        assert "second" not in runtime.extensions.loaded
        assert any("уже занято" in w["error"] for w in runtime.extensions.warnings)
        assert music.selected(runtime.settings)["ext_id"] == "first"
    finally:
        await runtime.close()


async def test_selected_setting_and_empty_registry(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        assert music.selected(runtime.settings) is None  # поставщиков нет
        marker = tmp_path / "marker.json"
        make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)})
        runtime.extensions.load_enabled()
        runtime.settings["music"]["provider"] = "нет-такого"
        assert music.selected(runtime.settings) is None  # выбранный отключён — источника нет
        runtime.settings["music"]["provider"] = "fake"
        assert music.selected(runtime.settings)["id"] == "fake"
    finally:
        await runtime.close()


# --- полный стек виджета на «включи музыку» ---------------------------------------------


async def test_full_widget_stack_on_play_phrase(tmp_path):
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)})
    runtime.extensions.load_enabled()
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
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)})
    runtime.extensions.load_enabled()
    try:
        report = await say(runtime, "включи музыку")
        assert report["handled"] and report["messages"]
        assert _reply_speech(report["messages"][0]) == "Включаю."
        assert json.loads(marker.read_text(encoding="utf-8"))[0] == ["search", "", "music"]
        queue = _decoded(report["messages"][0], "apply_player_playback_queue_directive", QUEUE_PROTO)
        assert len(queue["MusicPlatformQueue"]["Entities"]) == 5  # топ-5 поиска
    finally:
        await runtime.close()


async def test_search_kind_by_phrase(tmp_path):
    """«включи сказку/аудиокнигу/подкаст» — kind из реплики, хвост — запрос."""
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)})
    runtime.extensions.load_enabled()
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
    make_music_extension(runtime, files={"music/fake.py": module})
    runtime.extensions.load_enabled()
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
    make_music_extension(runtime, files={"music/fake.py": module})
    runtime.extensions.load_enabled()
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
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)})
    runtime.extensions.load_enabled()
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
    make_music_extension(runtime, files={"music/fake.py": module})
    runtime.extensions.load_enabled()
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
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)})
    runtime.extensions.load_enabled()
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


async def test_no_provider_simple_mode_answers_no_data_source(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings["intelligence"] = "scenarios"  # «Простой»
    try:
        report = await say(runtime, "включи музыку хороший день")
        assert report["handled"]
        message = report["messages"][0]
        assert _reply_names(message) == ["tts_play_placeholder"]
        assert _reply_speech(message) == "Для этого нет источника данных."
    finally:
        await runtime.close()


async def test_no_provider_model_mode_declines(tmp_path):
    """«Модель»: сценарий отказывается (None) — фразу забирает модель."""
    runtime = Runtime(tmp_path)
    runtime.settings["intelligence"] = "model"
    try:
        report = await say(runtime, "включи музыку хороший день")
        assert not report["handled"]  # реплика уходит модели
    finally:
        await runtime.close()


async def test_provider_answers_in_every_intelligence_mode(tmp_path):
    """Поставщик есть — играет он во всех режимах «Интеллекта» (модель не участвует)."""
    runtime = Runtime(tmp_path)
    marker = tmp_path / "marker.json"
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)})
    runtime.extensions.load_enabled()
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
    make_music_extension(runtime, files={"music/fake.py": _provider_module(marker)})
    runtime.extensions.load_enabled()
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
