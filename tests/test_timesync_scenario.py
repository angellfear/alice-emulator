"""Служебные колбэки не озвучиваются: встроенные JSON-сценарии раздела «Системные»
(scenarios/system/*.json) отвечают тихим ответом облака.

Фон: станция с выключенными микрофонами раз в 75 с присылала TextInput
Type=server_action Name=@@mm_typed_callback (purpose=sync_timezone, Text пустой);
эмулятор считал это фразой и отвечал устной заглушкой. Теперь: system.timesync (set_timezone/
set_location из params), system.service_ack (тихий ack), виджет погоды —
informers.weather.widget, тап по виджету — informers.weather.
"""
import base64
import datetime

import pytest
from google.protobuf import json_format

from server_emulator.runtime import ROOT, Runtime
from server_emulator.scenario_engine.bindings import BindingError, _tz_offset_seconds
from server_emulator.schema import load_type, schema_file
from server_emulator.voice import is_service_text_input, service_event_fields

CLIENT = load_type(ROOT / 'schemas', 'alice-api.pb', 'NAlice.NAliceApi.TClientMessage')
SERVER = load_type(ROOT / 'schemas', 'alice-api.pb', 'NAlice.NAliceApi.TServerMessage')
SN = 'SN-A'
LAT, LON = 55.755826, 37.617326
FORECAST = {
    "current": {"temperature_2m": 12.4, "apparent_temperature": 10.1, "weather_code": 3, "is_day": 1},
    "hourly": {"time": [f"2026-10-27T{h % 24:02d}:00" for h in range(12, 36)],
               "temperature_2m": [10 + h % 5 for h in range(24)], "weather_code": [3] * 24, "is_day": [1] * 24},
    "daily": {"temperature_2m_max": [14.2], "temperature_2m_min": [8.1], "precipitation_probability_max": [10]},
}


async def collect(sent, data):
    sent.append(data)


def service_request(purpose='sync_timezone', timezone='Asia/Tashkent', name='@@mm_typed_callback'):
    """TextInput как на wire (архив 2026-10-21, connection c2809aa8…)."""
    payload = {'request_params': {'disable_output_speech': True, 'is_parallel': True},
               'analytics': {'purpose': purpose, 'origin': 'SmartSpeaker'}}
    return json_format.ParseDict({'Event': {'TextInput': {
        'Header': {'RequestId': 'req-1'},
        'Application': {'Timezone': timezone, 'DeviceId': SN},
        'Request': {'Event': {'Type': 'server_action', 'Name': name, 'Payload': payload}}}}}, CLIENT())


def make_runtime(tmp_path, geo=True):
    """Владелец + колонка; гео дома — params пользователя; погода без сети."""
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'Alex'})
    runtime.identities.mutate({'operation': 'device', 'sn': SN, 'user_id': 'owner', 'device_ids': []})
    if geo:
        runtime.quasar_params.set_value("user:owner", "user.location.latitude", LAT)
        runtime.quasar_params.set_value("user:owner", "user.location.longitude", LON)

    async def offline(key, args):
        raise ConnectionError("провайдер погоды недоступен")

    runtime.scenario_plugins["weather"] = offline
    return runtime


def ctx_for(runtime, purpose='sync_timezone', timezone='Asia/Tashkent', name='@@mm_typed_callback'):
    """Контекст server_action-ветки voice.py: поля запроса + реальный контекст сценариев."""
    request = service_request(purpose=purpose, timezone=timezone, name=name)
    fields = service_event_fields(request.Event.TextInput, request.Event.TextInput.Request.Event)
    ctx = {'input': {'message_id': 'msg-1', 'request_id': 'req-1', 'dialog_id': 'dlg-1',
                     'sn': SN, 'text': '', **fields},
           'clock': {'now': datetime.datetime.now()}, 'sequence': 1}
    ctx.update(runtime._scenario_context(sn=SN))
    return ctx


def test_library_has_system_scenarios():
    from server_emulator.scenario_engine.library import ERRORS, SCENARIOS
    assert ERRORS == []
    assert {"system.timesync", "system.service_ack", "informers.weather.widget"} <= set(SCENARIOS)


