"""TASK-087: медиа на штатном плеере колонки.

Своей медиатеки/очереди у эмулятора нет: «стоп/пауза» отвечает теми же директивами,
что настоящий сервер (обезличенный образец tests/fixtures/directives/media-pause.json,
журнал 2026-10-21…2026-10-26), состояние плеера читается из запроса колонки
(capability 46, station_state), колбэки HollywoodMusic (on_started/on_stopped/
@@mm_stack_engine_get_next) — тихий ответ, как в образце (только tts_play_placeholder,
без речи; журнал 2026-10-21…26).
"""
import datetime
import json
from pathlib import Path

import pytest
from google.protobuf import json_format

from server_emulator import station_state
from server_emulator.aapi_handlers import route as callback_route
from server_emulator.media_directives import PLAYER_PAUSE_ANIMATION, S3_BUCKET
from server_emulator.runtime import Runtime
from server_emulator.schema import load_type, schemas_dir, station_file
from server_emulator.server import create_app
from server_emulator.voice import service_event_fields
from tests.conftest import _open_session, listen, types

CLIENT, SERVER = types()
ANIMATION_PROTO = "NAlice.TAnimationCapability.TDrawAnimationDirective"
HIDE_VIEW_PROTO = "NAlice.TLayeredDivUICapability.TDivUIHideViewDirective"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "directives"


def _station_type(full_name):
    return load_type(schemas_dir(), station_file(), full_name)


def _state_raw(state_type="playing", title="Ромашки", subtitle="Смешарики",
               is_playing=True, played_ms=30_000, duration_ms=186_000):
    """EnvironmentStateRaw колонки с capability 46 — как в журнале (2026-10-21 20:55):
    State.StateType (Idle/Playing/Paused) + CommonPlayerState с прогрессом."""
    env = load_type(schemas_dir(), "alice-api.pb", "NAlice.NAliceApi.TEnvironmentState")()
    endpoint = env.Endpoints.add()
    endpoint.Id = "dev-t"
    cap = _station_type("NAlice.TAudioPlayerCapability")()
    cap.State.StateType = {"idle": 1, "playing": 2, "paused": 3}[state_type]
    if state_type != "idle":
        cap.State.Title = title
        cap.State.SubTitle = subtitle
        cap.State.CommonPlayerState.IsPlaying = bool(is_playing)
        cap.State.CommonPlayerState.PlayedMs = int(played_ms)
        cap.State.CommonPlayerState.DurationMs = int(duration_ms)
    holder = endpoint.AnyCapabilities.add()
    holder.CapabilityType = station_state.PLAYER_CAPABILITY
    holder.RawCapability = cap.SerializeToString()
    return env.SerializeToString()


# --- состояние плеера из запроса (station_state) ------------------------------------


def test_station_state_player_playing_and_idle():
    state = station_state.parse(_state_raw())
    assert state["player"] == {"state": "playing", "playing": True, "title": "Ромашки",
                               "subtitle": "Смешарики", "played_ms": 30_000,
                               "duration_ms": 186_000}
    assert station_state.parse(_state_raw("paused", is_playing=False))["player"]["state"] == "paused"
    idle = station_state.parse(_state_raw("idle"))
    assert idle["player"] == {"state": "idle", "playing": False, "title": "", "subtitle": "",
                              "played_ms": 0, "duration_ms": 0}
    # запрос без cap 46 — пустое состояние, не ошибка
    assert station_state.parse(b"")["player"] == {}


def test_pause_scenario_does_not_keep_its_own_queue():
    """Очереди плеера в runtime больше нет — состояние только из запроса (087)."""
    assert not hasattr(Runtime.__new__(Runtime), "media_state")
    from server_emulator import sdk
    props = {name for name in dir(sdk.ScenarioContext) if not name.startswith("_")}
    assert {"media", "library", "media_base"}.isdisjoint(props)
    assert "player" in props


# --- WS: «стоп» — директивы образца --------------------------------------------------


async def _ask(tmp_path, phrase, env_state=None):
    runtime = Runtime(tmp_path)
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    runner, url = await listen(create_app(runtime=runtime))
    try:
        reply, _, ws = await _open_session(runtime, url, phrase, env_state=env_state)
        await ws.close()
        return reply
    finally:
        await runner.cleanup()
        await runtime.close()


def _names(reply):
    return [d.Name for d in reply.Event.AliceResponse.Response.Directives]


def _directive(reply, name):
    return next(d for d in reply.Event.AliceResponse.Response.Directives if d.Name == name)


def _decoded(reply, name, proto):
    message = _station_type(proto)()
    message.ParseFromString(bytes(_directive(reply, name).PayloadRaw))
    return json_format.MessageToDict(message, preserving_proto_field_name=True)


def _check_sample(sample_name, reply):
    """Как в test_time_scenarios: имена и порядок директив, type, is_led_silent, payload."""
    sample = json.loads((FIXTURES / sample_name).read_text(encoding="utf-8"))
    assert _names(reply) == [d["name"] for d in sample["directives"]], sample_name
    for want in sample["directives"]:
        directive = _directive(reply, want["name"])
        assert directive.Type == want["type"], want["name"]
        assert directive.IsLedSilent is bool(want["is_led_silent"]), want["name"]
        if "decoded" in want:
            got = _decoded(reply, want["name"], want["proto"])
            assert got == want["decoded"], want["name"]
        elif "payload" in want:
            got = json_format.MessageToDict(directive.Payload, preserving_proto_field_name=True)
            assert got == want["payload"], want["name"]
        else:
            assert bytes(directive.PayloadRaw) == b"", want["name"]


