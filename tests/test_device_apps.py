"""Приложения колонки — список из состояния запроса (TAppLaunchCapability.InstalledAppsList,
cap 39): scenario device.app.open находит приложение по имени и человеческому имени пакета,
промпт агента и режима «модель» получают компактные имена (intelligence.apps_context),
«какие приложения установлены?» перечисляет список без системных пакетов. Списки ведутся
по серийнику: две колонки — два списка. «Открой ютуб» — тихий запуск webview-картой
по образцу записи; прочие приложения из списка — launch_app по пакету (запись 2026-11-12);
без пакета — честный ответ. Колонка список не прислала — честный ответ «пока не получен».
"""
import json
from pathlib import Path

import pytest
from google.protobuf import json_format

from server_emulator import intelligence, station_state
from server_emulator.runtime import Runtime
from server_emulator.schema import load_type, schemas_dir, station_file

# Фикстура списка: фиктивный серийник с префиксом модели и пакеты, которых нет в живом
# реестре, — тесты не зависят от записей с колонок.
EP_APPS = [
    {"name": "Браузер", "package": "com.example.browser"},
    {"name": "youtube-proxy", "package": "com.example.youtube_proxy"},
    {"name": "Кинопоиск", "package": "com.example.kinopoisk"},
    {"name": "Система Android", "package": "android"},
    {"name": "Настройки", "package": "com.example.settings"},
    {"name": "Настройки", "package": "com.example.centaur.settings"},
    {"name": "com.example.providers.media", "package": "com.example.providers.media"},
    {"name": "Внешний накопитель", "package": "com.example.externalstorage"},
    {"name": "com.example.overlay.framework", "package": "com.example.overlay.framework"},
]

# «Яндекс Телемост» — публичное приложение, имя как на проводе записи 2026-11-12
# (неразрывный пробел между словами — так его сообщает колонка).
TELE_APPS = EP_APPS + [{"name": "Яндекс\u00a0Телемост", "package": "ru.yandex.telemost"}]


def env_state(apps):
    """EnvironmentStateRaw (обезличенная фикстура): один endpoint, cap 39 со списком."""
    env_type = load_type(schemas_dir(), "alice-api.pb", "NAlice.NAliceApi.TEnvironmentState")
    apps_cap = load_type(schemas_dir(), station_file(), "NAlice.TAppLaunchCapability")
    env = env_type()
    endpoint = env.Endpoints.add()
    endpoint.Id = "fixture-endpoint"
    holder = endpoint.AnyCapabilities.add()
    holder.CapabilityType = station_state.APP_LAUNCH_CAPABILITY
    capability = apps_cap()
    capability.Parameters.SupportsActivateOwnApp = True
    for app in apps:
        item = capability.State.InstalledAppsList.add()
        item.AppName = app["name"]
        item.PackageName = app["package"]
        item.VersionCode = 0
    holder.RawCapability = capability.SerializeToString()
    return env.SerializeToString()


def apps_state(runtime, apps, sn="SN-T"):
    """Устройство конфтест-сессии (SN-T/dev-t) и список приложений колонки."""
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': sn, 'user_id': 'owner',
                               'device_ids': ['dev-t'], 'name': 'Гостиная'})
    runtime.device_apps[sn] = list(apps)


def registry(runtime, apps, sn="SN-A"):
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': sn, 'user_id': 'owner',
                               'device_ids': ['dev-' + sn.lower()], 'name': 'Гостиная'})
    runtime.device_apps[sn] = list(apps)


# --- источник: состояние запроса ------------------------------------------------------


def test_state_carries_installed_apps():
    """cap 39 из EnvironmentStateRaw — сырой список: AppName, PackageName, VersionCode."""
    state = station_state.parse(env_state(EP_APPS))
    assert [app["name"] for app in state["apps"]][:3] == ["Браузер", "youtube-proxy", "Кинопоиск"]
    assert state["apps"][0] == {"name": "Браузер", "package": "com.example.browser", "version_code": 0}


def test_empty_or_broken_state_has_no_apps():
    """Состояния нет/битое — пустой список, не исключение (TV-устройства список не шлют)."""
    assert station_state.parse(b"")["apps"] == []
    assert station_state.parse(b"\xff\xff\xff")["apps"] == []


def test_user_apps_filter():
    """Системные пакеты и записи, где AppName повторяет пакет, в ответ не попадают."""
    names = [app["name"] for app in
             station_state.user_apps(station_state.parse(env_state(EP_APPS))["apps"])]
    assert names == ["Браузер", "youtube-proxy", "Кинопоиск", "Настройки", "Настройки",
                     "Внешний накопитель"]