def test_service_text_input_detection():
    assert is_service_text_input(service_request())
    phrase = json_format.ParseDict({'Event': {'TextInput': {
        'Request': {'Event': {'Type': 'text_input', 'Text': 'привет'}}}}}, CLIENT())
    assert not is_service_text_input(phrase)
    sync = json_format.ParseDict({'Event': {'SynchronizeStateRequest': {}}}, CLIENT())
    assert not is_service_text_input(sync)


def test_service_event_fields_from_wire():
    request = service_request()
    fields = service_event_fields(request.Event.TextInput, request.Event.TextInput.Request.Event)
    assert fields == {'event_name': '@@mm_typed_callback', 'purpose': 'sync_timezone',
                      'disable_output_speech': True, 'timezone': 'Asia/Tashkent',
                      'callback': '', 'frame': ''}
    request = service_request(purpose='', timezone='')
    fields = service_event_fields(request.Event.TextInput, request.Event.TextInput.Request.Event)
    assert fields == {'event_name': '@@mm_typed_callback', 'purpose': '', 'disable_output_speech': True,
                      'timezone': '', 'callback': '', 'frame': ''}


def test_tz_offset_seconds():
    assert _tz_offset_seconds('Asia/Tashkent') == 18000
    assert _tz_offset_seconds('Europe/Moscow') == 10800
    with pytest.raises(BindingError):
        _tz_offset_seconds('')
    with pytest.raises(BindingError):
        _tz_offset_seconds('Mars/Olympus')