PAUSE_ANALYTICS = {
    "tts_play_placeholder": "tts_play_placeholder",
    "divui_hide_view": "divui_hide_view",
    "draw_animation_directive": "draw_animation_directive",
    "player_pause": "general_conversation_player_pause",
    "clear_queue": "general_conversation_player_pause",
}


@pytest.mark.parametrize("phrase", ["стоп", "пауза", "подожди"])
async def test_stop_phrase_matches_sample_directives(tmp_path, phrase):
    """На играющей колонке (cap 46 Playing) — форма x20 из образца, речь пустая."""
    reply = await _ask(tmp_path, phrase, env_state=_state_raw())
    _check_sample("media-pause.json", reply)
    for name, analytics in PAUSE_ANALYTICS.items():
        assert _directive(reply, name).AnalyticsType == analytics, name
    alice = reply.Event.AliceResponse
    speech = alice.VoiceResponse.OutputSpeech.Text if alice.HasField("VoiceResponse") else ""
    assert speech == ""
    assert not [c.Text for c in alice.Response.Cards]


async def test_stop_form_does_not_depend_on_player_state(tmp_path):
    """Сервер слал одну форму и на паузе, и на Idle (typed-«стоп» на Idle — x20,
    журнал 2026-10-23 16:52:41 и ещё 4) — эмулятор тоже не ветвится."""
    for state in ("paused", "idle", None):
        env = _state_raw(state) if state else None
        reply = await _ask(tmp_path, "стоп", env_state=env)
        _check_sample("media-pause.json", reply)


async def test_hvatit_without_ringing_gets_pause_form(tmp_path):
    reply = await _ask(tmp_path, "хватит", env_state=_state_raw())
    assert "player_pause" in _names(reply)
    assert {"timer_stop_directive", "alarm_stop_directive"}.isdisjoint(_names(reply))


# --- колбэки HollywoodMusic: тихий ответ как в образце --------------------------------


def _service_ctx(runtime, name):
    """Контекст server_action-ветки voice.py для колбэка плеера (журнал: Text пустой,
    Event.Type=server_action, Name=music_thin_client_on_* / @@mm_stack_engine_get_next)."""
    request = CLIENT()
    request.Event.TextInput.Header.RequestId = "req-1"
    request.Event.TextInput.Application.DeviceId = "SN-A"
    event = request.Event.TextInput.Request.Event
    event.Type = 6
    event.Name = name
    return {**{"input": {"message_id": "msg-1", "request_id": "req-1", "dialog_id": "dlg-1",
                         "sn": "SN-A", "text": "",
                         **service_event_fields(request.Event.TextInput, event)}},
            "clock": {"now": datetime.datetime.now()}, "sequence": 1,
            **runtime._scenario_context(sn="SN-A")}


@pytest.mark.parametrize(("name", "provide"), [
    ("music_thin_client_on_started", "callbacks.ack"),
    ("music_thin_client_on_stopped", "callbacks.ack"),
    ("@@mm_stack_engine_get_next", "music.stack_next"),  # 099: сценарий поставщика, без него — умолчание
])
async def test_player_callbacks_match_silent_sample(tmp_path, name, provide):
    """Ответ колбэку — как в образце (on_started x112, on_stopped x134, get_next без
    очереди x4): пустой диалоговый ответ, только tts_play_placeholder {channel: Dialog},
    без речи и других директив. Get_next маршрутизируется поставщику музыки (099):
    поставщика нет — сценарий отказывается, умолчание маршрута (quiet_ack) даёт
    ту же форму образца."""
    assert callback_route(name, {}) == provide
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({"operation": "device", "sn": "SN-A", "device_ids": []})
    sent = []

    async def send(data):
        sent.append(data)

    try:
        report = await runtime.dialog.handle_event(provide, _service_ctx(runtime, name),
                                                   "dlg-1", send)
        if provide == "callbacks.ack":
            assert report["handled"]
        else:
            assert not report["handled"]  # поставщика нет — умолчание колбэка (voice.py)
            message = await runtime.dialog.quiet_ack(_service_ctx(runtime, name))
            sent.append(b"AAPI" + message.SerializeToString())
        names = []
        for raw in sent:
            message = SERVER.FromString(raw[4:])
            alice = message.Event.AliceResponse
            names += [d.Name for d in alice.Response.Directives]
            assert alice.VoiceResponse.OutputSpeech.Text == ""
        assert names == ["tts_play_placeholder"]
        placeholder = next(d for raw in sent for d in
                           SERVER.FromString(raw[4:]).Event.AliceResponse.Response.Directives
                           if d.Name == "tts_play_placeholder")
        got = json_format.MessageToDict(placeholder.Payload, preserving_proto_field_name=True)
        assert got == {"channel": "Dialog"}
    finally:
        await runtime.close()


def test_pause_animation_constants_match_sample():
    """Константы формы из расшифрованного payload_raw образца (20:35:28)."""
    assert S3_BUCKET == "https://quasar.s3.yandex.net"
    assert PLAYER_PAUSE_ANIMATION == "animations/player_pause"