# --- по серийнику ---------------------------------------------------------------------


async def test_apps_are_per_device(tmp_path):
    """Два серийника — два разных списка в ctx.device["apps"]."""
    runtime = Runtime(tmp_path)
    try:
        registry(runtime, [{"name": "Браузер", "package": "com.example.browser"}], sn="SN-A")
        registry(runtime, [{"name": "Кинопоиск", "package": "com.example.kinopoisk"}], sn="SN-B")
        first = [a["name"] for a in runtime._scenario_context(sn="SN-A")["device"]["apps"]]
        second = [a["name"] for a in runtime._scenario_context(sn="SN-B")["device"]["apps"]]
        assert first == ["Браузер"] and second == ["Кинопоиск"]
        assert runtime._scenario_context(sn="SN-НЕТ")["device"]["apps"] == []
    finally:
        await runtime.store.close()


async def test_app_open_finds_string_names(tmp_path):
    """Сценарий работает с {name, package}-записями и находит приложение по пакету —
    запуск launch_app по найденному пакету (образца запуска для спотифая нет)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "app_open", "server_emulator/scenarios/device/app.open.py")
    app_open = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_open)
    from server_emulator.sdk import ScenarioContext
    runtime = Runtime(tmp_path)
    registry(runtime, [{"name": "Спотифай", "package": "com.example.spotify"},
                       {"name": "youtube-proxy", "package": "com.example.youtube_proxy"}])
    ctx = {"input": {"text": "открой спотифай", "sn": "SN-A", "message_id": "m", "request_id": "r",
                     "dialog_id": "d"},
           "clock": {"now": __import__('datetime').datetime.now()},
           "sequence": 1,
           "slots": {"app": "спотифай"}}
    ctx.update(runtime._scenario_context(sn="SN-A"))
    scenario = ScenarioContext(runtime, ctx)
    result = await app_open.handle(scenario)
    assert result["speech"] == "<speaker audio='open_sound.opus'>"
    assert [d["name"] for d in result["directives"]] == ["launch_app"]
    assert result["directives"][0]["fields"]["PackageToLaunch"] == "com.example.spotify"


async def test_apps_context_names_and_empty():
    runtime = Runtime(__import__('pathlib').Path(__import__('tempfile').mkdtemp()))
    try:
        registry(runtime, [{"name": "YouTube", "package": "com.example.youtube"},
                           {"name": "Спотифай", "package": "com.example.spotify"}])
        text = intelligence.apps_context(runtime, "SN-A")
        assert text and "YouTube" in text and "Спотифай" in text
        assert intelligence.apps_context(runtime, "SN-НЕТ") is None  # нет устройства
        registry(runtime, [])
        assert intelligence.apps_context(runtime, "SN-A") is None  # пустой список
    finally:
        await runtime.store.close()


async def test_agent_prompt_carries_apps(tmp_path):
    runtime = Runtime(tmp_path)
    if runtime.agent is None:
        pytest.skip("LangChain не установлен")
    registry(runtime, [{"name": "YouTube", "package": "com.example.youtube"},
                       {"name": "Спотифай", "package": "com.example.spotify"}])
    try:
        prompt = runtime.agent.system_prompt(runtime, {"sn": "SN-A"})
        assert "YouTube" in prompt and "Спотифай" in prompt
        assert "scenario_run" in prompt
    finally:
        await runtime.close()


async def test_legacy_model_prompt_carries_apps(tmp_path):
    """Режим «модель»: system-сообщение запроса содержит список приложений."""
    runtime = Runtime(tmp_path)
    registry(runtime, [{"name": "YouTube", "package": "com.example.youtube"}])
    runtime.settings["model"]["stub_text"] = ""
    captured = []

    async def provider(kind, payload=None, **kwargs):
        captured.append(json.loads(json.dumps(payload)))
        return json.dumps({"choices": [{"message": {"content": "ок"}}], "usage": {}}).encode(), "application/json"
    runtime.provider = provider
    try:
        await runtime.provider_manager.chat(runtime, "привет", history=[], sn="SN-A")
        system = captured[0]["messages"][0]["content"]
        assert "YouTube" in system
    finally:
        await runtime.close()


# --- wire: список и запуск из состояния запроса ----------------------------------------


async def test_apps_list_answer_from_request_state(tmp_path):
    """«какие приложения установлены?» — перечень из состояния запроса, системные скрыты."""
    runtime = Runtime(tmp_path)
    apps_state(runtime, [])
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    from server_emulator.server import create_app
    from tests.conftest import _open_session, listen
    runner, url = await listen(create_app(runtime=runtime))
    try:
        reply, _, ws = await _open_session(runtime, url, "какие приложения установлены?",
                                           env_state=env_state(EP_APPS))
        await ws.close()
        text = reply.Event.AliceResponse.VoiceResponse.OutputSpeech.Text
        assert text == ("На колонке установлены Браузер, youtube-proxy, Кинопоиск, Настройки, "
                        "Внешний накопитель.")
        assert runtime.device_apps["SN-T"][0]["package"] == "com.example.browser"
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_apps_list_empty_is_honest(tmp_path):
    """Колонка списка не прислала — честный ответ о состоянии, промаха нет."""
    runtime = Runtime(tmp_path)
    apps_state(runtime, [])
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    from server_emulator.server import create_app
    from tests.conftest import _open_session, listen
    runner, url = await listen(create_app(runtime=runtime))
    try:
        reply, _, ws = await _open_session(runtime, url, "какие приложения установлены?")
        await ws.close()
        text = reply.Event.AliceResponse.VoiceResponse.OutputSpeech.Text
        assert text == "Список приложений колонки пока не получен."
        assert runtime.phrase_misses.public() == []  # фраза обслужена, не промах
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_app_open_launches_youtube_webview(tmp_path):
    """«открой ютуб» (в списке «youtube-proxy») — тихий ответ с divui_show_view и
    webview-картой по образцу записи (126042): tts_play_placeholder + карта, речи нет;
    логId/request_id карты — один свежий uuid, константы формы совпадают с образцом."""
    import json as jsonlib
    from server_emulator import app_directives
    runtime = Runtime(tmp_path)
    apps_state(runtime, list(EP_APPS))
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    from server_emulator.server import create_app
    from tests.conftest import _open_session, listen
    runner, url = await listen(create_app(runtime=runtime))
    try:
        reply, _, ws = await _open_session(runtime, url, "открой ютуб", env_state=env_state(EP_APPS))
        await ws.close()
        directives = reply.Event.AliceResponse.Response.Directives
        assert [d.Name for d in directives] == ["tts_play_placeholder", "divui_show_view"]
        assert reply.Event.AliceResponse.VoiceResponse.OutputSpeech.Text == ""  # тихий запуск
        view = load_type(schemas_dir(), station_file(),
                         "NAlice.TLayeredDivUICapability.TDivUIShowViewDirective")
        raw = directives[1].PayloadRaw
        assert raw, "payload_raw обязателен"
        msg = view.FromString(bytes(raw))
        assert msg.Name == "divui_show_view"
        assert msg.InactivityTimeout == 3  # Infinity
        assert msg.Div2Card.CardName == "YouTubeWebviewData"
        assert msg.Div2Card.Id.CardName == "youtube" and msg.Div2Card.Id.CardId == "youtube_key_static_key"
        body = jsonlib.loads(msg.Div2Card.StringBody)
        props = body["card"]["states"][0]["div"]["items"][0]["items"][0]["custom_props"]
        request_id = props["request_id"]
        assert request_id == body["card"]["log_id"] and request_id.count("-") == 4  # свежий uuid, как в записи
        assert props["url"] == "https://m.youtube.com/" and props["content_provider"] == "YOUTUBE"
        # константы формы — байт-в-байт с образцом (кроме подменённого uuid)
        resource = jsonlib.loads((Path(app_directives.CARDS_DIR) / "youtube.json").read_text(encoding="utf-8"))
        sample_body = resource["directive"]["Div2Card"]["StringBody"]  # в ресурсе — уже словарь
        sample_props = sample_body["card"]["states"][0]["div"]["items"][0]["items"][0]["custom_props"]
        sample_props["request_id"] = request_id
        sample_body["card"]["log_id"] = request_id
        assert body == sample_body
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_app_open_launches_by_package(tmp_path):
    """«запусти телемост» (запись .85 2026-11-12 14:28:31): launch_app до
    tts_play_placeholder, payload TLaunchAppDirective по пакету из InstalledAppsList;
    речью — звук-тег образца, карточек нет."""
    runtime = Runtime(tmp_path)
    apps_state(runtime, list(TELE_APPS))
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    from server_emulator.server import create_app
    from tests.conftest import _open_session, listen
    runner, url = await listen(create_app(runtime=runtime))
    try:
        reply, _, ws = await _open_session(runtime, url, "запусти телемост",
                                           env_state=env_state(TELE_APPS))
        await ws.close()
        alice = reply.Event.AliceResponse
        directives = alice.Response.Directives
        assert [d.Name for d in directives] == ["launch_app", "tts_play_placeholder"]
        action = directives[0]
        assert action.Type == "client_action" and action.IsLedSilent
        launch = load_type(schemas_dir(), station_file(),
                           "NAlice.TAppLaunchCapability.TLaunchAppDirective")()
        launch.ParseFromString(bytes(action.PayloadRaw))
        assert json_format.MessageToDict(launch, preserving_proto_field_name=True) == {
            "PackageToLaunch": "ru.yandex.telemost",
            "IntentAction": "android.intent.action.MAIN",
            "Analytics": {"AppLaunch": {"PackageName": "ru.yandex.telemost",
                                        "VisibleName": "Яндекс\u00a0Телемост"}}}
        assert alice.VoiceResponse.OutputSpeech.Text == "<speaker audio='open_sound.opus'>"
        assert len(alice.Response.Cards) == 0  # в образце карточек нет
    finally:
        await runner.cleanup()
        await runtime.close()




async def test_app_open_not_in_list(tmp_path):
    """Приложения нет в присланном списке — «нет среди установленных»."""
    runtime = Runtime(tmp_path)
    apps_state(runtime, [])
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    from server_emulator.server import create_app
    from tests.conftest import _open_session, listen
    runner, url = await listen(create_app(runtime=runtime))
    try:
        reply, _, ws = await _open_session(runtime, url, "открой фейсбук",
                                           env_state=env_state(EP_APPS))
        await ws.close()
        text = reply.Event.AliceResponse.VoiceResponse.OutputSpeech.Text
        assert text == "Приложения «фейсбук» нет среди установленных на этой колонке."
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_app_open_browser_and_settings_named_directives(tmp_path):
    """«открой Браузер»/«открой Настройки» (запись .85 2026-11-12 13:56:15/13:56:21):
    tts_play_placeholder + client_action open_browser_directive/open_android_settings
    с пустым payload и IsLedSilent, речи нет."""
    runtime = Runtime(tmp_path)
    apps_state(runtime, list(EP_APPS))
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    from server_emulator.server import create_app
    from tests.conftest import _open_session, listen
    runner, url = await listen(create_app(runtime=runtime))
    try:
        for phrase, directive in (("открой браузер", "open_browser_directive"),
                                  ("открой настройки", "open_android_settings")):
            reply, _, ws = await _open_session(runtime, url, phrase, env_state=env_state(EP_APPS))
            await ws.close()
            directives = reply.Event.AliceResponse.Response.Directives
            assert [d.Name for d in directives] == ["tts_play_placeholder", directive], phrase
            action = directives[1]
            assert action.Type == "client_action" and action.IsLedSilent, phrase
            assert not action.PayloadRaw and len(action.Payload.fields) == 0, phrase
        assert reply.Event.AliceResponse.VoiceResponse.OutputSpeech.Text == ""  # тихое открытие
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_app_open_system_screen_without_list(tmp_path):
    """Список приложений не получен — системный экран всё равно открывается:
    директива по образцу записи, про список не гадаем."""
    runtime = Runtime(tmp_path)
    apps_state(runtime, [])
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    from server_emulator.server import create_app
    from tests.conftest import _open_session, listen
    runner, url = await listen(create_app(runtime=runtime))
    try:
        reply, _, ws = await _open_session(runtime, url, "открой настройки")
        await ws.close()
        assert [d.Name for d in reply.Event.AliceResponse.Response.Directives] == \
            ["tts_play_placeholder", "open_android_settings"]
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_app_open_without_list_is_honest(tmp_path):
    """Список не получен — про запуск не гадаем, честный ответ о состоянии."""
    runtime = Runtime(tmp_path)
    apps_state(runtime, [])
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    from server_emulator.server import create_app
    from tests.conftest import _open_session, listen
    runner, url = await listen(create_app(runtime=runtime))
    try:
        reply, _, ws = await _open_session(runtime, url, "открой ютуб")
        await ws.close()
        text = reply.Event.AliceResponse.VoiceResponse.OutputSpeech.Text
        assert text == "Список приложений колонки пока не получен."
    finally:
        await runner.cleanup()
        await runtime.close()