async def test_timesync_gets_silent_cloud_reply(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.settings['model']['stub_text'] = 'Проверка связи. Я вас слышу.'  # заглушка не должна прозвучать
    try:
        sent = []
        report = await runtime.dialog.handle_event('@@mm_typed_callback', ctx_for(runtime), 'dlg-1',
                                                   lambda data: collect(sent, data))
        assert report['handled'] and report['scenario'] == 'system.timesync'
        assert len(sent) == 1 and sent[0].startswith(b'AAPI')
        message = SERVER.FromString(sent[0][4:])
        response = message.Event.AliceResponse
        # Тишина: без голоса, без карточки, без текста — как ответ реального облака
        assert not response.VoiceResponse.HasVoiceResponse
        assert not response.VoiceResponse.OutputSpeech.Text
        assert len(response.Response.Cards) == 0
        # set_timezone + set_location собраны из JSON (proto+fields) — байт-в-байт с облаком
        names = [d.Name for d in response.Response.Directives]
        assert names == ['set_timezone', 'set_location']
        timezone_directive, location_directive = response.Response.Directives
        assert base64.b64encode(timezone_directive.PayloadRaw).decode() == \
            'ChMKDUFzaWEvVGFzaGtlbnQQ0IwBEh1zZXRfZGV2aWNlX3RpbWV6b25lX2RpcmVjdGl2ZQ=='
        # та же структура, что у облака (перехват: 55.751244, 37.618423), но гео — владельца из params
        location = load_type(ROOT / 'schemas', schema_file(ROOT / 'schemas', 'centaur'),
                             'NAlice.TGeoSyncCapability.TSetLocationDirective').FromString(location_directive.PayloadRaw)
        assert (location.Location.Latitude, location.Location.Longitude) == (LAT, LON)
        assert location.Name == 'set_device_location_directive'
        assert timezone_directive.IsLedSilent and location_directive.IsLedSilent
        assert timezone_directive.AnalyticsType == 'set_device_timezone_directive'
        # service-конверт: без голосовых добавок, с ResponseId и пустыми EffectiveSettings
        assert not [d for d in response.Response.Directives if d.Name == 'tts_play_placeholder']
        assert not response.Header.HasField('SequenceNumber') or response.Header.SequenceNumber == 0
        assert response.Response.Alice2EffectiveSettings.EffectiveSettings.ListFields() == []
        assert response.Header.ResponseId
        assert message.Timings.SendingTime.ToSeconds() > 1_700_000_000
        assert message.Event.Header.RefMessageId == 'msg-1'
        assert response.Header.RequestId == 'req-1'
    finally:
        await runtime.close()


async def test_timesync_without_owner_geo_sends_timezone_only(tmp_path):
    runtime = make_runtime(tmp_path, geo=False)
    try:
        sent = []
        report = await runtime.dialog.handle_event('@@mm_typed_callback', ctx_for(runtime), 'dlg-1',
                                                   lambda data: collect(sent, data))
        assert report['handled'] and report['scenario'] == 'system.timesync'
        response = SERVER.FromString(sent[0][4:]).Event.AliceResponse
        assert [d.Name for d in response.Response.Directives] == ['set_timezone']  # гео не задано
        assert b'Asia/Tashkent' in response.Response.Directives[0].PayloadRaw
        assert not response.VoiceResponse.HasVoiceResponse
    finally:
        await runtime.close()


async def test_timesync_timezone_from_params_overrides_station(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.quasar_params.set_value(f"device:{SN}", "device.timezone", "Europe/Moscow")
    try:
        sent = []
        await runtime.dialog.handle_event('@@mm_typed_callback', ctx_for(runtime), 'dlg-1',
                                          lambda data: collect(sent, data))
        payload = SERVER.FromString(sent[0][4:]).Event.AliceResponse.Response.Directives[0].PayloadRaw
        assert b'Europe/Moscow' in payload and b'Asia/Tashkent' not in payload
    finally:
        await runtime.close()


async def test_other_service_purposes_get_silent_ack(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.settings['model']['stub_text'] = 'Проверка связи. Я вас слышу.'
    try:
        for purpose in ('collect_upper_shutter', 'screensaver_callback_get_config', ''):
            sent = []
            report = await runtime.dialog.handle_event('@@mm_typed_callback', ctx_for(runtime, purpose=purpose),
                                                       'dlg-1', lambda data: collect(sent, data))
            assert report['handled'] and report['scenario'] == 'system.service_ack', purpose
            response = SERVER.FromString(sent[0][4:]).Event.AliceResponse
            assert not response.VoiceResponse.HasVoiceResponse
            assert not response.VoiceResponse.OutputSpeech.Text
            assert len(response.Response.Cards) == 0
            assert not [d for d in response.Response.Directives if d.Name == 'set_timezone']
        for name in ('@@mm_semantic_frame', 'music_thin_client_on_started'):
            report = await runtime.dialog.handle_event(name, ctx_for(runtime, name=name, purpose='x'), 'dlg-1',
                                                       lambda data: collect([], data))
            assert report['handled'] and report['scenario'] == 'system.service_ack', name
    finally:
        await runtime.close()


async def test_get_informers_sends_weather_informer(tmp_path):
    """get_informers → update_all_informers_directive с погодой из БД (как облако на .85)."""
    informers = load_type(ROOT / 'schemas', schema_file(ROOT / 'schemas', 'centaur'),
                          'NAlice.TInformersCapability.TUpdateAllInformersDirective')
    runtime = make_runtime(tmp_path)
    runtime.store.scenario_data_set("dataset:weather", "system", "", f"{LAT},{LON}", FORECAST)
    try:
        sent = []
        ctx = ctx_for(runtime, purpose='get_informers')
        ctx['clock'] = {'now': datetime.datetime(2026, 10, 25, 1, 1)}
        report = await runtime.dialog.handle_event('@@mm_typed_callback', ctx, 'dlg-1',
                                                   lambda data: collect(sent, data))
        assert report['handled'] and report['scenario'] == 'informers.weather.widget'
        message = SERVER.FromString(sent[0][4:])
        response = message.Event.AliceResponse
        assert [d.Name for d in response.Response.Directives] == ['update_all_informers_directive']
        directive = response.Response.Directives[0]
        assert directive.IsLedSilent and directive.AnalyticsType == 'update_all_informers'
        assert not response.VoiceResponse.HasVoiceResponse and len(response.Response.Cards) == 0
        assert message.Event.Header.RefMessageId == 'msg-1'
        payload = informers.FromString(directive.PayloadRaw)
        weather = payload.WeatherInformerState
        assert payload.Name == 'update_all_informers'
        assert round(float(weather.Temperature)) == 12
        assert weather.IconType == 'ovc'
        assert weather.Icon == 'https://yastatic.net/weather/i/icons/funky/png/flat/128/ovc.png'
        assert weather.Condition.Title == 'облачно' and weather.Condition.Season == 'autumn'
    finally:
        await runtime.close()


async def test_get_informers_without_data_is_silent(tmp_path):
    """Нет данных и плагин молчит — тихий ответ без директивы (виджет не трогаем)."""
    runtime = make_runtime(tmp_path)
    try:
        sent = []
        report = await runtime.dialog.handle_event('@@mm_typed_callback', ctx_for(runtime, purpose='get_informers'),
                                                   'dlg-1', lambda data: collect(sent, data))
        assert report['handled'] and report['scenario'] == 'informers.weather.widget'
        assert not [m for m in sent if b'update_all_informers' in m]
    finally:
        await runtime.close()


async def test_weather_informer_tap_shows_weather_card(tmp_path):
    """Тап по виджету (@@mm_semantic_frame, purpose=weather_informer_tap_action) → карточка погоды."""
    runtime = make_runtime(tmp_path)
    runtime.store.scenario_data_set("dataset:weather", "system", "", f"{LAT},{LON}", FORECAST)
    try:
        sent = []
        ctx = ctx_for(runtime, purpose='weather_informer_tap_action', name='@@mm_semantic_frame')
        report = await runtime.dialog.handle_event('@@mm_semantic_frame', ctx, 'dlg-1',
                                                   lambda data: collect(sent, data))
        assert report['handled'] and report['scenario'] == 'informers.weather'
        response = SERVER.FromString(sent[0][4:]).Event.AliceResponse
        names = [d.Name for d in response.Response.Directives]
        assert 'divui_show_view' in names and 'draw_animation_directive' in names
        assert response.Response.Cards[0].Text.startswith('Сейчас +12')
    finally:
        await runtime.close()


async def test_unknown_service_name_and_user_phrases_unaffected(tmp_path):
    runtime = make_runtime(tmp_path)
    try:
        report = await runtime.dialog.handle_event('future_callback', ctx_for(runtime, name='future_callback'),
                                                   'dlg-1', lambda data: None)
        assert not report['handled'] and report['reason'] == 'no_match'
        sent = []
        ctx = ctx_for(runtime)
        ctx['input'] = {'text': 'привет', 'sn': SN}
        report = await runtime.dialog.handle_text('привет', ctx, 'dlg-2', lambda data: collect(sent, data))
        assert not report['handled'] and not sent
    finally:
        await runtime.close()


def _user_scenario(scenario_id, phrase):
    return {"format_version": 2, "id": scenario_id, "version": "1.0.0", "category": "custom",
            "title": {"ru": scenario_id}, "triggers": [{"kind": "phrase", "phrases": [phrase]}],
            "flow": [{"id": "r", "op": "compose", "reply": {"text": phrase}}, {"id": "d", "op": "finish"}]}


async def test_stale_settings_form_cannot_wipe_scenarios(tmp_path):
    """UI-форма, открытая до добавления сценариев, присылает старый список;
    файлы state/scenarios — источник правды: сценарий с живым файлом
    восстанавливается, удалённый (без файла) — нет."""
    runtime = Runtime(tmp_path)
    try:
        cfg = runtime.public()
        cfg['scenarios'] = [_user_scenario('user.a', 'альфа'), _user_scenario('user.b', 'бета')]
        await runtime.save_settings(cfg)
        assert {s['id'] for s in runtime.settings['scenarios']} >= {'user.a', 'user.b'}
        stale = runtime.public()
        stale['scenarios'] = [_user_scenario('user.other', 'привет')]
        await runtime.save_settings(stale)
        ids = {s['id'] for s in runtime.settings['scenarios']}
        assert 'user.a' in ids and 'user.b' in ids
        (runtime.scenario_dir / 'user.a.json').unlink()
        fresh = runtime.public()
        fresh['scenarios'] = [s for s in fresh['scenarios'] if s['id'] != 'user.a']
        await runtime.save_settings(fresh)
        ids = {s['id'] for s in runtime.settings['scenarios']}
        assert 'user.a' not in ids and 'user.b' in ids
    finally:
        await runtime.close()
